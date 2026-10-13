"""打包脚本：生成 CLI 与 GUI 两个 EXE。

重要（本机环境）：
    · 沙箱禁止批量删除（>50 文件会直接终止进程，且不是可捕获异常），
      所以**绝不能** rmtree / 不要用 PyInstaller 的 --clean。
    · 做法：构建到全新目录（_buildtmp / _distnew），再把结果拷贝覆盖到 dist/。
      拷贝是覆盖写，不触发删除保护。

用法（必须用托管解释器）：
    "C:/Users/Administrator/.workbuddy/binaries/python/envs/default/Scripts/python.exe" tests/build_exe.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time

PY = sys.executable
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXCLUDES = ["tkinter", "matplotlib", "numpy", "pandas", "PIL", "scipy"]
TARGETS = [
    ("GoldETFMonitor-cli", "--console"),
    ("GoldETFMonitor", "--windowed"),
]
WORK = "_buildtmp"
DIST_NEW = "_distnew"

# 交付包【根目录】文档（保持包根清爽，只放入口文档 + 程序会直接读取的 P0 证据报告）。
# ⚠ 后三份被运行时/自检按包根目录读取，不能移入 reports/（移动会破坏可信等级
#    的 P0 齐备性判定、策略页展示与打包版自检）：
#      · core/strategy_validation.py 读 4 份 P0 报告判可信等级
#      · ui/strategy_page.py 读 STRATEGY_VALIDATION_REPORT.md 展示
#      · tests/selftest.py 读 DATA_VALIDATION_REPORT / BACKTEST_REPORT /
#        BACKTEST_REAL_6ETF / STRATEGY_VALIDATION_REPORT
ROOT_DOCS = [
    "README.txt",
    "TEST_REPORT.md",
    "DATA_SOURCE.md",
    "BACKTEST_REPORT.md",
    "DATA_VALIDATION_REPORT.md",
    "STRATEGY_VALIDATION_REPORT.md",
    "BACKTEST_REAL_6ETF.md",
]

# 研究 / 审计 / 验证类报告：只随包交付作证据，程序不读取 → 统一归入 reports/ 子目录，
# 不再摊在包根目录（旧布局把它们全铺在根目录，一眼看不出主次）。
REPORT_DOCS = [
    # ---- 交易价值重构 ----
    "STRATEGY_VALIDATION_V2.md",
    "TRADE_POLICY_VALIDATION.md",
    "SELL_VALIDATION_REPORT.md",
    "PARAMETER_SENSITIVITY_REPORT.md",
    "BACKTEST_REAL_6ETF_V2.md",
    "SHADOW_TRADING_REPORT.md",
    # ---- 事件流水线审计 ----
    "EVENT_FUNNEL_REPORT.md",
    "SAMPLE_ADEQUACY_REPORT.md",
    "TRADE_EVENT_AUDIT.md",
    "STRATEGY_VALIDATION_V3.md",
]


def _copy_docs(dst: str) -> None:
    """把交付文档复制进 dist 包：入口文档→根目录，研究报告→reports/。

    只写不删（本机禁批量删除）；缺失只告警不中断。
    兼容旧布局：若包根目录残留研究报告（老版本直接摊在根），移动到 reports/
    —— 移动是重命名，不是删除，安全。
    """
    reports_dir = os.path.join(dst, "reports")
    copied_root: list[str] = []
    copied_rep: list[str] = []
    missing: list[str] = []
    for name in ROOT_DOCS:
        src = os.path.join(ROOT, name)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(dst, name))
            copied_root.append(name)
        else:
            missing.append(name)
        # 兼容旧布局：ROOT_DOC 若曾被误放进 reports/，移回包根目录。
        stray = os.path.join(reports_dir, name)
        if os.path.exists(stray):
            shutil.move(stray, os.path.join(dst, name))
    for name in REPORT_DOCS:
        src = os.path.join(ROOT, name)
        if os.path.exists(src):
            os.makedirs(reports_dir, exist_ok=True)
            shutil.copy2(src, os.path.join(reports_dir, name))
            copied_rep.append(name)
        else:
            missing.append(name)
        # 旧布局残留：根目录若有同名报告，移入 reports/
        stray = os.path.join(dst, name)
        if os.path.exists(stray):
            os.makedirs(reports_dir, exist_ok=True)
            shutil.move(stray, os.path.join(reports_dir, name))
    print(f"  文档：根目录 {len(copied_root)} 份 / reports {len(copied_rep)} 份"
          + (f"；缺失 {missing}" if missing else ""), flush=True)


def _stamp_build() -> None:
    """打包前把构建时刻写进 core/version.py 的 BUILD_STAMP。

    目的：窗口标题栏能显示"构建时间"，用户一眼可辨 EXE 新旧
    （2026-10-09 用户反馈打开的还是旧界面，无法区分版本）。
    源码态该文件同步更新，属预期行为（记录最近一次打包时刻）。
    """
    import re
    stamp = time.strftime("%Y-%m-%d %H:%M")
    path = os.path.join(ROOT, "core", "version.py")
    text = open(path, encoding="utf-8").read()
    text = re.sub(r'BUILD_STAMP = "[^"]*"', f'BUILD_STAMP = "{stamp}"', text)
    open(path, "w", encoding="utf-8").write(text)
    print(f"  版本戳：BUILD_STAMP = {stamp}", flush=True)


def main() -> int:
    os.chdir(ROOT)
    _stamp_build()
    # ⚠ 每次必须用【全新目录】：若 workpath/distpath 已存在，PyInstaller 会去删除它，
    #   从而触发本机的批量删除保护、进程被直接杀掉（try/except 抓不到）。
    #   所以用时间戳目录，永远没有"已存在的旧目录"需要删。
    ts = time.strftime("%H%M%S")
    work = f"{WORK}_{ts}"
    dist_new = f"{DIST_NEW}_{ts}"
    print(f"工作目录 {work} / {dist_new}（全新建，不做任何删除）", flush=True)
    rc = 0
    for name, mode in TARGETS:
        # 注意：--specpath 会让 --add-data 的相对路径以 spec 目录为基准，
        # 所以源路径必须用绝对路径。
        cmd = [PY, "-m", "PyInstaller", "--noconfirm", mode,
               "--name", name, "--paths", ROOT,
               "--add-data", os.path.join(ROOT, "config", "config.json") + ";config",
               "--workpath", os.path.join(ROOT, work, name),
               "--distpath", os.path.join(ROOT, dist_new),
               "--specpath", os.path.join(ROOT, work)]
        for ex in EXCLUDES:
            cmd += ["--exclude-module", ex]
        cmd.append("main.py")
        log = os.path.join(ROOT, "logs", f"_build_{name}.log")
        os.makedirs(os.path.dirname(log), exist_ok=True)
        print(f"打包 {name} …", flush=True)
        with open(log, "w", encoding="utf-8", errors="replace") as fh:
            p = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT)
        src = os.path.join(ROOT, dist_new, name)
        dst = os.path.join(ROOT, "dist", name)
        exe = os.path.join(dst, name + ".exe")
        if p.returncode == 0 and os.path.isdir(src):
            os.makedirs(dst, exist_ok=True)
            shutil.copytree(src, dst, dirs_exist_ok=True)   # 只写不删
            # EXE 旁边的 config/config.json 是用户可编辑配置，升级时会被保留。
            # 但若它缺少源码新增的必需键，会导致打包版行为/自检与源码不一致
            # （例如缺 backtest.costs → “成本参数化”自检判失败；缺
            #  hist.allow_distance_relax → 与“3× 硬上限不放宽”口径不符）。
            # 这里只【补齐缺失键】并按需同步 hist.horizons，绝不覆盖用户已设的值。
            try:
                import json
                src_cfg = json.load(open(os.path.join(ROOT, "config", "config.json"),
                                         encoding="utf-8"))
                dst_cfg_p = os.path.join(dst, "config", "config.json")
                if os.path.exists(dst_cfg_p):
                    dst_cfg = json.load(open(dst_cfg_p, encoding="utf-8"))
                    fixed: list[str] = []

                    def _backfill(path: list[str], src_val) -> None:
                        node = dst_cfg
                        for k in path[:-1]:
                            node = node.setdefault(k, {})
                        if path[-1] not in node:
                            node[path[-1]] = src_val
                            fixed.append(".".join(path))

                    for path in (["hist", "allow_distance_relax"],
                                 ["backtest", "costs"],
                                 ["trust", "buy_permission_min_level"],
                                 ["data_sources", "realtime_quote"],
                                 ["data_sources", "ths_timeout_seconds"],
                                 ["data_sources", "bpgdm_use_reader"],
                                 ["gold", "radar",
                                  "use_sentiment_proxy_when_official_missing"]):
                        node = src_cfg
                        ok = True
                        for k in path:
                            if isinstance(node, dict) and k in node:
                                node = node[k]
                            else:
                                ok = False
                                break
                        if ok:
                            _backfill(path, node)

                    # ETF 缓存 TTL：源码默认已由 720(12h) 下调为 60(1h)。
                    # 若 dist 仍是旧的 720，跟随源码同步；用户自定义过的其它值不动。
                    try:
                        src_ttl = src_cfg["data_sources"]["cache_ttl_minutes"]["etf"]
                        dst_ttl = (dst_cfg.setdefault("data_sources", {})
                                   .setdefault("cache_ttl_minutes", {}).get("etf"))
                        if dst_ttl == 720 and src_ttl != 720:
                            dst_cfg["data_sources"]["cache_ttl_minutes"]["etf"] = src_ttl
                            fixed.append("data_sources.cache_ttl_minutes.etf(720→%s)"
                                         % src_ttl)
                    except Exception:  # noqa: BLE001
                        pass

                    if (src_cfg.get("hist", {}).get("horizons")
                            != dst_cfg.get("hist", {}).get("horizons")):
                        dst_cfg.setdefault("hist", {})["horizons"] = \
                            src_cfg["hist"]["horizons"]
                        fixed.append("hist.horizons(sync)")
                    if fixed:
                        print("  ⚠ dist config 已补齐/同步必需键："
                              f"{fixed}（用户其余设置保留）", flush=True)
                        with open(dst_cfg_p, "w", encoding="utf-8") as fh:
                            json.dump(dst_cfg, fh, ensure_ascii=False, indent=2)
            except Exception as exc:  # noqa: BLE001
                print(f"  ⚠ config 同步检查失败: {exc}", flush=True)
            _copy_docs(dst)   # 报告与 README 进入交付包根目录
            print(f"  {name}: OK -> {exe} ({os.path.getsize(exe)} bytes)",
                  flush=True)
        else:
            print(f"  {name}: FAILED rc={p.returncode} src_exists="
                  f"{os.path.isdir(src)}", flush=True)
            rc = 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
