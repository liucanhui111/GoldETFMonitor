"""个人全天候 ETF 量化资产配置与择时系统 —— 程序入口。

用法
----
双击运行（无参数）        启动 Windows 桌面 GUI
GoldETFMonitor.exe --cli   在终端输出今日完整文本报告
    --cli --no-macro       跳过宏观/黄金数据（离线快速自检）
    --cli --cache-first    优先使用本地缓存
    --selftest             运行内置自检（数据源/指标/无未来函数/回测）
    --backtest             运行组合回测并输出 BACKTEST 报告
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path


def _base_dir() -> Path:
    """可写工作目录：打包后为 exe 所在目录，否则为项目根目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


BASE = _base_dir()
os.environ.setdefault("GOLD_ETF_HOME", str(BASE))
for _p in (str(BASE), getattr(sys, "_MEIPASS", "")):
    if _p and _p not in sys.path:
        sys.path.insert(0, _p)


def bootstrap() -> None:
    """首次运行时创建工作目录与默认配置（绝不覆盖已有配置）。"""
    for sub in ("config", "data", "logs", "data/cache"):
        (BASE / sub).mkdir(parents=True, exist_ok=True)
    cfg_path = BASE / "config" / "config.json"
    if cfg_path.exists():
        return
    cands = []
    mei = getattr(sys, "_MEIPASS", None)
    if mei:
        cands.append(Path(mei) / "config" / "config.json")
    cands.append(Path(__file__).resolve().parent / "config" / "config.json")
    for c in cands:
        try:
            if c.exists() and c.resolve() != cfg_path.resolve():
                shutil.copy2(c, cfg_path)
                return
        except Exception:  # noqa: BLE001
            continue


def _harden_console() -> None:
    """中文 Windows 控制台默认使用 GBK(cp936)，打印 √/×/※ 之外的罕见符号
    （如 ✓ ✅ ⚠）会抛 UnicodeEncodeError 导致程序中断。
    这里统一把标准输出/错误的编码错误策略改为 replace，保证任何字符都不会让程序崩溃，
    同时保留原有编码，中文仍可正常显示。"""
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        try:
            if stream is not None and hasattr(stream, "reconfigure"):
                stream.reconfigure(errors="replace")
        except Exception:  # noqa: BLE001
            pass


def _setup_logging() -> None:
    import logging
    from logging.handlers import RotatingFileHandler

    level = os.environ.get("GOLD_ETF_LOG", "INFO").upper()
    handlers: list[logging.Handler] = []
    try:
        handlers.append(RotatingFileHandler(
            BASE / "logs" / "app.log", maxBytes=2_000_000, backupCount=3,
            encoding="utf-8"))
    except Exception:  # noqa: BLE001
        pass
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler(sys.stderr))
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers, force=True)


def run_cli(args) -> int:
    from core.config import get_config
    from core.pipeline import run_full
    from core.report import render_daily, render_questions

    cfg = get_config(refresh=True)
    print("正在获取数据并计算…（首次运行需要 1-3 分钟）", flush=True)

    def progress(msg: str) -> None:
        print(f"  · {msg}", flush=True)

    snap = run_full(cfg, progress=progress, use_cache_first=args.cache_first,
                    with_macro=not args.no_macro, save=not args.no_save)
    print()
    print(render_daily(snap))
    if snap.get("ok"):
        print()
        print("=" * 60)
        print("每天必须能回答的 10 个问题")
        print("=" * 60)
        for q, a in render_questions(snap):
            print(f"{q}\n    → {a}")
    else:
        print("【错误】" + "\n".join(snap.get("errors", [])[:5]))
        return 2
    if args.out:
        Path(args.out).write_text(render_daily(snap), encoding="utf-8")
        print(f"\n报告已写入：{args.out}")
    return 0


def run_selftest(args) -> int:
    from tests.selftest import main as selftest_main

    return selftest_main()


def run_backtest(args) -> int:
    from core.backtest import run_backtest_cli

    return run_backtest_cli(args)


def run_gui(args) -> int:
    try:
        from PySide6.QtWidgets import QApplication
    except Exception as exc:  # noqa: BLE001
        print("未安装 PySide6，无法启动图形界面。\n"
              "请运行 install.bat 安装依赖，或使用 --cli 模式。\n"
              f"详细信息：{exc}")
        return 3
    from ui.main_window import MainWindow
    from ui.theme import apply_theme

    app = QApplication(sys.argv)
    app.setApplicationName("全天候ETF组合系统")
    apply_theme(app)
    win = MainWindow()
    win.show()
    return app.exec()


