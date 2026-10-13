"""交易成本模型（LOW / BASE / HIGH 三档）。

纪律
----
1. 成本只描述"真实成交需要付出多少"，不参与任何"让收益变好看"的调整；
2. 三档成本必须分别重跑策略：若某策略只在 LOW 档成立 → 判"对交易成本敏感，
   不适合实盘"，不得只报告最好的一档；
3. 成本口径 = 佣金(单边) + 滑点(单边)，按**换手率**计费：
       成本金额 = 组合市值 × 换手率 × (佣金率 + 滑点率)
   换手率 = Σ|w_after − w_before| / 2（双边换手的一半，再平衡常用口径）。
4. 最小交易单位（ETF 100 份）与价格最小变动（0.001 元）用于"实际可交易金额"，
   避免出现"建议买入 3.7 元"这种不可执行结果。
"""
from __future__ import annotations

TRADING_DAYS = 252

# 单边成本率（小数）。BASE 档与原系统 cost.trade_cost_pct=0.20% 对齐。
COST_TIERS: dict[str, dict] = {
    "LOW":  {"commission": 0.00005, "slippage": 0.00045, "label": "低（大额/免佣/低冲击）"},
    "BASE": {"commission": 0.00010, "slippage": 0.00190, "label": "基准（默认 0.20%）"},
    "HIGH": {"commission": 0.00030, "slippage": 0.00470, "label": "高（小额/高滑点/弱流动性）"},
}

# ETF 最小交易单位与价格最小变动
ETF_LOT = 100
PRICE_TICK = 0.001


def tier_names() -> tuple[str, ...]:
    return tuple(COST_TIERS)


def one_way_rate(tier: str = "BASE") -> float:
    """单边成本率（小数）：佣金 + 滑点。"""
    t = COST_TIERS.get(str(tier).upper()) or COST_TIERS["BASE"]
    return float(t["commission"]) + float(t["slippage"])


def turnover(weights_before: dict[str, float],
             weights_after: dict[str, float]) -> float:
    """换手率（0~1）：Σ|Δw| / 2。仅在两者共同资产上比较。"""
    keys = set(weights_before) | set(weights_after)
    tot = 0.0
    for k in keys:
        tot += abs(float(weights_after.get(k, 0.0)) - float(weights_before.get(k, 0.0)))
    return tot / 2.0


def trade_cost(weights_before: dict[str, float],
               weights_after: dict[str, float],
               tier: str = "BASE") -> float:
    """一次调仓的成本（占组合总市值的比例，小数）。"""
    return turnover(weights_before, weights_after) * one_way_rate(tier)


def cost_for_turnover(turnover_frac: float, tier: str = "BASE") -> float:
    return max(0.0, float(turnover_frac)) * one_way_rate(tier)


def tier_table() -> list[dict]:
    out = []
    for name, d in COST_TIERS.items():
        out.append({
            "tier": name,
            "commission_pct": round(d["commission"] * 100, 4),
            "slippage_pct": round(d["slippage"] * 100, 4),
            "one_way_pct": round((d["commission"] + d["slippage"]) * 100, 4),
            "round_trip_pct": round((d["commission"] + d["slippage"]) * 200, 4),
            "label": d["label"],
        })
    return out


# --------------------------------------------------------------------------
# 实际可交易金额（最小单位 / 价格最小变动 / 成交量约束）
# --------------------------------------------------------------------------
def tradable_shares(amount: float, price: float, lot: int = ETF_LOT) -> int:
    """把金额换成"可成交份额"（向下取整到最小交易单位）。"""
    if not price or price <= 0 or amount <= 0:
        return 0
    return int(amount / price / lot) * lot


def tradable_amount(amount: float, price: float, lot: int = ETF_LOT) -> float:
    """把金额换成"可成交金额"（含最小单位约束）。"""
    return tradable_shares(amount, price, lot) * float(price or 0.0)


def round_to_tick(price: float, tick: float = PRICE_TICK) -> float:
    if not price or price <= 0:
        return 0.0
    return round(price / tick) * tick


def estimate_fill_price(open_price: float, side: str, tier: str = "BASE") -> float:
    """估实际成交价：以 T+1 开盘价为基准，叠加该档滑点（买向上、卖向下）。

    只用开盘价 + 滑点，不使用任何未来信息。
    """
    if not open_price or open_price <= 0:
        return 0.0
    slip = float((COST_TIERS.get(str(tier).upper()) or COST_TIERS["BASE"])["slippage"])
    if str(side).upper() == "BUY":
        return round_to_tick(open_price * (1.0 + slip))
    return round_to_tick(open_price * (1.0 - slip))


def cost_sensitivity_verdict(results_by_tier: dict[str, float | None]) -> dict:
    """三档成本下的增量结论是否一致。results_by_tier: {tier: 增量pp 或 None}。

    · 三档同号且都 > 0 → 通过成本压力测试；
    · 只在 LOW 档 > 0 → 对成本敏感，不适合实盘；
    · 全档 <= 0 → 无正增量。
    """
    vals = {k: v for k, v in (results_by_tier or {}).items() if v is not None}
    if not vals:
        return {"pass": False, "verdict": "UNKNOWN", "detail": "无成本敏感性数据"}
    low = vals.get("LOW")
    base = vals.get("BASE")
    high = vals.get("HIGH")
    all_pos = all(v > 0 for v in vals.values())
    all_neg = all(v <= 0 for v in vals.values())
    if all_pos:
        return {"pass": True, "verdict": "通过",
                "detail": "LOW/BASE/HIGH 三档增量均为正"}
    if all_neg:
        return {"pass": False, "verdict": "无正增量",
                "detail": "三档增量均不为正"}
    if low is not None and low > 0 and (base is None or base <= 0):
        return {"pass": False, "verdict": "对交易成本敏感",
                "detail": "仅 LOW 档为正 → 不适合实盘"}
    return {"pass": False, "verdict": "不一致",
            "detail": f"LOW={low} / BASE={base} / HIGH={high} 方向不一致"}
