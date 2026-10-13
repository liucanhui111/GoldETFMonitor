"""交易价值引擎（Trade Value）——本轮策略重构的核心。

唯一目标
--------
把"预测未来涨不涨"升级为：

    **这笔交易，相对【什么都不做 / WAIT / 继续持有】，是否真的改善整个组合的结果？**

    Trade Value = 执行交易后的组合结果 − 不执行交易的组合结果

BUY / WAIT / REDUCE / REBALANCE 全部用同一思想衡量：
    · 交易后组合 = 把组合权重换成 w_after，再按真实价格前推 h 个交易日（T+1 开盘执行、扣成本）；
    · 不交易组合 = 保持 w_before 不动；
    · 所有 delta 均为 "交易后 − 不交易"，**不是 ETF 自己涨不涨**。

纪律
----
1. 无未来函数：t 决策只用 ≤ t 的数据；前推收益取自 t+1 起；
2. 成本按换手率一次性计入 delta_transaction_cost；
3. 质量分用**连续有界映射**，避免"Quality Score 饱和"导致的虚假排序；
4. 数据缺失一律 None，绝不填 0 冒充。
"""
from __future__ import annotations

import math

TRADING_DAYS = 252


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------
def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _map01(v, lo: float, hi: float):
    """把 v 线性映射到 0~1（超界截断）。v 为 None → None。"""
    if v is None:
        return None
    if hi == lo:
        return 0.5
    return _clamp((float(v) - lo) / (hi - lo), 0.0, 1.0)


def _mean(xs):
    ys = [float(x) for x in xs if x is not None]
    return sum(ys) / len(ys) if ys else 0.0


def _std(xs):
    ys = [float(x) for x in xs if x is not None]
    if len(ys) < 2:
        return 0.0
    m = _mean(ys)
    return math.sqrt(sum((x - m) ** 2 for x in ys) / (len(ys) - 1))


# --------------------------------------------------------------------------
# 组合前推
# --------------------------------------------------------------------------
def port_ret_at(panel, weights: dict[str, float], i: int):
    """第 i 日、给定权重下的组合日收益（权重在当日有效资产上归一）。"""
    act = panel.active[i] if i < len(panel.active) else []
    tot = 0.0
    acc = 0.0
    for a in act:
        w = float(weights.get(a, 0.0))
        if w <= 0:
            continue
        r = panel.ret[a][i]
        if r is None:
            continue
        tot += w
        acc += w * r
    return (acc / tot) if tot > 0 else None


def forward_nav(panel, weights: dict[str, float], t: int, h: int,
                *, cost: float = 0.0) -> list[float]:
    """从 t 起（T+1 执行）前推 h 日、给定权重的组合净值序列（起点 1.0）。

    cost 为一次性成本（占市值比例），在第一步扣除。
    """
    n = len(panel.nav)
    end = min(t + h, n - 1)
    nav = [1.0]
    first = True
    for i in range(t + 1, end + 1):
        r = port_ret_at(panel, weights, i)
        step = (1.0 + (r or 0.0))
        if first:
            step *= (1.0 - float(cost))
            first = False
        nav.append(nav[-1] * step)
    return nav


def max_drawdown(nav: list[float]) -> float:
    peak = nav[0] if nav else 0.0
    mdd = 0.0
    for v in nav:
        peak = max(peak, v)
        if peak:
            mdd = min(mdd, v / peak - 1.0)
    return mdd


def stats_from_nav(nav: list[float], horizon: int, rf: float = 0.02) -> dict:
    """由前推净值序列算出风险收益统计（年化）。"""
    if len(nav) < 2 or horizon <= 0:
        return {"cum_return": None, "max_drawdown": None, "volatility": None,
                "sharpe": None, "sortino": None, "quality": None}
    cum = nav[-1] / nav[0] - 1.0
    rets = [nav[i] / nav[i - 1] - 1.0 for i in range(1, len(nav))]
    vol = _std(rets) * math.sqrt(TRADING_DAYS)
    try:
        ann = (1.0 + cum) ** (TRADING_DAYS / float(horizon)) - 1.0
    except (OverflowError, ValueError):
        ann = cum * (TRADING_DAYS / float(horizon))
    sharpe = ((ann - rf) / vol) if vol > 1e-9 else None
    neg = [r for r in rets if r < 0]
    dvol = _std(neg) * math.sqrt(TRADING_DAYS) if neg else 0.0
    sortino = ((ann - rf) / dvol) if dvol > 1e-9 else None
    mdd = max_drawdown(nav)
    # 连续有界质量分（0~100）：不做硬阈值，避免饱和导致"金额差一点就排序大变"
    q = 100.0 * (
        0.35 * (_map01(ann, -0.15, 0.25) or 0.0)
        + 0.25 * (_map01(mdd, -0.25, -0.02) or 0.0)
        + 0.20 * (1.0 - (_map01(vol, 0.05, 0.30) or 0.0))
        + 0.20 * (_map01(sharpe, -1.0, 2.0) or 0.0)
    )
    return {
        "cum_return": round(cum * 100, 4),
        "max_drawdown": round(mdd * 100, 4),
        "volatility": round(vol * 100, 4),
        "sharpe": round(sharpe, 4) if sharpe is not None else None,
        "sortino": round(sortino, 4) if sortino is not None else None,
        "quality": round(q, 3),
    }