def run_shot(args) -> int:
    """离屏渲染截图（用于验证"结论是否真的在最显眼位置"）。

    GoldETFMonitor.exe --shot docs   导出新手/专业/买入态首页 + 黄金ETF页 后退出
    """
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
    import copy

    from PySide6.QtWidgets import QApplication

    from core.config import get_config
    from core.pipeline import run_full
    from ui.main_window import MainWindow
    from ui.theme import apply_theme

    out_dir = Path(args.shot)
    out_dir.mkdir(parents=True, exist_ok=True)

    app = QApplication(sys.argv)
    apply_theme(app)
    cfg = get_config(refresh=True)
    snap = run_full(cfg, use_cache_first=True, with_macro=False, save=False)
    win = MainWindow()
    win.resize(1240, 940)
    win.show()

    def snap_to(name: str, s: dict, w: int, h: int) -> None:
        win.page_portfolio.set_snapshot(s)
        win.resize(w, h)
        # 标签换行/高度要等布局重算后再截图，否则会抓到"上一屏"的排版
        win.page_portfolio.container.adjustSize()
        for _ in range(6):
            app.processEvents()
            win.repaint()
        pix = win.grab()
        dst = out_dir / name
        pix.save(str(dst))
        print(f"已导出 {dst}（{pix.width()}x{pix.height()}）")

    win.page_portfolio.set_simple(True)
    snap_to("shot_home_simple.png", snap, 1240, 940)
    win.page_portfolio.set_simple(False)
    snap_to("shot_home_full.png", snap, 1240, 1100)

    cfg2 = get_config(refresh=True)
    assets = copy.deepcopy(cfg2.data.get("assets") or [])
    for a in assets:
        if a.get("asset_class") == "gold":
            a["shares"] = 500
    cfg2.data["assets"] = assets
    snap2 = run_full(cfg2, use_cache_first=True, with_macro=False, save=False)
    win.page_portfolio.set_simple(True)
    snap_to("shot_home_buy.png", snap2, 1240, 940)

    # ---- 「黄金ETF」页（159937 集成页）：需要宏观/黄金数据才完整 ----
    try:
        snapm = run_full(cfg, use_cache_first=True, with_macro=True, save=False)
    except Exception as exc:  # noqa: BLE001
        print(f"· 宏观数据获取失败，黄金ETF页用离线快照截图：{exc}")
        snapm = snap
    try:
        win.page_gold.set_snapshot(snapm)
        win.stack.setCurrentIndex(2)
        win.resize(1240, 1520)
        for _ in range(8):
            app.processEvents()
            win.repaint()
        pix = win.grab()
        dst = out_dir / "shot_gold_page.png"
        pix.save(str(dst))
        print(f"已导出 {dst}（{pix.width()}x{pix.height()}）"
              f"（雷达 {win.page_gold.tbl_radar.rowCount()} 行 / "
              f"8指标 {win.page_gold.tbl_ind8.rowCount()} 行）")
    except Exception as exc:  # noqa: BLE001
        print(f"【警告】黄金ETF页截图失败：{exc}")

    win.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="个人全天候 ETF 量化资产配置与择时系统")
    parser.add_argument("--cli", action="store_true", help="终端文本报告模式")
    parser.add_argument("--no-macro", action="store_true", help="跳过宏观/黄金数据")
    parser.add_argument("--cache-first", action="store_true", help="优先使用本地缓存")
    parser.add_argument("--no-save", action="store_true", help="不写入本地历史数据")
    parser.add_argument("--out", default="", help="把报告写入指定文件")
    parser.add_argument("--selftest", action="store_true", help="运行内置自检")
    parser.add_argument("--backtest", action="store_true", help="运行组合回测")
    parser.add_argument("--validate-data", dest="validate_data", action="store_true",
                        help="双数据源交叉验证并生成 DATA_VALIDATION_REPORT.md")
    parser.add_argument("--validate-strategy", dest="validate_strategy",
                        action="store_true",
                        help="策略有效性验证并生成 STRATEGY_VALIDATION_REPORT.md")
    parser.add_argument("--validate-trades", dest="validate_trades",
                        action="store_true",
                        help="交易价值 / BUY-SELL OOS 验证，生成 5 份 V2 报告")
    parser.add_argument("--shadow-report", dest="shadow_report",
                        action="store_true",
                        help="影子交易记录与统计，生成 SHADOW_TRADING_REPORT.md")
    parser.add_argument("--event-audit", dest="event_audit",
                        action="store_true",
                        help="事件流水线审计，生成 EVENT_FUNNEL_REPORT / "
                             "SAMPLE_ADEQUACY_REPORT / TRADE_EVENT_AUDIT / "
                             "STRATEGY_VALIDATION_V3 并刷新 V2 家族")
    parser.add_argument("--shot", default="", metavar="DIR",
                        help="离屏导出首页与黄金ETF页截图到指定目录后退出")
    parser.add_argument("--refresh", action="store_true", help="强制刷新配置")
    args = parser.parse_args()

    bootstrap()
    _harden_console()
    _setup_logging()

    if args.selftest:
        return run_selftest(args)
    if args.backtest:
        return run_backtest(args)
    if args.validate_data:
        return run_validate_data(args)
    if args.validate_strategy:
        return run_validate_strategy(args)
    if args.validate_trades:
        return run_validate_trades(args)
    if args.shadow_report:
        return run_shadow_report(args)
    if args.event_audit:
        return run_event_audit_cmd(args)
    if args.shot:
        return run_shot(args)
    if args.cli:
        return run_cli(args)
    return run_gui(args)


