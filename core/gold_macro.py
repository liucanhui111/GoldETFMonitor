"""黄金宏观加权评分 + 底部雷达三级分级（用户策略 2026-10 版）。

一、黄金宏观评分（0~100，50 = 中性，>50 = 利多黄金环境）
    国际黄金趋势  20%  核心方向
    美元指数      15%  黄金反向压力
    美国10Y实际利率 15%  核心宏观变量
    美国10Y收益率 10%  利率压力
    Fed利率预期   10%  中期政策方向（2Y-3M 政策预期代理）
    159937日线    10%  实际持仓
    黄金波动率    10%  市场情绪（GVZ，只测波动幅度不测方向）
    BPGDM         10%  金矿股市场广度，黄金底部雷达

二、底部雷达 8 条件与三级分级（满足越多，底部信号越强）：
    ① 黄金不再创新低
    ② 黄金波动率 GVZ 低位（系统研究分档：正常基准及以下 = <20）
    ③ 黄金波动率 GVZ 低位拐头向上（低位前提：偏高及以下 = <30）
    ④ BPGDM < 30（真实值优先；代理只作方向参考）
    ⑤ BPGDM 开始向上拐
    ⑥ 美元开始走弱
    ⑦ 美国实际利率开始下降
    ⑧ 黄金突破前一个日线反弹高点

    仅 ①②③          → 「恐慌区」
    ①②③④⑤          → 「底部观察区」
    ①~⑧ 全满足        → 「趋势反转确认区」
    其余               → 「未入底部信号」

【硬约束】GVZ 与 BPGDM 都**不能单独触发黄金买入**：
  · GVZ 只用于识别"波动/恐慌状态"（且只测幅度、不测方向）；
  · BPGDM（或代理）只用于识别"金矿股内部广度"；
  · 只有与国际金价趋势 / DXY / 美国10Y实际利率 / Fed 预期 / 金价回撤
    等条件**共同满足**时，才提高"底部观察"分数；
  · 严禁出现「GVZ<20 → 自动买入」「BPGDM<30 → 自动买入」；
  · 底部雷达整体仍受系统【BUY Permission Gate】限制（见 core/decision.py）：
    研究级下即使雷达满格也只输出"持有观察/暂停加仓"，不输出买入。

所有子分只由真实数据推导；某项数据缺失时该项不参与加权（权重重归一），
条件判定为 None（不算满足），界面如实标注，绝不伪造。
"""
from __future__ import annotations

from data_sources.gvz_source import gvz_band

from .indicators import zigzag_pivots


