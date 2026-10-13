"""首页「实时持仓」面板的核心逻辑：改份额 → 立即重算权重与逐只判断。

设计目标（对应用户口语「实时输入每个 ETF 的份额，然后判断该加该减」）：
    只把「新份额 × 快照现价」重新汇总，再调用与首页**完全相同**的
    signals.rebalance_plan() 得到逐只「加/减/持有」判断。

因此：
  · 不联网、不重跑模型 —— 秒级刷新；
  · 判断口径与整包重算一致（同一函数、同一阈值），只是快；
  · 模型级「BUY 权限」不在此处重算（它取决于当日市场状态，与份额无关），
    由首页沿用当日结论。本模块只负责【再平衡级】判断。

安全边界：本模块**只做计算，不写任何文件、不下单、不联网**。
"""
from __future__ import annotations

from . import signals


def _ok_rows(snap: dict) -> list[dict]:
    """取出可用于重算的持仓行（有成交价快照且有资产定义）。"""
    return [r for r in (snap.get("rows") or [])
            if r.get("snapshot") and r.get("asset")]


def table_rows(snap: dict) -> list[dict]:
    """返回给界面表格用的基础行（份额取配置里的当前值、现价取快照）。

    `price` 是**日线收盘价**（判断用）；`ref_price` 是盘中参考报价
    （snap["realtime"]，**仅供显示，绝不参与判断**）。
    """
    ref_all = snap.get("realtime") or {}
    out = []
    for r in _ok_rows(snap):
        a = r["asset"]
        s = r["snapshot"]
        ref = ref_all.get(str(a.get("id"))) or {}
        out.append({
            "id": a.get("id"),
            "name": a.get("name"),
            "asset_class": a.get("asset_class"),
            "shares": float(a.get("shares") or 0.0),
            "price": s.get("price"),
            "price_time": s.get("data_time"),
            "price_source": s.get("data_source"),
            "price_from_cache": bool(s.get("from_cache")),
            "cache_age_minutes": s.get("cache_age_minutes"),
            "ref_price": ref.get("price"),
            "ref_change_pct": ref.get("change_pct"),
            "ref_time": ref.get("time_hhmm"),
            "market_value": float(s.get("market_value") or 0.0),
            "weight_pct": r.get("weight_pct"),
            "target_pct": r.get("target_weight_pct"),
        })
    return out


def price_caption(snap: dict) -> str:
    """价格口径正文（不含「价格口径」标签，调用方自行加前缀）：

    判断用哪份价、参考价是什么时候的。刻意把「判断口径」与「参考报价」分开写，
    避免用户把参考价当成判断依据。
    """
    rows = table_rows(snap)
    if not rows:
        return "暂无数据。"
    dts = sorted({r.get("price_time") for r in rows if r.get("price_time")})
    srcs = sorted({r.get("price_source") for r in rows if r.get("price_source")})
    cached = sum(1 for r in rows if r.get("price_from_cache"))
    ages = [r.get("cache_age_minutes") for r in rows if r.get("cache_age_minutes")]
    etf_part = f"判断用「现价(日线收盘)」：{dts[-1] if dts else '—'} 收盘"
    if srcs:
        etf_part += f"（{ '、'.join(srcs) }）"
    if cached:
        age = f"，最旧约 {max(ages):.0f} 分钟前抓取" if ages else "，来自本地缓存"
        etf_part += f"；{cached}/{len(rows)} 只取自本地缓存{age}"
    else:
        etf_part += "；均为本次联网抓取"

    try:
        from data_sources.realtime_quote import latest_time
        rt = latest_time(snap.get("realtime") or {})
    except Exception:  # noqa: BLE001
        rt = None
    if rt:
        ref_part = f"「参考现价(盘中)」为实时行情，行情时间 {rt}，仅供参考，不参与判断。"
    else:
        ref_part = ("「参考现价(盘中)」本次未取到；该列仅为参考，"
                    "即使取到也不参与判断。")
    return f"{etf_part}。{ref_part}"


def _total_and_cash(etf_value: float, cfg) -> tuple[float, float, str]:
    """与 optimizer.current_weights 完全相同的资金口径。"""
    declared = 0.0
    cash_declared = None
    if cfg is not None:
        try:
            declared = float(cfg.declared_total_assets or 0.0)
        except Exception:  # noqa: BLE001
            declared = 0.0
        try:
            cash_declared = cfg.declared_cash
        except Exception:  # noqa: BLE001
            cash_declared = None
    if declared > 0:
        return declared, declared - etf_value, "按『总资产 − ETF市值』计算"
    if cash_declared is not None:
        return etf_value + float(cash_declared), float(cash_declared), "按『ETF市值 + 登记现金』计算"
    return etf_value, 0.0, "按 ETF 市值计算（未登记总资产/现金）"


