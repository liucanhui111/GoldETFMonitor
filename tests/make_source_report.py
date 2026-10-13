"""生成「数据来源与有效性」核对报告（HTML）。

把两件事并排放到一页里，方便用户**一眼判断数据对不对**：
  ① 程序这次实际取到的来源表（来源 / 状态 / 数据日期 / 读取时间到秒 / 网址）；
  ② 每个来源页的**实时截图**（由 tests/shot_sources.py 产出）。

用法（托管解释器，需联网）：
    python tests/make_source_report.py
输出：
    __shots/sources/REPORT.html
"""
from __future__ import annotations

import html
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "__shots", "sources")

_STATUS_CLS = {
    "OK": "ok", "CACHE": "cache", "CACHE_FALLBACK": "warn",
    "ERROR": "err", "UNCONFIGURED": "mut", "UNKNOWN": "mut",
}
_SHOT_TXT = {"ok": "✅ 正常", "ok-alt": "✅ 正常（经中转核对页）",
             "error": "❌ 错误", "wrong-page": "❌ 内容不符",
             "timeout": "⚠ 截图超时（页面过重）", "no-shot": "⚠ 未截到"}
_CHANNEL_TXT = {"browser": "浏览器 UA（直连）",
                "honest": "诚实 UA（WAF 一致性校验）",
                "curl": "系统 curl 通道",
                "reader": "阅读器转发（r.jina.ai）"}


def _e(x) -> str:
    return html.escape("" if x is None else str(x))


