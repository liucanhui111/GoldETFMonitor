"""信号与决策引擎。

方向约定（与 scoring.py / UI 文案严格一致）：
    TrendScore      高 = 趋势越强
    PositionScore   高 = 位置越高（越贵）
    ShortTermScore  高 = 短线越热（越超买）
    SwingScore      高 = 越接近波段底部
    BuyScore / EntryQuality  高 = 越值得配置（但仍需组合层放行）
    RiskScore       高 = 组合风险越大

核心原则（写死在逻辑里，不允许被"指标好看"绕过）：
    1. 目标仓位 ≠ 当前必须买满；WAIT 是完全合法的结果；
    2. 低位置 ≠ 可以买（必须叠加"止跌确认"）；
    3. 高胜率 ≠ 可以重仓（必须叠加回撤、盈亏比、样本可信度、样本外）；
    4. 黄金永远不超过 5%；
    5. 单 ETF 买入质量再高，也必须由组合层决定"买不买、买多少、分几批"。
"""
from __future__ import annotations

from . import scoring

STOCK_CLASSES = {"stock"}
RISK_CLASSES = {"stock", "gold"}


def _num(v, nd: int = 2):
    try:
        if v is None:
            return None
        return round(float(v), nd)
    except (TypeError, ValueError):
        return None


def _map(v, lo: float, hi: float):
    """把 lo..hi 线性映射到 0..100（越界截断）。"""
    if v is None:
        return None
    if hi == lo:
        return 50.0
    return max(0.0, min(100.0, (float(v) - lo) / (hi - lo) * 100.0))


def _avg(vals) -> float | None:
    xs = [float(v) for v in vals if v is not None]
    return sum(xs) / len(xs) if xs else None


# ==========================================================================
# 单 ETF
# ==========================================================================
def etf_snapshot(asset: dict, ind: dict, hist: dict, cfg) -> dict:
    """单 ETF 全部指标快照（供详情页显示）。"""
    bands_r = cfg.get("classification.return20_pct", {})
    bands_b = cfg.get("classification.bias20_pct", {})
    bands_rp = cfg.get("classification.relpos_bands", [20, 35, 65, 80])
    bands_wr = cfg.get("classification.wr_bands", [-20, -50, -80])

    close = ind["close"][-1]
    prev = ind["close"][-2] if ind["n"] > 1 else None
    ma = ind.get("ma", {})
    dd = ind.get("dd_windows", {})
    swing = ind.get("swing", {})

    snap = {
        "id": asset.get("id"),
        "name": asset.get("name"),
        "thscode": asset.get("thscode"),
        "asset_class": asset.get("asset_class"),
        "price": _num(close, 3),
        "change_pct": _num((close / prev - 1) * 100) if (close and prev) else None,
        "data_time": ind["dates"][-1] if ind.get("dates") else None,
        "bars": ind.get("n"),
        "warnings": [],
    }

    snap["ma"] = {k: _num(v[-1], 3) for k, v in ma.items()}
    macd = ind.get("macd", {})
    snap["macd"] = {k: _num(v[-1], 4) for k, v in macd.items()}
    snap["rsi14"] = _num(ind.get("rsi14", [None])[-1])
    snap["atr14"] = _num(ind.get("atr14", [None])[-1], 3)
    snap["atr_pct"] = _num(ind.get("atr_pct", [None])[-1])
    snap["wr14"] = _num(ind.get("wr14", [None])[-1])
    snap["bias20"] = _num(ind.get("bias20", [None])[-1])
    snap["ret20"] = _num(ind.get("ret20", [None])[-1])
    snap["ret20_class"] = scoring.classify_return20(snap["ret20"], bands_r)
    snap["bias20_class"] = scoring.classify_bias20(snap["bias20"], bands_b)
    snap["vol20_annual_pct"] = _num(ind.get("vol20_annual_pct"))

    snap["dd"] = {
        k: {
            "max_drawdown_pct": _num(v.get("max_drawdown_pct")),
            "current_drawdown_pct": _num(v.get("current_drawdown_pct")),
            "highest": _num(v.get("highest"), 3),
            "lowest": _num(v.get("lowest"), 3),
            "available": v.get("available", False),
        }
        for k, v in dd.items()
    }
    snap["relpos"] = {k: _num(v) for k, v in (ind.get("relpos") or {}).items()}
    snap["relpos_class"] = {
        k: scoring.classify_relpos(v, bands_rp) for k, v in snap["relpos"].items()
    }
    snap["high_low"] = ind.get("high_low", {})
    snap["wr_class"] = scoring.classify_wr(snap["wr14"], bands_wr)
    snap["swing"] = {
        "state": swing.get("state"),
        "detail": swing.get("detail"),
        "last_low": swing.get("last_low"),
        "last_high": swing.get("last_high"),
        "run_min_30": _num(swing.get("run_min_30"), 3),
        "run_max_30": _num(swing.get("run_max_30"), 3),
        "rise_from_low": _num(swing.get("rise_from_low"), 3),
        "fall_from_high": _num(swing.get("fall_from_high"), 3),
        "new_low_made": swing.get("new_low_made"),
    }
    snap["wr_reversal"] = ind.get("wr_reversal", {})

    # 持仓
    shares = float(asset.get("shares") or 0)
    cost = asset.get("cost")
    snap["shares"] = shares
    snap["cost"] = _num(cost, 4)
    snap["market_value"] = _num(shares * close, 2) if close else None
    if cost and close:
        snap["pnl"] = _num((close - float(cost)) * shares, 2)
        snap["pnl_pct"] = _num((close / float(cost) - 1) * 100)

    if not ind.get("dates") or ind["n"] < 120:
        snap["warnings"].append(f"历史仅 {ind.get('n')} 根K线，长周期指标可能不准")
    return snap


