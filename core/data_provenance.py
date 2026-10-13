"""数据来源与有效性（PROVENANCE）。

目的（用户明确要求）
--------------------
每一项参数都要能回答四个问题：
  1. **来源**是什么（哪家数据商 / 哪个接口）；
  2. **网址**是什么（可点开自己去核对）；
  3. **有效性**如何——**正常**还是**错误**（错误必须给出原因）；
  4. **数据时间**是什么（数据本身的日期；以及本次读取的精确时间，到秒）。

设计
----
* `CATALOG` 是"参数 → 来源 → 网址"的**单一事实源**（不依赖各源自己上报 URL）；
* 运行期状态来自快照 `snap["data_status"]`（`StatusRegistry` 的 to_dict，
  含 ok / message / updated_at(到秒) / data_time / label）；
* 数据日期来自新鲜度闸门 `snap["freshness"]["rows"]`（data_date / age_days / status）；
* **绝不用代理值冒充真实值**：BPGDM 真实源失败时如实标【错误】并附原因；
  代理值单独一行、明确标注"非真实 BPGDM"。

对外只有一个入口：`build_provenance(cfg, snap) -> dict`。
"""
from __future__ import annotations

from datetime import datetime

# --------------------------------------------------------------------------
# 参数目录（单一事实源）：key / 中文名 / 提供方 / 核对网址 / 状态名匹配关键字
# --------------------------------------------------------------------------
CATALOG: list[dict] = [
    {
        "key": "etf_close",
        "label": "ETF 收盘行情（6 只）",
        "source": "同花顺扶摇（备源：腾讯证券 → 新浪财经）",
        # 核对页用程序**实际会读的腾讯前复权日线接口**（页面轻、直接给日期与收盘价）；
        # 备用页为腾讯自选股人读页（较慢，接外部节点后可能超时）。
        "url": "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
               "?param=sh513100,day,,,10,qfq",
        "alt_url": "https://gu.qq.com/sh513100/gp",
        "expect": ["qfqday"],
        "alt_expect": ["513100"],
        "match": ["ETF行情"],
        "note": "前复权日线；T 日收盘产生信号，T+1 执行。核对页为腾讯备源接口，"
                "可直接看到 513100 的日期与收盘价；主源为扶摇 API。",
    },
    {
        "key": "etf_realtime",
        "label": "ETF 盘中参考报价",
        "source": "腾讯证券 qt.gtimg.cn",
        "url": "https://qt.gtimg.cn/q=sh513100",
        "expect": ["513100"],
        "match": [],
        "note": "仅供显示，不参与判断（快照 realtime 块）",
    },
    {
        "key": "gold",
        "label": "国际金价",
        "source": "Yahoo Finance（COMEX 黄金连续 GC=F）",
        # 核对页：程序**实际读的就是这个接口**，页面直接给出日期与收盘价，最便于比对；
        # 备用页为新浪纽约黄金（大陆可访问，不依赖任何节点）。
        "url": "https://query1.finance.yahoo.com/v8/finance/chart/GC=F?range=5d&interval=1d",
        "alt_url": "https://finance.sina.com.cn/futures/quotes/GC.shtml",
        "expect": ["GC=F", "close"],
        "alt_expect": ["黄金", "GC"],
        "match": ["国际金价"],
        "note": "Yahoo 行情接口需可达网络；若 WAF 按 UA 指纹拦人，程序会自动换"
                "「诚实 UA / 系统 curl」通道再取。核对页与该接口一致，备用页为新浪纽约黄金。",
    },
    {
        "key": "dxy",
        "label": "美元指数 DXY",
        "source": "Yahoo Finance（DX-Y.NYB）",
        "url": "https://query1.finance.yahoo.com/v8/finance/chart/DX-Y.NYB?range=5d&interval=1d",
        "alt_url": "https://finance.sina.com.cn/money/forex/hq/DINIW.shtml",
        "expect": ["DX-Y.NYB", "close"],
        "alt_expect": ["美元指数", "DINIW"],
        "match": ["美元指数"],
        "note": "核对页为程序实际读取的 Yahoo 接口（直接给出日期与收盘值）；"
                "备用页为新浪美元指数 DINIW（大陆可访问）。",
    },
    {
        "key": "real_rate",
        "label": "美国10年期实际利率",
        "source": "美国财政部官方 CSV（备源：FRED DFII10）",
        "url": "https://fred.stlouisfed.org/series/DFII10",
        "expect": ["DFII10"],
        "match": ["美国10年期实际利率"],
        "note": "TIPS 口径。FRED 的前置 WAF 会拦「浏览器 UA + 非浏览器握手」，"
                "程序自动换诚实 UA 通道（见【说明】列的取数通道）。",
    },
    {
        "key": "nominal_rate",
        "label": "美国10年期国债收益率",
        "source": "美国财政部官方 CSV（备源：FRED DGS10）",
        "url": "https://fred.stlouisfed.org/series/DGS10",
        "expect": ["DGS10"],
        "match": ["美国10年期国债收益率"],
        "note": "",
    },
    {
        "key": "fed",
        "label": "Fed 利率预期（2Y−13W 政策预期代理）",
        "source": "美国财政部官方 CSV",
        # 核对页用**同一份财政部官方数据的网页视图（TextView）**：程序实际读的是
        # 财政部 CSV（是文件下载，无头浏览器截图会又慢又空白），故核对页改用该 CSV
        # 对应的官方 HTML 表格页——同一数据源、同一批 2Y/13W，能正常渲染与截图。
        "url": "https://home.treasury.gov/resource-center/data-chart-center/"
               "interest-rates/TextView"
               "?type=daily_treasury_yield_curve&field_tdr_date_value=2026",
        "alt_url": "https://fred.stlouisfed.org/series/DGS2",
        "expect": ["Daily Treasury", "Par Yield", "Treasury"],
        "alt_expect": ["DGS2"],
        "match": ["Fed利率预期"],
        "note": "负值=市场定价未来降息（利多黄金）；核对页为财政部官方利率页（含 2Y 与 13W）。",
    },
    {
        "key": "gvz",
        "label": "黄金波动率指数 GVZ",
        "source": "Cboe 官方 → Yahoo ^GVZ → FRED GVZCLS（官方镜像）",
        "url": "https://fred.stlouisfed.org/series/GVZCLS",
        "expect": ["GVZCLS", "GVZ"],
        "match": ["黄金波动率指数GVZ"],
        "note": "系统研究分档，非官方阈值",
    },
    {
        "key": "bpgdm_real",
        "label": "金矿股广度 BPGDM（真实值）",
        "source": "StockQ $BPGDM（官方镜像）／用户自定义 URL",
        "url": "https://en.stockq.org/index/BPGDM.php",
        # 源站对本机 IP 段 403 时，用阅读器转发页来做**同样的核对截图**
        # （页面内容一致、能直接看到 BPGDM 数值）。
        "alt_url": "https://r.jina.ai/https://en.stockq.org/index/BPGDM.php",
        "expect": ["BPGDM"],
        "match": ["BPGDM"],
        "note": "0~100。源站对本机 IP 段 403 时，程序经 r.jina.ai 阅读器**转发**取同一页面"
                "（来源仍是 StockQ），并在【说明】列注明『经转发』；"
                "**任何情况下都不得用代理值冒充真实 BPGDM**。",
    },
    {
        "key": "bpgdm_proxy",
        "label": "BPGDM 代理【非真实 BPGDM】",
        "source": "本地：约 27 只金矿股站上 200 日均线占比",
        "url": "",
        "match": [],
        "note": "趋势广度代理，仅作方向参考，**不参与正式宏观评分**",
    },
    {
        "key": "fear_greed",
        "label": "Gold Fear & Greed",
        "source": "官方（用户自定义 URL）／本地代理",
        "url": "",
        "match": ["Gold Fear & Greed"],
        "note": "官方接口不可用时如实显示，不伪造",
    },
]