# --------------------------------------------------------------------------
# 核心：一笔交易的增量价值
# --------------------------------------------------------------------------
def evaluate_trade(panel, t: int, horizon: int,
                   weights_before: dict[str, float],
                   weights_after: dict[str, float],
                   *, cost: float = 0.0, rf: float = 0.02) -> dict:
    """计算"执行 weights_after 的交易"相对"保持 weights_before（WAIT）"的增量价值。

    返回全部 delta 字段（交易后 − 不交易）。所有字段可为 None（数据不足）。
    """
    n = len(panel.nav)
    if t + horizon > n - 1:
        return {"ok": False, "reason": "forward window unavailable",
                "horizon": horizon, "t": t}
    wait_nav = forward_nav(panel, weights_before, t, horizon, cost=0.0)
    aft_net = forward_nav(panel, weights_after, t, horizon, cost=cost)
    aft_gross = forward_nav(panel, weights_after, t, horizon, cost=0.0)
    a = stats_from_nav(aft_net, horizon, rf)
    g = stats_from_nav(aft_gross, horizon, rf)
    w = stats_from_nav(wait_nav, horizon, rf)
    if a["cum_return"] is None or w["cum_return"] is None:
        return {"ok": False, "reason": "insufficient data", "horizon": horizon, "t": t}

    def d(x, k):
        if x.get(k) is None or w.get(k) is None:
            return None
        return round(float(x[k]) - float(w[k]), 4)

    # delta_return 为**净增量（已在净值里扣除成本）**；gross 为不扣成本的对照。
    delta_return = d(a, "cum_return")
    return {
        "ok": True,
        "t": t, "horizon": horizon,
        "after": a, "after_gross": g, "wait": w,
        "delta_return": delta_return,                       # 净（含成本）
        "delta_return_gross": d(g, "cum_return"),           # 毛（不含成本）
        "delta_return_net": delta_return,                   # 同义字段，明确"已扣成本"
        "delta_max_drawdown": d(a, "max_drawdown"),
        "delta_volatility": d(a, "volatility"),
        "delta_sharpe": d(a, "sharpe"),
        "delta_sortino": d(a, "sortino"),
        "delta_portfolio_quality": d(a, "quality"),
        "delta_transaction_cost": round(-cost * 100, 4),    # pp（成本为负贡献）
        "cost_frac": round(cost, 6),
        "beats_wait": (delta_return is not None and delta_return > 0),
        "incremental_alpha": delta_return,                  # 增量 Alpha vs WAIT（pp，已扣成本）
        "incremental_risk_adjusted": (
            round(delta_return / max(1e-6, abs(w["volatility"])), 4)
            if delta_return is not None and w["volatility"] else None),
    }


def multi_horizon(panel, t: int, horizons, weights_before, weights_after, *,
                  cost: float = 0.0, rf: float = 0.02) -> dict:
    """对 20/60/120 等多个周期同时评估一笔交易。"""
    out: dict = {}
    for h in horizons:
        out[int(h)] = evaluate_trade(panel, t, int(h), weights_before,
                                     weights_after, cost=cost, rf=rf)
    return out


# --------------------------------------------------------------------------
# 聚合（把多个事件的增量汇总成统计量）
# --------------------------------------------------------------------------
def aggregate_deltas(rows: list[dict], key: str = "delta_return") -> dict:
    """把多个事件的某个增量字段聚合成 独立事件数/胜率/均值/中位/PF/CI 等。"""
    vals = [float(r[key]) for r in rows if r.get("ok") and r.get(key) is not None]
    n = len(vals)
    if n == 0:
        return {"n": 0, "win_rate_pct": None, "mean": None, "median": None,
                "profit_factor": None, "ci95": [None, None],
                "max_favorable": None, "max_adverse": None}
    wins = [v for v in vals if v > 0]
    losses = [-v for v in vals if v < 0]
    gp = sum(wins)
    gl = sum(losses)
    pf = (gp / gl) if gl > 1e-9 else (float("inf") if gp > 0 else None)
    s = sorted(vals)
    med = (s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0)
    # 正态近似 95%CI（样本少时仅供参考，报告会显著标注）
    mean = sum(vals) / n
    sd = _std(vals)
    half = 1.96 * sd / math.sqrt(n) if n > 1 else None
    return {
        "n": n,
        "win_rate_pct": round(len(wins) / n * 100, 2),
        "mean": round(mean, 4),
        "median": round(med, 4),
        "profit_factor": (round(pf, 3) if pf not in (None, float("inf")) else pf),
        "expected_value": round(mean, 4),
        "ci95": ([round(mean - half, 4), round(mean + half, 4)]
                 if half is not None else [None, None]),
        "max_favorable": round(max(vals), 4),
        "max_adverse": round(min(vals), 4),
        "n_pos": len(wins), "n_neg": len([v for v in vals if v <= 0]),
    }