def _technical_confirm(ind: dict) -> float | None:
    """止跌/趋势确认分（0-100）。刻意不包含任何"未来"信息。"""
    i = ind["n"] - 1
    if i < 3:
        return None
    pts = 0.0
    total = 0.0
    c = ind["close"][i]

    def add(cond, w):
        nonlocal pts, total
        total += w
        if cond:
            pts += w

    ma20 = (ind.get("ma", {}).get("ma20") or [None])[i]
    ma60 = (ind.get("ma", {}).get("ma60") or [None])[i]
    sl20 = ((ind.get("ma_slope") or {}).get("ma20") or [None])[i]
    hist = ((ind.get("macd") or {}).get("hist") or [None])[i]
    wr = (ind.get("wr14") or [None])[i]
    rev = ind.get("wr_reversal") or {}
    swing_states = ind.get("swing_states") or []
    state = swing_states[i] if swing_states else None

    add(bool(c and ma20 and c > ma20), 25)
    add(bool(ma20 and ma60 and ma20 > ma60), 15)
    add(bool(sl20 is not None and sl20 > 0), 15)
    add(bool(hist is not None and hist > 0), 15)
    add(bool(rev.get("confirmed_reversal")), 15)
    add(state in ("底部确认", "底部观察"), 15)
    add(bool(wr is not None and wr > -80), 10)
    return round(pts / total * 100, 1) if total else None