def run_validate_data(args) -> int:
    from core.config import get_config
    from core.data_validation import run_and_write

    def progress(msg: str) -> None:
        print(f"  · {msg}", flush=True)

    res, out = run_and_write(get_config(refresh=True), progress)
    print(f"\n已写入：{out}")
    n_ok = sum(1 for r in res.get("rows", []) if r.get("cross", {}).get("available"))
    print(f"交叉验证：{n_ok}/{len(res.get('rows', []))} 只 ETF 完成双源比对")
    return 0 if res.get("ok") else 2


def run_validate_strategy(args) -> int:
    from core.config import get_config
    from core.strategy_validation import run_and_write

    def progress(msg: str) -> None:
        print(f"  · {msg}", flush=True)

    res, out = run_and_write(get_config(refresh=True), progress)
    print(f"\n已写入：{out}")
    print(f"系统可信等级：{res.get('trust_level')}")
    return 0 if res.get("ok") else 2


def run_validate_trades(args) -> int:
    """交易价值 / BUY-SELL 独立 OOS 验证（交易策略重构主入口）。"""
    from core.config import get_config
    from core.policy_reports import run_and_write

    def progress(msg: str) -> None:
        print(f"  · {msg}", flush=True)

    res, written = run_and_write(get_config(refresh=True), progress)
    for p in written:
        print(f"已写入：{p}")
    if not res.get("ok"):
        print("【错误】" + str(res.get("message")))
        return 2
    v = res.get("verdict") or {}
    print(f"\n主判定：{v.get('label')}")
    print(f"能否宣称交易有效：{'是' if v.get('can_claim_effectiveness') else '否'}")
    return 0


def run_shadow_report(args) -> int:
    from core.config import get_config
    from core.shadow_trading import run_and_write

    def progress(msg: str) -> None:
        print(f"  · {msg}", flush=True)

    out, path = run_and_write(get_config(refresh=True), progress)
    print(f"\n已写入：{path}")
    print(f"影子记录：{out.get('rows')} 条")
    return 0


def run_event_audit_cmd(args) -> int:
    """事件流水线审计（事件口径 + 候选/可执行双层 + 研究/生产双口径）。"""
    from core.config import get_config
    from core.event_reports import run_and_write

    def progress(msg: str) -> None:
        print(f"  · {msg}", flush=True)

    res, written = run_and_write(get_config(refresh=True), progress)
    for p in written:
        print(f"已写入：{p}")
    if not res.get("ok"):
        print("【错误】" + str(res.get("message")))
        return 2
    d = res.get("diagnosis") or {}
    print(f"\n旧口径 60 日事件锚点（折数）：{d.get('old_folds_60')}"
          f"（BUY 排除 Risk Off/Crisis → 2）")
    print(f"新口径 60 日：候选日 {res['funnels'][60]['candidate_days']['any']} / "
          f"独立事件 {res['funnels'][60]['independent'][60]}")
    print(f"独立事件理论上界：20日 {res['bounds'][20]} / 60日 {res['bounds'][60]} / "
          f"120日 {res['bounds'][120]}")
    print(f"归因：{d.get('primary')}")
    ans = res.get("answers") or {}
    print(f"最简最稳模型：{ans.get('9_最简最稳模型')}")
    print(f"是否有足够证据：{ans.get('10_是否已有足够证据')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
