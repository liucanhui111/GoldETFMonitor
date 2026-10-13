"""数据来源页截图存证（用本机 Chrome 无头模式，无需下载任何浏览器）。

为什么
------
用户要求：**贴出来源的实时界面图**，以判断数据的正确性；并且
**连不上相应网站时要显示错误**。本脚本对「数据来源与有效性」表里的
每个来源页逐个截图：

* 能打开 → 截真实页面（存 __shots/sources/<key>.png）；
* 打不开 / 403 / 出错 → **照样截图**（会把错误页截下来），
  并在 INDEX.md 里标成「❌ 错误」＋原因。

实现要点（性能/可靠性）
------------------------
* **每源一次 Chrome 调用**：同一次启动里同时 `--screenshot=` 与 `--dump-dom`
  （实测可行，约 9 秒/页；旧版分两次调用会白白翻倍）。
* **每源一个独立 profile 目录**：避免多个无头实例抢同一 user-data-dir 的锁
  （抢锁会导致截图串页 / 卡死）。
* 每源超时 30 秒，超时也记为警告并写入 INDEX.md。

用法（托管解释器）：
    python tests/shot_sources.py
输出：
    __shots/sources/<key>.png
    __shots/sources/INDEX.md
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "__shots", "sources")
TMP = tempfile.gettempdir()

CHROME_CANDIDATES = [
    os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
    os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
    os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
    os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
    os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
]

TIMEOUT = 40
VTB = "5000"  # virtual-time-budget：给重页面留 5 秒虚拟时间
# 个别重页面单独放宽超时（实测：接外部节点后，新浪/腾讯/财政部这类页面会明显变慢；
# gu.qq 约 63s，财政部页面更慢，故给到 150s）。
TIMEOUTS = {"gold": 90, "dxy": 90, "etf_close": 90, "fed": 200,
            "real_rate": 90, "nominal_rate": 90, "gvz": 90,
            "bpgdm_real": 200}


def find_chrome() -> str | None:
    for p in CHROME_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return None


def _log(msg: str) -> None:
    print(msg, flush=True)


def _is_error_page(dom: str) -> tuple[bool, str]:
    """从 DOM 判断是否拿到了错误页（而非目标页）。"""
    low = dom.lower()
    for kw, why in (("403 forbidden", "HTTP 403 Forbidden（被拦截）"),
                    ("404 not found", "HTTP 404 Not Found"),
                    ("502 bad gateway", "HTTP 502 Bad Gateway"),
                    ("503 service", "HTTP 503 Service Unavailable"),
                    ("429 too many requests", "HTTP 429 请求过多"),
                    ("access denied", "Access Denied"),
                    ("<title>error", "服务器返回错误页"),
                    ("err_connection", "连接失败（ERR_CONNECTION）"),
                    ("err_name_not_resolved", "域名解析失败")):
        if kw in low:
            return True, why
    if "<html" in low and len(dom) < 400:
        return True, f"页面内容过短（{len(dom)} 字节），疑似被拦截"
    return False, ""


def _kill_tree(proc) -> None:
    """按进程树杀掉截图进程（含 Chrome 的 renderer/gpu 孙子进程），避免残留。

    Windows 用 `taskkill /F /T`；其它平台退回 kill 直接子进程。
    """
    if proc is None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=15,
                           creationflags=0x08000000)
        else:
            proc.kill()
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


def shoot(chrome: str, url: str, png: str, key: str = "",
          expect: list[str] | None = None) -> tuple[str, str, int]:
    """对单个 URL：一次调用拿 DOM + 截图。

    返回 (status, why, dt_ms)；status ∈ ok / error / wrong-page / timeout / no-shot。

    `expect`：页面里**应当出现**的关键字（任一命中即可）。若不命中，说明
    打开的其实是别的页（被重定向 / 被墙 / 弹了拦截页）——这正是
    "Yahoo 官网在中国大陆显示不可用提示页"的教训，必须判成错误而不是"正常"。
    """
    timeout = TIMEOUTS.get(key, TIMEOUT)
    prof = os.path.join(TMP, "_shot_prof_" + hashlib.md5(url.encode()).hexdigest()[:10])
    shutil.rmtree(prof, ignore_errors=True)
    if os.path.exists(png):
        os.remove(png)
    cmd = [
        chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
        "--hide-scrollbars", "--no-first-run", "--no-default-browser-check",
        "--disable-extensions", "--disable-background-networking",
        "--disable-sync", "--mute-audio",
        "--virtual-time-budget=" + VTB, "--window-size=1440,1100",
        "--screenshot=" + png, "--dump-dom",
        "--user-data-dir=" + prof, url,
    ]
    t0 = time.time()
    dom = ""
    # 关键修复（2026-10-08）：不能用 subprocess.run(capture_output=True, timeout=…)。
    # Chrome 会派生一堆子/孙进程（renderer、gpu、utility）并**继承 stdout 管道**；
    # 超时后 Python 只杀掉了直接子进程，孙进程仍持有管道写端，于是
    # `communicate()` 会永久阻塞——实测 fed 源卡死 50 分钟不返回。
    # 改用：输出重定向到**文件**（不经过管道），并在超时时按进程树杀干净。
    dom_file = png + ".dom.txt"
    err_file = png + ".err.txt"
    _NO_WINDOW = 0x08000000 if os.name == "nt" else 0
    proc = None
    try:
        with open(dom_file, "wb") as fo, open(err_file, "wb") as fe:
            proc = subprocess.Popen(cmd, stdout=fo, stderr=fe,
                                    creationflags=_NO_WINDOW)
            proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        shutil.rmtree(prof, ignore_errors=True)
        for f in (dom_file, err_file):
            try:
                os.remove(f)
            except Exception:  # noqa: BLE001
                pass
        return "timeout", f"超过 {timeout} 秒未返回（已杀进程树）", int((time.time() - t0) * 1000)
    except Exception as exc:  # noqa: BLE001
        _kill_tree(proc)
        shutil.rmtree(prof, ignore_errors=True)
        return "no-shot", f"启动截图失败：{type(exc).__name__}: {exc}", int((time.time() - t0) * 1000)
    finally:
        shutil.rmtree(prof, ignore_errors=True)

    try:
        if os.path.exists(dom_file):
            dom = open(dom_file, "rb").read().decode("utf-8", "replace")
        if not dom.strip() and os.path.exists(err_file):
            out_txt = open(err_file, "rb").read().decode("utf-8", "replace")
            if "err_" in out_txt.lower():
                dom = out_txt
    finally:
        for f in (dom_file, err_file):
            try:
                os.remove(f)
            except Exception:  # noqa: BLE001
                pass

    dt_ms = int((time.time() - t0) * 1000)
    bad, why = _is_error_page(dom)
    if bad:
        return "error", why, dt_ms
    if expect and not any(str(t) in dom for t in expect):
        return ("wrong-page",
                "页面未包含预期内容（需含：" + " 或 ".join(map(str, expect))
                + "），疑被拦截/重定向到其它页", dt_ms)
    ok_png = os.path.exists(png) and os.path.getsize(png) > 2000
    if not ok_png:
        return "no-shot", "未生成有效截图", dt_ms
    return "ok", "", dt_ms


def main() -> int:
    sys.path.insert(0, ROOT)
    from core.data_provenance import CATALOG

    only = set()
    for a in sys.argv[1:]:
        if a.startswith("--only="):
            only = {k.strip() for k in a.split("=", 1)[1].split(",") if k.strip()}

    chrome = find_chrome()
    if not chrome:
        _log("未找到 Chrome/Edge，无法截图。")
        return 2
    os.makedirs(OUT, exist_ok=True)
    log_path = os.path.join(OUT, "_run.log")
    json_path = os.path.join(OUT, "_results.json")
    _log(f"浏览器：{chrome}")

    # 历史结果（支持 --only 只补拍失败的几个，其余沿用上次）
    prev: dict = {}
    if os.path.exists(json_path):
        try:
            with open(json_path, encoding="utf-8") as fh:
                prev = json.load(fh)
        except Exception:  # noqa: BLE001
            prev = {}

    targets = [e for e in CATALOG if e.get("url")]
    store: dict = dict(prev)
    with open(log_path, "w", encoding="utf-8") as log:
        log.write(f"# shot_sources run {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        todo = [e for e in targets if not only or e["key"] in only]
        for i, ent in enumerate(todo, 1):
            key = ent["key"]
            url = ent["url"]
            png = os.path.join(OUT, f"{key}.png")
            status, why, dt = shoot(chrome, url, png, key=key,
                                    expect=ent.get("expect"))
            # 主核对页打不开时（如 StockQ 对本机 403），改用登记在案的中转页
            # （内容为同一页面），让它也有一张**能看到真实数值**的存证图。
            alt_png = None
            alt_url = ent.get("alt_url")
            if alt_url and status in ("error", "wrong-page", "timeout"):
                alt_png = f"{key}_alt.png"
                st2, why2, dt2 = shoot(chrome, alt_url,
                                       os.path.join(OUT, alt_png), key=key,
                                       expect=ent.get("alt_expect")
                                       or ent.get("expect"))
                _log(f"       ↳ 备用核对页 {alt_url}：{st2} {why2 or '正常'} "
                     f"{dt2/1000:.1f}s")
                log.write(f"       ↳ alt {alt_url} -> {st2} {why2}\n")
                if st2 == "ok":
                    status, why = ("ok-alt",
                                   f"直连页被拦（{why}）；改用中转核对页，内容一致")
                    dt = dt + dt2
                else:
                    alt_png = None
            size = os.path.getsize(png) if os.path.exists(png) else 0
            store[key] = {"label": ent["label"], "url": url, "status": status,
                          "why": why, "size": size, "dt": dt,
                          "alt_url": alt_url if alt_png else None,
                          "alt_png": alt_png,
                          "at": time.strftime("%Y-%m-%d %H:%M:%S")}
            line = (f"  [{i}/{len(todo)}] {key:<14} {status:<8} "
                    f"{why or '正常'}  {dt/1000:.1f}s")
            _log(line)
            log.write(line + "\n")
            log.flush()

    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(store, fh, ensure_ascii=False, indent=2)

    rows = [(e["key"], store[e["key"]]) for e in targets if e["key"] in store]
    lines = ["# 数据来源页截图存证", "",
             f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
             f"- 浏览器：`{chrome}`",
             "- 说明：**能打开→真实页面；打不开→错误页也照样截图并标注**。",
             "- 复现：`python tests/shot_sources.py`（补拍个别源：`--only=gold,dxy`）。", "",
             "| 参数 | 来源页 | 状态 | 说明 | 耗时 | 截图 |",
             "|---|---|---|---|---|---|"]
    for key, r in rows:
        mark = {"ok": "✅ 正常", "ok-alt": "✅ 正常（经中转核对页）",
                "error": "❌ 错误", "wrong-page": "❌ 内容不符",
                "timeout": "⚠ 截图超时（页面过重，非取数失败）",
                "no-shot": "⚠ 未截到"}.get(r["status"], r["status"])
        shot = f"![.{key}]({key}.png)"
        if r.get("alt_png"):
            shot += f"<br>中转页：![.{key}_alt]({r['alt_png']})"
        lines.append(f"| {r['label']} | {r['url']} | {mark} | {r['why'] or '—'} | "
                     f"{r['dt']/1000:.1f}s | {shot} |")
    with open(os.path.join(OUT, "INDEX.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")

    n_ok = sum(1 for _, r in rows if r["status"] == "ok")
    n_bad = sum(1 for _, r in rows if r["status"] in ("error", "timeout", "wrong-page"))
    _log(f"\n完成：{n_ok} 正常 / {n_bad} 错误或超时 / 共 {len(rows)}；"
         f"清单 → {os.path.join(OUT, 'INDEX.md')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