def etf_flags(asset: dict, ind: dict, snap: dict, cfg) -> dict:
    """追涨 / 抄底 / 超卖反转等硬性风控标记。"""
    rp1 = snap["relpos"].get("1y")
    rp2 = snap["relpos"].get("2y")
    r20 = snap["ret20"]
    b20 = snap["bias20"]
    wr = snap["wr14"]
    bands_r = cfg.get("classification.return20_pct", {})
    up = float(bands_r.get("up", 5.0))
    flat_down = float(bands_r.get("flat_down", -2.0))
    high_bias = float(cfg.get("classification.bias20_pct.high", 4.0))
    very_low_bias = float(cfg.get("classification.bias20_pct.very_low", -8.0))

    swing = snap["swing"]
    state = swing.get("state") or ""
    ma20 = snap["ma"].get("ma20")
    price = snap["price"]
    sl20 = ((ind.get("ma_slope") or {}).get("ma20") or [None])[-1]

    flags: dict = {}

    # ---- 高位强趋势：不要卖，但禁止追涨 ----
    chase = (
        (rp1 is not None and rp1 > 80) and (rp2 is not None and rp2 > 80)
        and (r20 is not None and r20 >= up)
        and (b20 is not None and b20 >= high_bias)
        and (wr is not None and wr > -20)
    )
    flags["high_strong_trend"] = bool(chase)
    flags["no_chase"] = bool(chase)

    # ---- 极度超跌但尚未止跌：WAIT，禁止抄底 ----
    new_low = bool(swing.get("new_low_made"))
    ma20_down = bool(sl20 is not None and sl20 < 0)
    bottom_unconfirmed = state not in ("底部确认", "底部观察")
    deep = (
        (rp1 is not None and rp1 < 20) and (rp2 is not None and rp2 < 20)
        and (r20 is not None and r20 <= flat_down)
        and (b20 is not None and b20 <= very_low_bias)
        and (wr is not None and wr < -80)
    )
    flags["deep_oversold"] = bool(deep)
    flags["not_stopped_falling"] = bool(new_low and ma20_down and bottom_unconfirmed)
    flags["no_bottom_fishing"] = bool(deep and (new_low or ma20_down) and bottom_unconfirmed)

    # ---- WR 超卖反转 ----
    rev = snap.get("wr_reversal") or {}
    flags["wr_reversal_confirmed"] = bool(rev.get("confirmed_reversal"))

    # ---- 明显下降趋势中的盲目抄底（E 级触发条件）----
    flags["downtrend"] = bool(
        (state in ("顶部确认", "波段中部") and new_low) or ma20_down
    )

    # ---- 决策影响 ----
    block = bool(flags["no_chase"] or flags["no_bottom_fishing"])
    flags["blocked"] = block
    if flags["no_chase"]:
        flags["block_reason"] = "高位强趋势：禁止追涨"
    elif flags["no_bottom_fishing"]:
        flags["block_reason"] = "极度超跌但尚未止跌：禁止抄底，继续等待"
    elif block:
        flags["block_reason"] = "技术形态未确认，暂不新增"
    else:
        flags["block_reason"] = ""
    return flags