def _clip(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


# --------------------------------------------------------------------------
# 一、宏观加权评分
# --------------------------------------------------------------------------
WEIGHTS = {
    "trend": 0.20, "usd": 0.15, "real": 0.15, "nominal": 0.10,
    "fed": 0.10, "etf159937": 0.10, "gvz": 0.10, "bpgdm": 0.10,
}


def gold_macro_score(macro: dict, gold_ind: dict | None = None) -> dict:
    gm = macro.get("gold_metrics") or {}
    um = macro.get("usd_metrics") or {}
    rm = macro.get("real_rate_metrics") or {}
    nm = macro.get("nominal_rate_metrics") or {}
    pm = macro.get("policy_metrics") or {}
    bm = macro.get("bpgdm_metrics") or {}
    gv = macro.get("gvz_metrics") or {}

    comps: list[dict] = []

    def add(key: str, name: str, score, reason: str) -> None:
        comps.append({"key": key, "name": name, "weight": WEIGHTS[key],
                      "score": None if score is None else round(_clip(score, 0, 100), 1),
                      "available": score is not None, "reason": reason})

    # 1) 国际黄金趋势 20%
    if gm.get("available"):
        s = 50.0
        r20 = gm.get("ret20_pct")
        if r20 is not None:
            s += _clip(r20, -10, 10) * 1.6
        s += 8 if (gm.get("ma60") and gm.get("last", 0) > gm["ma60"]) else -8
        s += 6 if (gm.get("ma200") and gm.get("last", 0) > gm["ma200"]) else -6
        ddh = gm.get("dd_from_high_pct")
        if ddh is not None:
            s += _clip(ddh, -20, 0) * 0.5
        add("trend", "国际黄金趋势", s,
            f"20日 {r20:+.2f}%，MA60/200 上方={'是' if s and gm.get('last') and gm.get('ma60') and gm['last'] > gm['ma60'] else '否'}")
    else:
        add("trend", "国际黄金趋势", None, "暂无数据")

    # 2) 美元指数 15%（反向）
    if um.get("available"):
        s = 50.0
        r20 = um.get("ret20_pct")
        if r20 is not None:
            s -= _clip(r20, -5, 5) * 3.6
        if um.get("weak_confirmed"):
            s += 8
        elif r20 is not None and r20 > 1:
            s -= 8
        add("usd", "美元指数（反向）", s,
            f"DXY 20日 {r20:+.2f}%" if r20 is not None else "暂无变化数据")
    else:
        add("usd", "美元指数（反向）", None, "暂无数据")

    # 3) 美国10Y实际利率 15%（反向）
    if rm.get("available"):
        s = 50.0
        chg = rm.get("chg_20d")
        if chg is not None:
            s -= _clip(chg, -0.5, 0.5) * 40
        if rm.get("falling"):
            s += 8
        elif rm.get("rising"):
            s -= 8
        add("real", "美国10Y实际利率（反向）", s,
            f"20日 {chg:+.2f}pp" if chg is not None else "暂无变化数据")
    else:
        add("real", "美国10Y实际利率（反向）", None, "暂无数据")

    # 4) 美国10Y收益率 10%（反向）
    if nm.get("available"):
        s = 50.0
        chg = nm.get("chg_20d")
        if chg is not None:
            s -= _clip(chg, -0.5, 0.5) * 30
        if nm.get("falling"):
            s += 5
        elif nm.get("rising"):
            s -= 5
        add("nominal", "美国10Y收益率（反向）", s,
            f"20日 {chg:+.2f}pp" if chg is not None else "暂无变化数据")
    else:
        add("nominal", "美国10Y收益率（反向）", None, "暂无数据")

    # 5) Fed 利率预期 10%
    if pm.get("available"):
        s = 50.0
        last = pm.get("last")
        if last is not None:
            s -= _clip(last, -2, 2) * 15
        if pm.get("easing"):
            s += 10
        elif (last or 0) > 0.05:
            s -= 6
        add("fed", "Fed利率预期", s, pm.get("direction") or "")
    else:
        add("fed", "Fed利率预期", None, "暂无数据")

    # 6) 159937 日线 10%（实际持仓 ETF 自身状态）
    if gold_ind and gold_ind.get("n"):
        r20 = (gold_ind.get("ret20") or [None])[-1]
        rel1y = gold_ind.get("relpos", {}).get("1y")
        s = 50.0
        if r20 is not None:
            s += _clip(r20, -10, 10) * 1.2
        if rel1y is not None:
            s += _clip((rel1y - 50) * 0.2, -8, 8)
        add("etf159937", "159937日线（实际持仓）", s,
            f"20日 {r20:+.2f}%，1年相对价位 {rel1y:.0f}" if r20 is not None and rel1y is not None else "部分数据缺失")
    else:
        add("etf159937", "159937日线（实际持仓）", None, "暂无数据")

    # 7) 黄金波动率 GVZ 10%（★系统研究分档★给分；GVZ 只测幅度不测方向、
    #    不是买卖指标；20/30 等阈值为系统研究参数，非官方参数）
    if gv.get("available"):
        last = gv.get("last")
        band = gv.get("band") or gvz_band(last)
        idx = (band or {}).get("index", 3)
        # 研究分档 → 分数：低波动档位对黄金环境偏利多；极端波动不给高评价
        s = {0: 64.0, 1: 62.0, 2: 58.0, 3: 52.0, 4: 46.0, 5: 40.0}.get(idx, 52.0)
        if gv.get("turning_up"):
            s += 12          # 低位拐头：避险资金重新进场（对应条件③）
        c20 = gv.get("chg_20d")
        if c20 is not None and c20 < 0:
            s += 6           # 波动降温
        # 注意：不再因"GVZ 上行"直接扣分——高 GVZ 在黄金牛市同样常见
        #       （2026-01 黄金创新高时 GVZ 达 46），把波动放大当利空是已知误读。
        if idx >= 4:
            s = min(s, 46.0)  # 高/极端压力档不给高评价（不可依赖）
        chg_txt = f"，20日 {c20:+.1f}" if c20 is not None else ""
        bname = band.get("name") if band else "—"
        add("gvz", "黄金波动率指数GVZ", s,
            f"GVZ {last:.1f} → 系统研究分档『{bname}』（非官方阈值{chg_txt}）")
    else:
        add("gvz", "黄金波动率指数GVZ", None, "暂无数据")

    # 8) BPGDM 10%（真实值才叫 BPGDM 且才参与评分）
    #    代理值与真实 BPGDM 口径不同（趋势广度 ≈ 粘性 P&F 的近似），
    #    若把代理当作评分输入会把宏观评分带偏 —— 因此**代理不参与正式评分**，
    #    只并列展示作方向参考，并如实说明真实源取数失败。
    if bm.get("real_available"):
        pct = bm.get("percentile_2y")
        s = 50.0
        if pct is not None:
            s += (50 - pct) * 0.3
        if bm.get("turning_up"):
            s += 15
        if bm.get("below_ma60"):
            s -= 5
        add("bpgdm", "真实BPGDM", s,
            f"2年分位 {pct}%" if pct is not None else "")
    elif bm.get("available"):
        pct = bm.get("percentile_2y")
        add("bpgdm", "BPGDM代理（非真实BPGDM）", None,
            "真实 BPGDM 取数失败，代理仅作方向参考、不参与评分"
            + (f"（代理 2 年分位 {pct}%）" if pct is not None else "")
            + (f"｜失败原因：{bm.get('real_error')}" if bm.get("real_error") else ""))
    else:
        add("bpgdm", "真实BPGDM", None, "暂无数据（代理亦不可用）")

    usable = [c for c in comps if c["available"] and c["score"] is not None]
    if not usable:
        return {"available": False, "score": None, "grade": None,
                "grade_label": "暂无数据", "components": comps,
                "drivers": [], "drags": [],
                "note": "宏观数据不可用时评分为空，绝不伪造"}
    score = sum(c["score"] * c["weight"] for c in usable) / \
        sum(c["weight"] for c in usable)
    grade = "bull" if score >= 62 else ("bear" if score < 45 else "neutral")
    grade_label = {"bull": "利多黄金环境", "bear": "偏空黄金环境",
                   "neutral": "中性"}[grade]
    ranked = sorted(usable, key=lambda c: -c["score"])
    return {
        "available": True,
        "score": round(score, 1),
        "grade": grade,
        "grade_label": grade_label,
        "components": comps,
        "drivers": [f"{c['name']} {c['score']:.0f}" for c in ranked[:3]],
        "drags": [f"{c['name']} {c['score']:.0f}" for c in reversed(ranked[-3:])],
        "note": "50=中性；评分只描述宏观环境，不构成仓位建议（黄金永远≤5%）",
    }


# --------------------------------------------------------------------------
# 二、底部雷达：8 条件 + 三级分级
# --------------------------------------------------------------------------
LEVEL_LABELS = ["未入底部信号", "恐慌区", "底部观察区", "趋势反转确认区"]


def _prev_swing_high(gold: dict | None):
    """条件⑧：最近一个【已确认】的日线反弹高点（zigzag），返回 (price, ok)。"""
    if not gold or not gold.get("close") or len(gold["close"]) < 60:
        return None, None
    high = gold.get("high") or gold["close"]
    low = gold.get("low") or gold["close"]
    close = gold["close"]
    thr = [c * 0.03 if c else None for c in close]
    pivots, _live = zigzag_pivots(high, low, close, thr)
    highs = [p for p in pivots if p["type"] == "H"]
    if not highs:
        return None, None
    ph = highs[-1]
    return ph["price"], bool(close[-1] > ph["price"])


def gold_bottom_radar(macro: dict, cfg=None) -> dict:
    gm = macro.get("gold_metrics") or {}
    um = macro.get("usd_metrics") or {}
    rm = macro.get("real_rate_metrics") or {}
    bm = macro.get("bpgdm_metrics") or {}
    gv = macro.get("gvz_metrics") or {}
    fg = macro.get("fear_greed") or {}

    conditions: list[dict] = []

    def cond(no: int, name: str, ok, detail: str) -> dict:
        return {"no": no, "name": name, "ok": ok, "detail": detail}

    # ① 黄金不再创新低
    v = gm.get("stopped_new_low")
    conditions.append(cond(1, "黄金不再创新低",
                           bool(v) if v is not None else None,
                           "近20日最低点未出现在最近5日" if v else
                           ("仍在创新低" if v is False else "暂无数据")))

    # ② 波动率低位判定：真实 GVZ 优先；官方不可用时按配置决定是否采用本地代理。
    #    输出 panic_input：真实GVZ / 官方F&G / 本地代理 / 最终采用值 / 采用原因。
    #    GVZ 只测波动幅度、不测方向；<20 为【系统研究参数】而非官方阈值。
    fg = macro.get("fear_greed") or {}
    proxy = macro.get("gold_sentiment_proxy") or {}
    pval = proxy.get("score") if proxy.get("available") else None
    panic: dict = {
        "official_gvz": gv.get("last") if gv.get("available") else None,
        "fg_official": fg.get("score"),
        "fg_proxy": pval,
        "adopted": None, "reason": "",
    }
    if gv.get("available"):
        ok = gv.get("below_20")
        _b = (gv.get("band") or {}).get("name") or "—"
        detail = (f"GVZ {gv.get('last')} → 系统研究分档『{_b}』"
                  f"（研究参数：正常基准及以下即 <20；非官方阈值）")
        panic["adopted"] = f"真实GVZ {gv.get('last')}（{_b}）"
        panic["reason"] = ("GVZ 可用，按系统研究分档判定（只测幅度不测方向；"
                           "20/30 为系统研究参数，非官方参数）")
    else:
        allow_proxy = bool(cfg.get(
            "gold.radar.use_sentiment_proxy_when_official_missing", False)) \
            if cfg is not None else False
        if allow_proxy and pval is not None:
            ok = bool(pval <= 40)
            detail = (f"GVZ 暂无数据；按配置采用【黄金情绪代理】{pval}"
                      f"（代理阈值 ≤40）——代理判定，非真实 GVZ")
            panic["adopted"] = f"黄金情绪代理 {pval}（阈值 ≤40）"
            panic["reason"] = "GVZ 不可用，配置 gold.radar.use_sentiment_proxy_when_official_missing=true 允许代理替代"
        else:
            ok = None
            miss_note = "" if pval is None else \
                f"；本地代理 {pval} 可用但配置未允许替代（不把代理当真实 GVZ）"
            detail = f"GVZ 暂无数据（波动率低位无法判定）{miss_note}"
            panic["reason"] = "真实 GVZ 不可用且配置未启用代理替代 → 条件按【未知】处理"
    conditions.append(cond(2, "黄金波动率指数GVZ 低位（<20，研究参数）", ok, detail))

    # ③ GVZ 开始向上（低位拐头；"低位"= 偏高及以下，<30，与系统研究分档一致）
    if gv.get("available"):
        ok = gv.get("turning_up")
        _b = (gv.get("band") or {}).get("name") or "—"
        detail = (f"低位拐头向上（当前 系统研究分档『{_b}』）" if ok else
                  (f"尚未拐头（5日 {gv.get('chg_5d')}，当前『{_b}』）"
                   if gv.get("chg_5d") is not None else f"数据不足（当前『{_b}』）"))
    else:
        ok, detail = None, "GVZ 暂无数据"
    conditions.append(cond(3, "黄金波动率指数开始向上", ok, detail))

    # ④ BPGDM < 30（研究解释区间：<30 超卖/弱势；0.0 是合法值，不得被 falsy 吞掉）
    #    真实值优先；代理只作方向参考，必须叫「BPGDM代理」而非「BPGDM」。
    #    GVZ/BPGDM 都不能单独触发买入——本条件只有与①②③等共同满足才三级分级。
    bm_real = bool(bm.get("real_available"))
    bm_label = "真实BPGDM" if bm_real else "BPGDM代理"
    if bm.get("available"):
        last = bm.get("last")
        ok = bool(bm.get("oversold_30", last is not None and last < 30))
        extra = f"，2年分位 {bm.get('percentile_2y')}%" if bm.get("percentile_2y") is not None else ""
        bias = "" if bm_real else "（代理非真实 BPGDM，与官方存在水平偏差，仅作方向参考）"
        detail = (f"{'真实' if bm_real else '代理'}值 {last}（研究区间 <30）{extra}，"
                  f"{bm.get('source') or ''}{bias}")
    else:
        ok, detail = None, f"{bm_label} 暂无数据"
    conditions.append(cond(4, f"{bm_label} <30", ok, detail))

    # ⑤ BPGDM 开始向上拐（低位拐头：2年分位 <30 且近 5 日回升）
    if bm.get("available"):
        ok = bm.get("turning_up")
        pct = bm.get("percentile_2y")
        tail = f"（2年分位 {pct}%）" if pct is not None else ""
        detail = (f"低位拐头{tail}" if ok else
                  (f"尚未拐头{tail}" if ok is False else "数据不足"))
    else:
        ok, detail = None, f"{bm_label} 暂无数据"
    conditions.append(cond(5, f"{bm_label} 开始向上拐", ok, detail))

    # ⑥ 美元开始走弱
    if um.get("available"):
        r20 = um.get("ret20_pct")
        ok = bool(um.get("weak_confirmed") or (r20 is not None and r20 < -1.0))
        detail = (f"DXY {um.get('last'):.2f}，20日 {r20:+.2f}%"
                  if r20 is not None else "暂无变化数据")
    else:
        ok, detail = None, "美元指数暂无数据"
    conditions.append(cond(6, "美元开始走弱", ok, detail))

    # ⑦ 美国实际利率开始下降
    if rm.get("available"):
        ok = rm.get("falling")
        chg = rm.get("chg_20d")
        detail = (f"实际利率 {rm.get('last')}%，20日 {chg:+.2f}pp"
                  if chg is not None else "暂无变化数据")
    else:
        ok, detail = None, "实际利率暂无数据"
    conditions.append(cond(7, "美国实际利率开始下降", ok, detail))

    # ⑧ 黄金突破前一个日线反弹高点
    ph, ok = _prev_swing_high(macro.get("gold"))
    if ok is None:
        conditions.append(cond(8, "黄金突破前一个日线反弹高点", None,
                               "金价数据不足"))
    else:
        conditions.append(cond(8, "黄金突破前一个日线反弹高点", ok,
                               f"前一反弹高点 {ph:.2f}，现价 {gm.get('last'):.2f}"
                               if gm.get("last") is not None else f"前一反弹高点 {ph:.2f}"))

    known = [c for c in conditions if c["ok"] is not None]
    hit = sum(1 for c in known if c["ok"])
    missing = [c["no"] for c in conditions if c["ok"] is None]

    def all_true(nums: tuple[int, ...]) -> bool:
        return all((next(c["ok"] for c in conditions if c["no"] == n)) is True
                   for n in nums)

    if all_true((1, 2, 3, 4, 5, 6, 7, 8)):
        level = 3
    elif all_true((1, 2, 3, 4, 5)):
        level = 2
    elif all_true((1, 2, 3)):
        level = 1
    else:
        level = 0
    status = LEVEL_LABELS[level]

    note = "即使雷达满格，黄金仓位也绝不能超过 5%"
    if missing:
        note += f"；条件 {missing} 数据缺失，分级可能被低估"

    return {
        "hit": hit, "total": len(conditions),
        "known": len(conditions) - len(missing),
        "unknown": len(missing),
        "conditions": conditions, "status": status, "level": level,
        "level_label": status, "missing": missing, "note": note,
        "panic_input": panic,
    }
