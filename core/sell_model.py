"""SELL / REDUCE 模型 —— 独立于 BUY 的卖出验证框架。

原则
----
1. 卖出**不能**因为"涨太多 / RSI 高 / 离高点近"这类单一指标直接触发；
   每个候选必须给出**≥2 个互相独立的支持信号**（single_signal=False），
   并且必须通过 OOS 验证（"减仓/卖出后的组合 vs 继续持有"的增量 ≥ 0）；
2. 对每个候选卖出动作，在 20 / 60 / 120 日上分别计算：
      delta_return / delta_max_drawdown / delta_volatility /
      delta_sharpe / delta_sortino / delta_portfolio_quality
   只有证明"减仓优于继续持有"，才成为有效 SELL/REDUCE 候选；
3. 支持的卖出类型（每种都单独做 OOS 验证，不为每种情况随意加规则）：
      ① 再平衡减仓  ② 风险恶化减仓  ③ 组合风险贡献过高减仓
      ④ 趋势破坏减仓 ⑤ 极端风险事件减仓
4. 黄金卖出另有专门处理（不许"一超 5% 就全部卖完"）：
   见 `gold_reduce_ladder`。
"""
from __future__ import annotations

import math

from . import trade_value as tv
from . import cost_model as cm

TRADING_DAYS = 252

SELL_KINDS = ("rebalance", "risk_deterioration", "risk_contribution",
              "trend_break", "extreme_risk")

_MIN_INDEPENDENT_SIGNALS = 2


def _mean(xs):
    ys = [x for x in xs if x is not None]
    return sum(ys) / len(ys) if ys else None


def _std(xs):
    ys = [x for x in xs if x is not None]
    if len(ys) < 2:
        return None
    m = _mean(ys)
    return math.sqrt(sum((x - m) ** 2 for x in ys) / (len(ys) - 1))


def asset_features(panel, aid: str, i: int) -> dict:
    """只用 ≤ i 的数据算资产特征（无未来函数）。"""
    close = panel.close.get(aid) or []
    if i >= len(close) or close[i] is None:
        return {"available": False}
    px = float(close[i])

    def ma(w):
        seg = [c for c in close[max(0, i - w + 1):i + 1] if c is not None]
        return (sum(seg) / len(seg)) if len(seg) >= min(w, 20) else None

    ma60 = ma(60)
    ma20_prev = ma(20)
    ma60_prev = None
    if i - 5 >= 0:
        seg = [c for c in close[max(0, i - 5 - 60 + 1):i - 5 + 1] if c is not None]
        ma60_prev = (sum(seg) / len(seg)) if len(seg) >= 40 else None
    seg1y = [c for c in close[max(0, i - 251):i + 1] if c is not None]
    hi1y = max(seg1y) if seg1y else None
    lo1y = min(seg1y) if seg1y else None
    rets = [r for r in (panel.ret.get(aid) or [])[max(0, i - 59):i + 1] if r is not None]
    vol60 = (_std(rets) or 0.0) * math.sqrt(TRADING_DAYS)
    return {
        "available": True,
        "price": px,
        "ma20": ma20_prev, "ma60": ma60, "ma60_prev": ma60_prev,
        "above_ma60": (ma60 is not None and px > ma60),
        "ma60_slope_down": (ma60 is not None and ma60_prev is not None
                            and ma60 < ma60_prev),
        "dd_from_1y_high_pct": ((px / hi1y - 1.0) * 100.0) if hi1y else None,
        "relpos_1y": (((px - lo1y) / (hi1y - lo1y)) * 100.0
                      if (hi1y and lo1y and hi1y > lo1y) else None),
        "vol60": vol60,
    }


def _risk_contribution(panel, weights: dict, i: int) -> dict:
    """粗略风险贡献：w_i·σ_i / Σ w_j·σ_j（用 60 日波动近似，避免协方差抖动）。"""
    out: dict[str, float] = {}
    num: dict[str, float] = {}
    for a in panel.assets:
        w = float(weights.get(a, 0.0))
        if w <= 0:
            continue
        f = asset_features(panel, a, i)
        vol = f.get("vol60") if f.get("available") else None
        if vol is None:
            continue
        num[a] = w * vol
    tot = sum(num.values())
    if tot <= 0:
        return out
    for a, v in num.items():
        out[a] = v / tot
    return out


def gold_reduce_ladder(weight_now: float, weight_cap: float,
                       levels=(0.10, 0.08, 0.05)) -> list[dict]:
    """黄金超配时给出"降到 X%"的阶梯方案（选择改善组合同时卖出最少的方案）。"""
    out = []
    for lv in levels:
        target = min(float(lv), float(weight_cap))
        if weight_now > target + 1e-9:
            out.append({"target_weight": round(target, 4),
                        "reduce_to_pct": round(target * 100, 2),
                        "sell_fraction": round(1.0 - target / weight_now, 4)})
    return out