def etf_scores(asset: dict, ind: dict, snap: dict, hist: dict, flags: dict,
               gap_pct: float | None, cfg) -> dict:
    """四个基础分 + BuyScore + EntryQuality + 等级。"""
    bands_rp = cfg.get("classification.relpos_bands", [20, 35, 65, 80])
    ts = scoring.trend_score(ind)
    ps = scoring.position_score(ind, bands_rp)
    ss = scoring.short_term_score(ind)
    sws = scoring.swing_score(ind)

    h60 = (hist.get(60) or {})
    prob60 = h60.get("win_rate_pct")
    exp60 = h60.get("expected_return_pct")
    payoff = h60.get("payoff_ratio")
    dd60 = h60.get("max_drawdown_pct")
    risk60 = min(100.0, abs(dd60) / 25.0 * 100.0) if dd60 is not None else None

    confirm = _technical_confirm(ind)

    bq = cfg.get("scores.buy_score", {})
    buy_factors = {
        "trend_quality": ts,
        "low_position": (100 - ps) if ps is not None else None,
        "short_term_oversold": (100 - ss) if ss is not None else None,
        "swing_bottom": sws,
        "hist_prob_60": _map(prob60, 30, 80),
        "expected_return_60": _map(exp60, -4, 10),
        "risk_control": (100 - risk60) if risk60 is not None else None,
        "portfolio_gap": _map(gap_pct, -8, 8) if gap_pct is not None else None,
    }
    buy = scoring.buy_score(buy_factors, bq) if any(
        v is not None for v in buy_factors.values()) else None
    # 风控标记直接压制买入质量分（只降不升）
    if buy is not None:
        if flags.get("no_chase"):
            buy = min(buy, 45.0)
        if flags.get("no_bottom_fishing"):
            buy = min(buy, 40.0)

    eq_w = cfg.get("scores.entry_quality", {})
    eq_factors = {
        "hist_prob_60": _map(prob60, 35, 75),
        "expected_return_60": _map(exp60, -3, 8),
        "payoff_ratio": _map(payoff, 0.5, 3.0),
        "risk_60": (100 - risk60) if risk60 is not None else None,
        "technical_confirm": confirm,
        "relative_position": (100 - ps) if ps is not None else None,
        "swing_state": sws,
    }
    eq = scoring.entry_quality(eq_factors, eq_w) if any(
        v is not None for v in eq_factors.values()) else None

    trend_state = scoring.classify_trend(ts)
    grade, grade_note = scoring.etf_grade(
        prob60, exp60, payoff, int(h60.get("samples") or 0), trend_state,
        cfg.get("scores.etf_grade", {}),
    )
    if grade not in ("样本不足",) and flags.get("blocked"):
        order = {"A": 4, "B": 3, "C": 2, "D": 1, "E": 0}
        cur = order.get(grade, 0)
        cap = 2 if flags.get("no_chase") else 1  # 追涨→最多C；未止跌→最多D
        if cur > cap:
            grade = "C" if cap == 2 else "D"
            grade_note = flags.get("block_reason") + "，等级已下调"

    return {
        "trend": _num(ts, 1), "position": _num(ps, 1),
        "shortterm": _num(ss, 1), "swing": _num(sws, 1),
        "trend_class": trend_state,
        "shortterm_class": scoring.classify_shortterm(ss, snap.get("atr_pct")),
        "buy_score": _num(buy, 1),
        "entry_quality": _num(eq, 1),
        "buy_factors": {k: _num(v, 1) for k, v in buy_factors.items()},
        "entry_factors": {k: _num(v, 1) for k, v in eq_factors.items()},
        "technical_confirm": confirm,
        "grade": grade,
        "grade_note": grade_note,
        "prob60": prob60, "exp60": exp60, "payoff": payoff,
        "dd60": dd60, "risk60": _num(risk60, 1),
        "flags": flags,
    }


# ==========================================================================
# 组合层
# ==========================================================================
def market_state(risk_score: float | None) -> str:
    if risk_score is None:
        return "暂无数据"
    if risk_score < 30:
        return "Risk On"
    if risk_score < 50:
        return "Normal"
    if risk_score < 70:
        return "Risk Off"
    return "Crisis"


def portfolio_risk_score(pstats: dict, corr: dict, p_hist: dict,
                         stock_trend_avg: float | None) -> dict:
    """组合风险 0-100（越高越危险）。"""
    parts: list[tuple[str, float | None, float]] = []

    dd1 = ((pstats.get("dd_windows") or {}).get("1y") or {}).get("current_drawdown_pct")
    parts.append(("组合当前回撤", _map(dd1, 0, -22) if dd1 is not None else None, 0.25))

    vol = pstats.get("vol20_annual_pct")
    parts.append(("组合波动率", _map(vol, 4, 26) if vol is not None else None, 0.20))

    parts.append(("股票趋势走弱",
                  100 - stock_trend_avg if stock_trend_avg is not None else None, 0.20))

    avg_corr = ((corr.get("matrices") or {}).get("60d") or {}).get("avg_corr")
    parts.append(("资产相关性", _map(avg_corr, 0.05, 0.75) if avg_corr is not None else None, 0.15))

    prob60 = (p_hist.get(60) or {}).get("win_rate_pct")
    parts.append(("组合60日胜率", 100 - prob60 if prob60 is not None else None, 0.20))

    tot = sum(w for _, v, w in parts if v is not None)
    score = sum(v * w for _, v, w in parts if v is not None) / tot if tot else None
    return {
        "score": _num(score, 1),
        "components": [
            {"name": n, "value": _num(v, 1), "weight": w} for n, v, w in parts
        ],
        "state": market_state(score),
    }