def recompute(snap: dict, shares_map: dict, cfg=None) -> dict:
    """按 shares_map（{asset_id: 份额}）重算权重与逐只判断。

    未在 shares_map 里给出的资产，沿用配置里的当前份额。
    返回：
        {ok, rows[{id,name,asset_class,shares,price,market_value,
                   weight_pct,target_pct,deviation_pct,action,level}],
         total_assets, etf_value, cash_value, source,
         gold_pct, gold_cap_pct, gold_over, need, message}
    """
    ok = _ok_rows(snap)
    if not ok:
        return {"ok": False, "rows": [], "message": "暂无可重算的持仓（数据不足）"}

    new_shares: dict = {}
    for r in ok:
        aid = r["asset"].get("id")
        try:
            new_shares[aid] = float(shares_map.get(aid, r["asset"].get("shares") or 0.0))
        except (TypeError, ValueError):
            new_shares[aid] = float(r["asset"].get("shares") or 0.0)
        if new_shares[aid] < 0:
            new_shares[aid] = 0.0

    # ---- 市值：优先用「新份额 × 现价」；无现价则退回快照市值 ----
    mv: dict = {}
    for r in ok:
        aid = r["asset"]["id"]
        price = r["snapshot"].get("price")
        if price is None:
            mv[aid] = float(r["snapshot"].get("market_value") or 0.0)
        else:
            mv[aid] = new_shares[aid] * float(price)

    etf_value = sum(mv.values())
    total, cash, source = _total_and_cash(etf_value, cfg)
    denom = total if total > 0 else etf_value

    by_class: dict = {}
    for r in ok:
        cls = r["asset"].get("asset_class") or "other"
        w = (mv[r["asset"]["id"]] / denom) if denom else 0.0
        by_class[cls] = by_class.get(cls, 0.0) + w
    by_class["cash"] = (max(0.0, cash) / denom) if denom else 0.0

    # ---- 逐只判断：复用首页同款 rebalance_plan（同一阈值、同一函数）----
    rows2: list[dict] = []
    for r in ok:
        rr = dict(r)
        rr["asset"] = dict(r["asset"])
        rr["scores"] = r.get("scores") or {}
        aid = rr["asset"]["id"]
        rr["weight_pct"] = round((mv[aid] / denom * 100), 2) if denom else 0.0
        rows2.append(rr)

    targets = (snap.get("portfolio") or {}).get("target_class") or {
        "bond": 0.5, "stock": 0.45, "gold": 0.05}
    risk = (snap.get("portfolio") or {}).get("risk") or {}

    rebal: dict = {"items": [], "need": False, "gold_current_pct": 0.0,
                   "gold_cap_pct": 5.0, "gold_state": "正常"}
    if cfg is not None:
        try:
            rebal = signals.rebalance_plan(rows2, by_class, targets, risk, cfg)
        except Exception as exc:  # noqa: BLE001
            rebal = {"items": [], "need": False, "gold_state": "正常",
                     "gold_current_pct": round(by_class.get("gold", 0.0) * 100, 2),
                     "gold_cap_pct": 5.0, "error": str(exc)}

    act = {i.get("id"): i for i in (rebal.get("items") or [])}
    watch = float(rebal.get("watch_threshold") or 3.0)

    out_rows: list[dict] = []
    for r in ok:
        a = r["asset"]
        aid = a.get("id")
        price = r["snapshot"].get("price")
        wp = round((mv[aid] / denom * 100), 2) if denom else 0.0
        tp = r.get("target_weight_pct")
        tp = float(tp) if tp is not None else (float(a.get("target_weight") or 0.0) * 100)
        dev = wp - tp
        it = act.get(aid)
        if it is not None:
            action = it.get("action") or "持有"
            level = it.get("level") or ""
        elif abs(dev) < watch:
            action = "持有（偏离在阈值内）"
            level = ""
        else:
            action = "持有"
            level = ""
        out_rows.append({
            "id": aid,
            "name": a.get("name"),
            "asset_class": a.get("asset_class"),
            "shares": new_shares.get(aid, 0.0),
            "price": price,
            "market_value": round(mv[aid], 2),
            "weight_pct": wp,
            "target_pct": round(tp, 2),
            "deviation_pct": round(dev, 2),
            "action": action,
            "level": level,
        })

    gold_pct = round(by_class.get("gold", 0.0) * 100, 2)
    gold_cap = round(float(rebal.get("gold_cap_pct") or 5.0), 2)
    return {
        "ok": True,
        "rows": out_rows,
        "total_assets": round(total, 2),
        "etf_value": round(etf_value, 2),
        "cash_value": round(cash, 2),
        "source": source,
        "by_class": by_class,
        "gold_pct": gold_pct,
        "gold_cap_pct": gold_cap,
        "gold_over": bool(gold_pct > gold_cap + 1e-9),
        "need": bool(rebal.get("need")),
        "message": "",
    }


def summary_text(res: dict) -> str:
    """把重算结果收成一句可显示的中文摘要。"""
    if not res.get("ok"):
        return res.get("message") or "暂无可重算的持仓"
    parts = [
        f"总资产 {res['total_assets']:,.0f} 元",
        f"ETF 市值 {res['etf_value']:,.0f}",
        f"现金 {res['cash_value']:,.0f}",
        f"黄金 {res['gold_pct']:.2f}%（上限 {res['gold_cap_pct']:.1f}%）",
    ]
    if res.get("gold_over"):
        parts.append("【黄金超配，须先减仓】")
    elif res.get("need"):
        parts.append("【有偏离超过阈值的标的，见下表】")
    else:
        parts.append("【均在阈值内，可不动】")
    return " · ".join(parts)


__all__ = ["table_rows", "recompute", "summary_text", "price_caption"]