def generate_sell_candidates(panel, i: int, weights: dict[str, float],
                             targets: dict[str, float], *,
                             gold_cap: float = 0.05,
                             rebalance_watch_pct: float = 3.0) -> list[dict]:
    """生成 5 类卖出候选（含 ≥2 信号要求与 single_signal 标记）。"""
    cands: list[dict] = []
    total_w = sum(float(v) for v in weights.values()) or 1.0
    # ① 再平衡减仓：任一资产偏离目标超过 watch 阈值
    for a in panel.assets:
        w = float(weights.get(a, 0.0)) / total_w
        tg = float(targets.get(a, 0.0))
        if tg <= 0:
            continue
        dev = (w - tg) * 100.0
        if dev > rebalance_watch_pct:
            after = dict(weights)
            after[a] = float(weights.get(a, 0.0)) * (tg / w) if w > 0 else 0.0
            cands.append({"kind": "rebalance", "asset": a, "after": after,
                          "signals": [f"权重偏离目标 +{dev:.1f}pp"],
                          "single_signal": True,
                          "note": "纯偏离触发 → 需叠加信号或再平衡验证"})
    # ② 风险恶化减仓：组合 60 日波动处于高位且趋势转弱
    rets = [r for r in panel.ret.get(panel.assets[0], [])[:i + 1] if r is not None] \
        if panel.assets else []
    port_vol = (_std([r for r in
                      [tv.port_ret_at(panel, weights, k) for k in range(max(1, i - 59), i + 1)]
                      if r is not None]) or 0.0) * math.sqrt(TRADING_DAYS)
    pct_above = _pct_above_ma60(panel, weights, i)
    if pct_above is not None and pct_above <= 0.34 and port_vol > 0.15:
        after = {a: float(v) * 0.66 for a, v in weights.items()
                 if _is_risk(a, panel, targets)}
        cands.append({"kind": "risk_deterioration", "asset": "*stock*", "after": after,
                      "signals": [f"仅 {pct_above * 100:.0f}% 风险资产站上 MA60",
                                  f"组合 60 日波动 {port_vol * 100:.1f}% 偏高"],
                      "single_signal": False,
                      "note": "趋势+波动双确认"})
    # ③ 风险贡献过高减仓
    rc = _risk_contribution(panel, weights, i)
    for a, share in rc.items():
        w = float(weights.get(a, 0.0)) / total_w
        if share > 2.0 * w and share > 0.35:
            after = dict(weights)
            after[a] = float(weights.get(a, 0.0)) * 0.8
            f = asset_features(panel, a, i)
            sig = [f"风险贡献 {share * 100:.0f}% 远超权重 {w * 100:.0f}%"]
            if f.get("vol60") and f["vol60"] > 0.20:
                sig.append(f"自身波动 {f['vol60'] * 100:.0f}% 偏高")
            if not f.get("above_ma60"):
                sig.append("价格已跌破 MA60")
            cands.append({"kind": "risk_contribution", "asset": a, "after": after,
                          "signals": sig, "single_signal": len(sig) < _MIN_INDEPENDENT_SIGNALS,
                          "note": "风险贡献过高"})
    # ④ 趋势破坏减仓
    for a in panel.assets:
        f = asset_features(panel, a, i)
        if not f.get("available"):
            continue
        if (not f.get("above_ma60")) and f.get("ma60_slope_down"):
            after = dict(weights)
            after[a] = float(weights.get(a, 0.0)) * 0.5
            cands.append({"kind": "trend_break", "asset": a, "after": after,
                          "signals": ["价格跌破 MA60", "MA60 斜率向下"],
                          "single_signal": False, "note": "趋势破坏"})
    # ⑤ 极端风险事件减仓
    dd = None
    nav = panel.nav[:i + 1]
    if nav:
        peak = max(nav[-252:]) if len(nav) >= 20 else max(nav)
        dd = (nav[-1] / peak - 1.0) * 100.0 if peak else None
    if dd is not None and dd <= -12.0 and port_vol > 0.18:
        after = {a: float(v) * 0.5 for a, v in weights.items()
                 if _is_risk(a, panel, targets)}
        cands.append({"kind": "extreme_risk", "asset": "*stock*", "after": after,
                      "signals": [f"组合回撤 {dd:.1f}%", f"波动 {port_vol * 100:.1f}% 处于危机区"],
                      "single_signal": False, "note": "极端风险事件"})
    return cands


def _is_risk(aid: str, panel, targets: dict) -> bool:
    tgt = float(targets.get(aid, 0.0))
    return tgt > 0 and panel.weights.get(aid, 0) > 0 and aid in panel.assets \
        and (panel.weights.get(aid, 0.0) >= 0.08)


def _pct_above_ma60(panel, weights: dict, i: int):
    stock = [a for a in panel.assets if panel.weights.get(a, 0.0) >= 0.08]
    if not stock:
        return None
    ok = 0
    tot = 0
    for a in stock:
        f = asset_features(panel, a, i)
        if f.get("available") and f.get("above_ma60") is not None:
            tot += 1
            ok += 1 if f.get("above_ma60") else 0
    return (ok / tot) if tot else None


def evaluate_sell(panel, t: int, horizons, hold_weights: dict,
                  sell_weights: dict, *, tier: str = "BASE") -> dict:
    """对"减仓/卖出"相对"继续持有（HOLD）"做多周期增量评估。"""
    cost = cm.trade_cost(hold_weights, sell_weights, tier)
    out = {"kind": None, "cost_frac": round(cost, 6), "horizons": {}}
    for h in horizons:
        out["horizons"][int(h)] = tv.evaluate_trade(
            panel, t, int(h), hold_weights, sell_weights, cost=cost)
    return out


def valid_sell_candidate(cand: dict) -> bool:
    """单指标触发一律无效（除非已通过 OOS 验证标注 os_verified）。"""
    if cand.get("os_verified"):
        return True
    return not bool(cand.get("single_signal")) and \
        len([s for s in (cand.get("signals") or []) if s]) >= _MIN_INDEPENDENT_SIGNALS