def portfolio_quality(pstats: dict, corr: dict, p_hist: dict,
                      weights_class: dict, targets: dict, cfg) -> dict:
    w = cfg.get("scores.portfolio_quality", {})
    prob60 = (p_hist.get(60) or {}).get("win_rate_pct")
    prob120 = (p_hist.get(120) or {}).get("win_rate_pct")
    exp60 = (p_hist.get(60) or {}).get("expected_return_pct")

    dd1 = ((pstats.get("dd_windows") or {}).get("1y") or {}).get("max_drawdown_pct")
    vol = pstats.get("annual_vol_pct")
    div = corr.get("diversification_score")

    # 仓位合理性：大类实际 vs 战术目标 的偏离 + 黄金是否超限
    dev = 0.0
    n = 0
    for k, tgt in (targets or {}).items():
        cur = weights_class.get(k)
        if cur is None or tgt is None:
            continue
        dev += abs(cur - tgt)
        n += 1
    avg_dev = (dev / n * 100) if n else None
    gold_over = (weights_class.get("gold") or 0) > float(cfg.get("portfolio.gold_max", 0.05)) + 1e-9
    pos_reason = None
    if avg_dev is not None:
        pos_reason = _map(avg_dev, 0, 15)
    if gold_over:
        pos_reason = min(pos_reason if pos_reason is not None else 50, 25.0)

    factors = {
        "prob_60": _map(prob60, 40, 75),
        "prob_120": _map(prob120, 40, 80),
        "expected_return_60": _map(exp60, -3, 8),
        "max_drawdown": _map(dd1, -25, -3) if dd1 is not None else None,
        "volatility": _map(vol, 30, 5) if vol is not None else None,
        "diversification": div,
        "position_reasonableness": pos_reason,
    }
    tot = sum(float(w.get(k, 0)) for k, v in factors.items() if v is not None)
    score = (sum(float(w.get(k, 0)) * v for k, v in factors.items() if v is not None) / tot
             if tot else None)
    return {
        "score": _num(score, 1),
        "components": [{"name": k, "value": _num(v, 1), "weight": w.get(k)}
                       for k, v in factors.items()],
        "avg_class_deviation_pct": _num(avg_dev, 1),
        "gold_over_cap": bool(gold_over),
    }