def main() -> int:
    sys.path.insert(0, ROOT)
    os.makedirs(OUT, exist_ok=True)

    from core.config import get_config
    from core.pipeline import run_full

    cfg = get_config()
    print("· 运行流水线（强制联网，以反映真实可达性）…")
    snap = run_full(cfg, progress=None, use_cache_first=False,
                    with_macro=True, save=False)
    prov = snap.get("provenance") or {}
    rows = prov.get("rows") or []

    shots = {}
    p = os.path.join(OUT, "_results.json")
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as fh:
                shots = json.load(fh)
        except Exception:  # noqa: BLE001
            shots = {}

    parts = [
        "<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>",
        "<title>数据来源与有效性核对报告</title>",
        "<style>",
        ":root{--bg:#f5f6f8;--card:#fff;--tx:#1f2328;--mut:#6b7280;--bd:#e1e5ea;",
        "--ok:#0f766e;--warn:#b45309;--err:#c0261b;--acc:#2563eb}",
        "*{box-sizing:border-box}",
        "body{margin:0;background:var(--bg);color:var(--tx);"
        "font:14px/1.6 'Microsoft YaHei',system-ui,sans-serif}",
        ".wrap{max-width:1180px;margin:0 auto;padding:24px 20px 60px}",
        "h1{font-size:24px;margin:0 0 6px}",
        ".sub{color:var(--mut);margin-bottom:18px}",
        ".card{background:var(--card);border:1px solid var(--bd);border-radius:12px;"
        "padding:16px 18px;margin-bottom:18px;box-shadow:0 1px 2px rgba(0,0,0,.03)}",
        "h2{font-size:17px;margin:0 0 12px}",
        "table{border-collapse:collapse;width:100%;font-size:13px}",
        "th,td{border:1px solid var(--bd);padding:7px 9px;text-align:left;"
        "vertical-align:top}",
        "th{background:#f0f2f5;font-weight:600}",
        "code{background:#f0f2f5;padding:1px 5px;border-radius:4px;font-size:12px}",
        ".ok{color:var(--ok);font-weight:600}.cache{color:var(--mut);font-weight:600}",
        ".warn{color:var(--warn);font-weight:600}.err{color:var(--err);font-weight:600}",
        ".mut{color:var(--mut)}",
        ".shot{display:grid;grid-template-columns:1fr;gap:14px}",
        ".shot figure{margin:0;border:1px solid var(--bd);border-radius:10px;"
        "overflow:hidden;background:#fff}",
        ".shot figcaption{padding:9px 12px;font-size:13px;border-bottom:1px solid var(--bd)}",
        ".shot img{display:block;width:100%;height:auto}",
        ".kv{color:var(--mut);font-size:12px}",
        ".pill{display:inline-block;padding:1px 8px;border-radius:999px;"
        "font-size:12px;border:1px solid var(--bd);background:#f7f8fa}",
        "a{color:var(--acc)}",
        "</style></head><body><div class='wrap'>",
        "<h1>数据来源与有效性核对报告</h1>",
        f"<div class='sub'>生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}　｜　"
        f"来源表核对时间：{_e(prov.get('checked_at'))}　｜　"
        f"{_e(prov.get('headline'))}</div>",
    ]

    # ---- ① 来源表 ----
    parts.append("<div class='card'><h2>① 程序本次取数：来源与有效性</h2>")
    parts.append("<table><tr><th>参数</th><th>数据来源</th><th>状态</th>"
                 "<th>数据日期</th><th>读取时间(到秒)</th><th>说明</th>"
                 "<th>核对网址</th></tr>")
    for r in rows:
        cls = _STATUS_CLS.get(r.get("status") or "", "mut")
        url = r.get("url") or ""
        link = (f"<a href='{_e(url)}' target='_blank'>打开核对</a>" if url else "—")
        det = _e(r.get("detail"))
        if r.get("mode"):
            det += (f"<br><span class='kv'>取数方式：{_e(r.get('mode'))}</span>")
        ch = r.get("channel") or ""
        if ch and ch != "browser":
            det += (f"<br><span class='kv'>取数通道："
                    f"{_e(_CHANNEL_TXT.get(ch, ch))}</span>")
        parts.append(
            f"<tr><td>{_e(r.get('label'))}</td><td>{_e(r.get('source'))}</td>"
            f"<td class='{cls}'>{_e(r.get('status_mark'))}</td>"
            f"<td>{_e(r.get('data_date') or '—')}</td>"
            f"<td>{_e(r.get('updated_at') or '—')}</td>"
            f"<td>{det or '—'}</td><td>{link}</td></tr>")
    parts.append("</table></div>")

    # ---- ② 来源页实时截图 ----
    parts.append("<div class='card'><h2>② 来源页实时截图（可自行核对数字）</h2>")
    parts.append("<div class='kv' style='margin-bottom:12px'>"
                 "截图由本机已安装的 Chrome/Edge 无头模式直接抓取；"
                 "打不开的页面（如被 WAF 拦截的 403）也照样截图留证。</div>")
    parts.append("<div class='shot'>")
    for r in rows:
        key = r.get("key")
        s = shots.get(key)
        png = f"{key}.png"
        if not s or not os.path.exists(os.path.join(OUT, png)):
            continue
        st = _SHOT_TXT.get(s.get("status"), s.get("status"))
        cap = (f"<b>{_e(r.get('label'))}</b>　"
               f"<span class='pill'>{st}</span>"
               f"{('　' + _e(s.get('why'))) if s.get('why') else ''}"
               f"<br><span class='kv'>{_e(s.get('url'))}　"
               f"抓取于 {_e(s.get('at'))}（{s.get('dt', 0)/1000:.1f}s）</span>")
        parts.append(f"<figure><figcaption>{cap}</figcaption>"
                     f"<img src='{png}' alt='{_e(key)}'>")
        alt = s.get("alt_png")
        if alt and os.path.exists(os.path.join(OUT, alt)):
            parts.append(
                f"<figcaption class='kv'>备用核对页（内容一致）："
                f"{_e(s.get('alt_url'))}</figcaption>"
                f"<img src='{alt}' alt='{_e(key)}_alt'>")
        parts.append("</figure>")
    parts.append("</div></div>")

    # ---- ③ 说明 ----
    parts.append(
        "<div class='card'><h2>③ 怎么用这份报告</h2>"
        "<ul>"
        "<li><b>看状态列</b>：<span class='ok'>✅ 正常（实时）</span>＝本次联网取到；"
        "<span class='cache'>○ 缓存（未过期）</span>＝本次有意用缓存、没联网；"
        "<span class='warn'>⚠ 缓存（实时源失败）</span>＝实时源连不上、退回旧缓存；"
        "<span class='err'>❌ 错误</span>＝连不上且没有可用数据。</li>"
        "<li><b>看读取时间</b>：精确到秒，直接和右侧截图页面上的数据时间比对。</li>"
        "<li><b>看截图</b>：与第 ① 张表的数值互相对照；数字对不上就是有问题。"
        "核对页优先用<b>程序实际读取的接口</b>（如 Yahoo 行情接口、腾讯前复权日线、"
        "财政部 CSV），页面直接给出日期与数值，最容易比对。</li>"
        "<li><b>取数通道</b>：若某行标了「取数通道：诚实 UA / 系统 curl / 阅读器转发」，"
        "说明该源的前置 WAF 拦了「浏览器 UA + 非浏览器握手」，程序自动换了通道"
        "（同一秒就能拿到），<b>这不是故障</b>，但如实标出，免得你以为直连就拿到了。</li>"
        "<li><b>已知</b>：StockQ 的 BPGDM 真实源对本机 IP 段一律 <code>403 Forbidden</code>"
        "（nginx WAF）。程序改经 <code>r.jina.ai</code> 阅读器<b>转发同一页面</b>，"
        "取到的仍是<b>真实 BPGDM</b>（说明列会写「经转发」）；"
        "只有在转发也不可用时才退回代理，且代理<b>不参与宏观评分</b>。</li>"
        "<li><b>提示</b>：截图脚本为独立存证工具，个别页面太重（如财政部图表页）"
        "可能「⚠ 截图超时」——这只说明<b>截图慢</b>，不代表程序取数失败"
        "（程序读的是同一家的 CSV/接口，通常几百毫秒）。</li>"
        "</ul></div>")

    parts.append("</div></body></html>")

    path = os.path.join(OUT, "REPORT.html")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(parts))
    print(f"完成 → {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