_LABELS = {"OK": "正常", "CACHE": "缓存（未过期·本次未联网）",
           "CACHE_FALLBACK": "缓存（实时源失败）", "ERROR": "错误",
           "UNKNOWN": "未尝试", "UNCONFIGURED": "未配置"}

# 状态 → 展示标记（表格/报告共用，避免各处各写一套）
_MARKS = {
    "OK": "✅ 正常（实时）",
    "CACHE": "○ 缓存（未过期·本次未联网）",
    "CACHE_FALLBACK": "⚠ 缓存（实时源失败）",
    "ERROR": "❌ 错误",
    "UNCONFIGURED": "○ 未配置",
    "UNKNOWN": "— 未尝试",
}

# 说明里出现这些词，说明"缓存"是**实时源失败后的兜底**，而不是有意走缓存
_FAIL_WORDS = ("失败", "不可达", "异常", "错误", "403", "超时", "Error", "error")


def _match_status(status_list: list[dict], keys: list[str]) -> dict | None:
    """在状态登记表里按名称关键字找一条（命中第一个）。"""
    if not keys:
        return None
    for kw in keys:
        for st in status_list:
            if kw in str(st.get("name", "")):
                return st
    return None


def _fmt_seconds(s) -> str | None:
    """把时间字符串规范到 秒 精度（'YYYY-MM-DD HH:MM:SS'）。"""
    if not s:
        return None
    t = str(s).strip().replace("T", " ")
    if len(t) == 16:            # 只有到分钟 → 补秒
        return t + ":00"
    return t[:19]