def portfolio_entry_signal(p_hist: dict, risk: dict, quality: dict,
                           etf_rows: list[dict], weights_class: dict,
                           targets: dict, cfg) -> dict:
    """组合买入门槛 S/A/B/C/D/E + 分批计划。"""
    gates = cfg.get("scores.entry_gate", {})
    prob60 = (p_hist.get(60) or {}).get("win_rate_pct")
    prob120 = (p_hist.get(120) or {}).get("win_rate_pct")
    exp60 = (p_hist.get(60) or {}).get("expected_return_pct")
    samples60 = int((p_hist.get(60) or {}).get("samples") or 0)

    grade = "C"
    reason: list[str] = []
    veto = None

    if prob60 is None or samples60 < 30:
        grade = "C"
        reason.append("组合历史相似样本不足，无法给出高等级买入信号")
    elif prob60 >= gates.get("S", {}).get("prob_60", 70) and (exp60 or -99) > gates.get("S", {}).get("exp_ret_60", 4):
        has_quality = any(r["scores"].get("grade") in ("A", "B") and not r["scores"]["flags"].get("blocked")
                          for r in etf_rows if r["asset"].get("asset_class") == "stock")
        if has_quality:
            grade = "S"
        else:
            grade = "A"
            reason.append("组合统计达 S 级，但暂无个股级高质量买点，降为 A")
    elif prob60 >= gates.get("A", {}).get("prob_60", 65) and (exp60 or -99) > gates.get("A", {}).get("exp_ret_60", 2):
        grade = "A"
    elif prob60 >= gates.get("B", {}).get("prob_60", 58) and (exp60 or 0) > gates.get("B", {}).get("exp_ret_60", 0):
        grade = "B"
    elif prob60 >= gates.get("C", {}).get("prob_60", 50):
        grade = "C"
    else:
        grade = "D"
        reason.append("组合60日历史盈利概率低于50%")

    if grade in ("D",) and (exp60 or 0) <= 0 and (risk.get("score") or 0) >= 60:
        grade = "E"
        reason.append("组合期望收益为负且风险评分偏高")

    # 否决项（顺序即优先级）
    gold_cap = float(cfg.get("portfolio.gold_max", 0.05))
    gold_now = weights_class.get("gold") or 0.0
    risk_now = (weights_class.get("stock") or 0.0) + gold_now
    risk_tgt = (targets.get("stock") or 0.0) + (targets.get("gold") or 0.0)

    if risk.get("state") == "Crisis":
        veto = "组合进入 Crisis 状态"
    elif (risk.get("score") or 0) >= 70:
        veto = "组合风险评分过高"
    elif gold_now > gold_cap + 1e-9:
        veto = (f"黄金已超配（当前 {gold_now * 100:.1f}% > 硬上限 {gold_cap * 100:.0f}%），"
                "必须先再平衡，不能继续增加风险资产")
    elif risk_now > risk_tgt + 0.05:
        veto = (f"风险资产已超配（当前 {risk_now * 100:.1f}% > 目标 {risk_tgt * 100:.0f}%），"
                "应先再平衡而不是继续买入")
    stock_rows = [r for r in etf_rows if r["asset"].get("asset_class") == "stock"]
    chase_all = bool(stock_rows) and all(
        r["scores"]["flags"].get("no_chase") for r in stock_rows)
    if veto is None and chase_all and (weights_class.get("stock") or 0) >= 0.33:
        veto = "股票资产整体处于高位强趋势，禁止追涨"

    if veto:
        order = {"S": 4, "A": 3, "B": 2, "C": 1, "D": 0, "E": -1}
        cap = 1 if veto != "组合进入 Crisis 状态" else 0
        if order.get(grade, 0) > cap:
            grade = "C" if cap == 1 else "D"
        reason.append(f"已被风控否决：{veto}")

    batch_total = int(cfg.get("portfolio.batch_count", 4))
    allowed = {"S": batch_total, "A": 3, "B": 2, "C": 0, "D": 0, "E": 0}.get(grade, 0)
    batch_pct = round(100.0 / batch_total, 1) if batch_total else 25.0

    action = {
        "S": "分批买入", "A": "分批买入", "B": "小仓试探",
        "C": "WAIT", "D": "WAIT", "E": "降低风险",
    }.get(grade, "WAIT")

    deploy = None
    stock_gap = None
    if targets and weights_class:
        gaps = {k: (targets.get(k, 0) or 0) - (weights_class.get(k) or 0)
                for k in set(targets) | set(weights_class)}
        deploy = {k: _num(v * 100, 2) for k, v in gaps.items()}
        stock_gap = gaps.get("stock")

    plan = []
    if allowed > 0:
        for i in range(1, batch_total + 1):
            plan.append({
                "batch": i,
                "pct_of_risk_capital": batch_pct,
                "status": "本次可执行" if i <= allowed else "条件改善后再执行",
            })

    return {
        "grade": grade,
        "action": action,
        "allowed_batches": allowed,
        "total_batches": batch_total,
        "batch_pct": batch_pct,
        "batches": plan,
        "prob60": prob60,
        "prob120": prob120,
        "exp60": exp60,
        "reasons": reason,
        "veto": veto,
        "class_gaps_pct": deploy,
        "stock_gap_pct": _num(stock_gap * 100, 2) if stock_gap is not None else None,
    }


def rebalance_plan(etf_rows: list[dict], weights_class: dict, targets: dict,
                   risk: dict, cfg) -> dict:
    watch = float(cfg.get("portfolio.rebalance_watch_pct", 3.0))
    prio = float(cfg.get("portfolio.rebalance_priority_pct", 5.0))

    items = []
    for r in etf_rows:
        a = r["asset"]
        cur = r.get("weight_pct")
        tgt = (a.get("target_weight") or 0) * 100
        if cur is None:
            continue
        dev = cur - tgt
        if abs(dev) < watch:
            continue
        s = r["scores"]
        swing = (r["snapshot"].get("swing") or {}).get("state")
        # 不能只因涨多了就机械卖：趋势与波段顶底给缓冲
        keep = (s.get("trend_class") in ("强势上涨", "上涨")
                and swing not in ("顶部确认",))
        if dev > 0:
            if abs(dev) >= prio:
                action = "减仓（偏高优先级）" if not keep else "观察（趋势仍强，暂不减）"
            else:
                action = "再平衡观察" if not keep else "持有（趋势仍强）"
        else:
            if abs(dev) >= prio:
                action = "加仓（低配）" if not s["flags"].get("blocked") else "暂缓（未确认止跌）"
            else:
                action = "再平衡观察" if not s["flags"].get("blocked") else "暂缓（未确认止跌）"
        items.append({
            "id": a.get("id"), "name": a.get("name"),
            "asset_class": a.get("asset_class"),
            "current_pct": _num(cur), "target_pct": _num(tgt),
            "deviation_pct": _num(dev),
            "level": "优先" if abs(dev) >= prio else "观察",
            "action": action,
        })

    # 黄金硬上限
    gold_cur = (weights_class.get("gold") or 0) * 100
    gold_cap = float(cfg.get("portfolio.gold_max", 0.05)) * 100
    gold_state = "正常"
    if gold_cur > gold_cap + 1e-6:
        gold_state = "黄金超配"

    need = bool([i for i in items if i["level"] == "优先"]) or gold_state != "正常"
    return {
        "items": items,
        "need": need,
        "watch_threshold": watch,
        "priority_threshold": prio,
        "gold_current_pct": _num(gold_cur, 2),
        "gold_cap_pct": _num(gold_cap, 2),
        "gold_state": gold_state,
        "risk_state": risk.get("state"),
    }


def gold_position_control(cfg, weights_class: dict, gold_row: dict | None,
                          radar: dict, macro_score: dict | None = None) -> dict:
    cap = float(cfg.get("portfolio.gold_max", 0.05))
    cur = weights_class.get("gold")
    out = {
        "current_pct": _num((cur or 0) * 100, 2) if cur is not None else None,
        "cap_pct": _num(cap * 100, 2),
        "radar": f"{radar.get('hit')}/{radar.get('total')}",
        "radar_status": radar.get("status"),
        "radar_level": radar.get("level"),
        "macro_score": (macro_score or {}).get("score"),
        "macro_grade": (macro_score or {}).get("grade_label"),
        "over_cap": bool(cur is not None and cur > cap + 1e-9),
    }
    if out["over_cap"]:
        out["advice"] = "黄金已超配：禁止增加黄金，进入再平衡观察"
    elif cur is not None and cur >= cap - 0.005:
        out["advice"] = "黄金已达上限：只持有，不再增加（即使底部雷达满格）"
    elif (radar.get("level") or 0) >= 2:
        out["advice"] = f"黄金底部信号进入『{radar.get('status')}』，但仓位仍受 5% 硬上限约束"
    else:
        out["advice"] = "黄金按目标权重参与，主要承担对冲与危机保护"
    return out