def build_provenance(cfg, snap: dict | None) -> dict:
    """汇总「数据来源与有效性」表。返回 {rows, checked_at, ok_count, err_count}。"""
    snap = snap or {}
    status_list = [s for s in (snap.get("data_status") or []) if isinstance(s, dict)]
    fresh_rows = {r.get("key"): r
                  for r in ((snap.get("freshness") or {}).get("rows") or [])}
    macro = snap.get("macro") or {}

    # 实际生效的取数通道（浏览器 UA / 诚实 UA / 系统 curl）——用于如实说明
    # "这份数是怎么取回来的"，尤其是走了兜底通道时（否则用户会以为直连就拿到了）。
    chan_map: dict[str, str] = {}
    chan_text: dict[str, str] = {}
    try:
        from data_sources.http_util import CHANNEL_TEXT, host_channels

        chan_map = host_channels() or {}
        chan_text = CHANNEL_TEXT or {}
    except Exception:  # noqa: BLE001
        pass

    def chan_of(url: str) -> str:
        try:
            from urllib.parse import urlparse

            h = (urlparse(str(url or "")).hostname or "").lower()
        except Exception:  # noqa: BLE001
            return ""
        return chan_map.get(h, "")

    def cfg_url(key: str) -> str:
        """允许用户在配置里覆盖核对网址。"""
        try:
            return str(cfg.get(f"data_sources.verify_url.{key}", "") or "").strip()
        except Exception:  # noqa: BLE001
            return ""

    rows: list[dict] = []
    for ent in CATALOG:
        key = ent["key"]
        st = _match_status(status_list, ent["match"])
        fr = fresh_rows.get(key) or {}

        ok = None
        detail = ""
        updated_at = None
        mode = ""
        from_cache = False
        force_channel = ""
        data_date = fr.get("data_date")

        if st is not None:
            ok = bool(st.get("ok"))
            detail = str(st.get("message") or "")
            updated_at = _fmt_seconds(st.get("updated_at"))
            mode = str(st.get("label") or "")
            from_cache = bool(st.get("from_cache")) or ("缓存" in mode) \
                or ("使用缓存" in detail)
            data_date = data_date or st.get("data_time")
        elif key == "etf_realtime":
            rt = snap.get("realtime") or {}
            rows_nonempty = bool(rt)
            ok = True if rows_nonempty else None
            detail = ("盘中参考报价已取到；仅显示，不参与判断"
                      if rows_nonempty else "未取到（不影响判断）")
            updated_at = _fmt_seconds(rt.get("fetched_at") or rt.get("time"))
            data_date = data_date or rt.get("data_date")
            mode = "本次联网获取" if rows_nonempty else ""
        elif key == "bpgdm_proxy":
            bm = macro.get("bpgdm_metrics") or {}
            if bm.get("real_available"):
                ok = None
                detail = "已有真实 BPGDM，未使用代理值"
                mode = "未使用"
            elif bm.get("proxy_available"):
                ok = True
                detail = (bm.get("bpgdm_proxy_algo")
                          or "本地趋势广度代理（非真实 BPGDM）")
                data_date = bm.get("bpgdm_proxy_data_date") or data_date
                mode = "本地计算"
            else:
                ok = False
                detail = ("代理不可用：" + str(bm.get("real_error")
                                          or "金矿股行情取不到，无法计算趋势广度"))
                mode = "本地计算失败"
        else:
            ok = None
            detail = "本次未尝试该数据源"

        # BPGDM 真实值：**以 bpgdm_metrics.real_available 为准**，不看登记表的 ok
        # ——因为真实源失败时代理会兜底并登记 ok=True，若不管就会把代理误判成"正常"。
        if key == "bpgdm_real":
            bm = macro.get("bpgdm_metrics") or {}
            if bm.get("real_available"):
                ok = True
                detail = bm.get("bpgdm_real_source") or "真实 BPGDM 已取到"
                if bm.get("bpgdm_relay"):
                    detail = ("取自 StockQ 官方 $BPGDM 页面；因源站对本机 IP 段 403，"
                              "经 r.jina.ai 阅读器**转发**（来源仍是 StockQ，如实注明）")
                    force_channel = "reader"
                data_date = bm.get("bpgdm_real_data_date") or data_date
                mode = "本次联网获取"
            else:
                ok = False
                detail = "真实源取数失败：" + (bm.get("real_error")
                                            or "BPGDM 源本次不可用")
                data_date = None
                mode = "取数失败（未用代理冒充真实值）"

        if ok is True:
            # 拿到了数据，但要区分三种情况，绝不能都算"正常"：
            #   ① 本次联网取到实时数据      → OK
            #   ② 命中未过期缓存（本次没联网）→ CACHE（中性，属正常策略）
            #   ③ 实时源失败、退回旧缓存      → CACHE_FALLBACK（警示，必须说明原因）
            if from_cache:
                status = ("CACHE_FALLBACK"
                          if any(w in detail for w in _FAIL_WORDS) else "CACHE")
            else:
                status = "OK"
        elif ok is False:
            # "未配置（可选源）"不是错误——它是可选项没配，别误报成故障。
            status = "UNCONFIGURED" if "未配置" in detail else "ERROR"
        else:
            status = "UNKNOWN"

        text = _LABELS[status]
        mark = _MARKS[status]
        if status == "OK" and mode == "本地计算":
            # 本地算出来的（如 BPGDM 代理）别写"实时"，那会让人以为是联网取到的。
            text, mark = "正常（本地计算）", "✅ 正常（本地计算）"

        row_url = cfg_url(key) or ent["url"]
        eff_ch = force_channel or chan_of(row_url)
        if eff_ch and eff_ch != "browser" and status in ("OK", "CACHE", "CACHE_FALLBACK"):
            if eff_ch == "reader":
                # 阅读器转发的说明已经写在 detail 里了，不重复
                pass
            else:
                cn = f"取数通道：{chan_text.get(eff_ch, eff_ch)}（直连 UA 被拦，已自动换通道）"
                detail = (detail + "｜" if detail else "") + cn

        rows.append({
            "key": key,
            "label": ent["label"],
            "source": ent["source"],
            "url": row_url,
            "ok": ok,
            "status": status,
            "status_text": text,
            "status_mark": mark,
            "from_cache": from_cache,
            "detail": detail,
            "data_date": data_date,
            "updated_at": updated_at,
            "mode": mode,
            "channel": eff_ch,
            "note": ent.get("note", ""),
        })

    ok_count = sum(1 for r in rows if r["status"] == "OK")
    cache_count = sum(1 for r in rows if r["status"] == "CACHE")
    cache_fb = sum(1 for r in rows if r["status"] == "CACHE_FALLBACK")
    err_count = sum(1 for r in rows if r["status"] == "ERROR")
    other = len(rows) - ok_count - cache_count - cache_fb - err_count
    return {
        "rows": rows,
        "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "ok_count": ok_count,
        "cache_count": cache_count,
        "cache_fallback_count": cache_fb,
        "err_count": err_count,
        "total": len(rows),
        "headline": (f"数据源：{ok_count} 实时正常 ｜ "
                     f"{cache_count + cache_fb} 缓存（其中 {cache_fb} 项是实时源失败后兜底）｜ "
                     f"{err_count} 错误 ｜ {other} 未配置或未尝试"),
    }


def render_block(prov: dict) -> list[str]:
    """把来源表渲染成 Markdown（供 CLI 报告复用）。"""
    L = ["| 参数 | 来源 | 状态 | 数据日期 | 读取时间(到秒) | 取数方式 | 网址 |",
         "|---|---|---|---|---|---|---|"]
    for r in (prov.get("rows") or []):
        mark = r.get("status_mark") or _MARKS.get(r.get("status") or "", "—")
        det = mark
        if r.get("detail") and (r.get("status") != "OK"
                                or r.get("channel") not in ("", "browser")):
            # OK 行平时不啰嗦；但若走了兜底通道（诚实 UA / curl / 阅读器转发），
            # 必须写出来 —— 用户有权知道"这份数是怎么取回来的"。
            det += f"：{r['detail']}"
        L.append(f"| {r['label']} | {r['source']} | {det} | "
                 f"{r.get('data_date') or '—'} | {r.get('updated_at') or '—'} | "
                 f"{r.get('mode') or '—'} | {r.get('url') or '—'} |")
    return L