def final_decision(entry: dict, quality: dict, risk: dict, etf_rows: list[dict],
                   rebalance: dict, gold: dict, waiting: dict) -> dict:
    """今日最终操作 + 最多 5 条核心理由。"""
    ranked = sorted(
        [r for r in etf_rows if r.get("scores", {}).get("buy_score") is not None],
        key=lambda r: -(r["scores"]["buy_score"] or 0),
    )
    top = [r for r in ranked if not r["scores"]["flags"].get("blocked")][:3]
    chase = [r for r in etf_rows if r["scores"]["flags"].get("no_chase")]
    no_bottom = [r for r in etf_rows if r["scores"]["flags"].get("no_bottom_fishing")]

    action = entry["action"]
    if rebalance.get("gold_state") == "黄金超配":
        action = "再平衡"
    elif rebalance.get("need") and entry["grade"] in ("S", "A", "B"):
        action = "再平衡" if any(i["action"].startswith("减仓") for i in rebalance["items"]) else action

    lines: list[str] = []
    st = risk.get("state")
    lines.append(
        f"组合处于 {st} 状态，风险评分 {risk.get('score')}/100，"
        f"Portfolio Quality {quality.get('score')}/100。")
    if entry.get("prob60") is not None:
        lines.append(
            f"未来60日历史盈利概率 {entry['prob60']}%，期望收益 {entry.get('exp60')}%，"
            f"信号等级 {entry['grade']}（{entry['action']}）。")
    if top:
        lines.append("优先关注：" + "、".join(
            f"{r['asset'].get('name')}（买入质量 {r['scores']['grade']}，"
            f"Buy Score {r['scores']['buy_score']}）"
            for r in top) + "。")
    if chase:
        lines.append("不要追涨：" + "、".join(r["asset"].get("name") for r in chase) + "。")
    if no_bottom:
        lines.append("不建议抄底：" + "、".join(r["asset"].get("name") for r in no_bottom)
                     + "（极度超跌但尚未止跌）。")
    if entry.get("class_gaps_pct"):
        gaps = entry["class_gaps_pct"]
        lines.append("大类缺口（目标-当前）：" + "、".join(
            f"{k} {v:+.1f}%" for k, v in gaps.items()) + "。")
    if waiting.get("wait_ratio_pct") is not None:
        lines.append(f"风险资产等待比例：{waiting['wait_ratio_pct']}%"
                     f"（{waiting.get('note')}）。")
    if rebalance.get("need"):
        lines.append("再平衡：需要（" + "；".join(
            f"{i['name']} {i['action']}" for i in rebalance["items"][:3]) + "）。")
    lines.append(gold.get("advice", ""))

    return {
        "action": action,
        "grade": entry["grade"],
        "lines": [x for x in lines if x][:5],
        "top_picks": [{"id": r["asset"].get("id"), "name": r["asset"].get("name"),
                       "buy_score": r["scores"]["buy_score"],
                       "grade": r["scores"]["grade"],
                       "reason": _pick_reason(r)} for r in top],
        "no_chase": [r["asset"].get("name") for r in chase],
        "no_bottom_fishing": [r["asset"].get("name") for r in no_bottom],
    }


def _pick_reason(row: dict) -> str:
    s = row["scores"]
    bits = []
    if s.get("prob60") is not None:
        bits.append(f"60日胜率{s['prob60']}%")
    if s.get("exp60") is not None:
        bits.append(f"期望{s['exp60']:+}%")
    if s.get("payoff") is not None:
        bits.append(f"盈亏比{s['payoff']}")
    if s.get("technical_confirm") is not None:
        bits.append(f"技术确认{s['technical_confirm']}")
    if s.get("swing") is not None:
        bits.append(f"波段分{s['swing']}")
    return "，".join(bits) or "历史样本不足"


def waiting_capital(weights_class: dict, targets: dict, entry: dict) -> dict:
    """风险资产等待比例：目标风险资产中"本次不投入"的比例。"""
    if not weights_class or not targets:
        return {"wait_ratio_pct": None, "note": "暂无数据"}
    risk_now = (weights_class.get("stock", 0) or 0) + (weights_class.get("gold", 0) or 0)
    risk_tgt = (targets.get("stock", 0) or 0) + (targets.get("gold", 0) or 0)
    allowed = entry.get("allowed_batches", 0)
    total = max(1, entry.get("total_batches", 4))
    deployed_share = allowed / total
    gap = max(0.0, risk_tgt - risk_now)
    undeployed = gap * (1 - deployed_share)
    ratio = undeployed / risk_tgt * 100 if risk_tgt > 0 else None
    over = risk_now > risk_tgt + 1e-9
    if over:
        note = "风险资产已超过目标，应通过再平衡降低，而不是等待"
    elif entry["grade"] in ("C", "D", "E"):
        note = "当前不适合增加风险资产，缺口全部以债券/防守仓形式等待"
    elif entry["grade"] == "B":
        note = "仅小仓试探，其余缺口继续等待"
    else:
        note = "按分批计划逐步投入，未到位部分继续等待"
    return {
        "wait_ratio_pct": _num(ratio, 1),
        "risk_current_pct": _num(risk_now * 100, 2),
        "risk_target_pct": _num(risk_tgt * 100, 2),
        "gap_pct": _num(gap * 100, 2),
        "note": note,
    }
