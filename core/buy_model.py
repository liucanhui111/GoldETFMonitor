"""BUY 模型 —— 把"上涨概率"与"交易价值"彻底分开。

BUY 不再因为"概率高"就能买。必须**同时**满足下列 10 个条件
（任一不满足 → 不允许 BUY，降级为 WAIT / HOLD / REBALANCE）：

  1. 样本外模型可用（存在 h=60 独立事件去重 OOS 结果）
  2. 独立事件样本达到门槛（≥30；不足则只作研究）
  3. 模型相对 WAIT 有**正的样本外增量**（增量收益均值 > 0）
  4. 交易后组合质量提升（delta_portfolio_quality > 0）
  5. 交易后组合风险没有异常恶化（delta_max_drawdown ≥ 阈值、delta_volatility 不超限）
  6. 交易成本后仍有正增量（delta_return_net > 0）
  7. 不存在明显追高（1 年相对价位 / BIAS20 未过高）
  8. 数据全部新鲜（freshness gate 放行）
  9. 没有关键宏观数据 UNKNOWN
 10. BUY Permission Gate 已开放（可信等级 ≥ 门槛）

界面必须**同时**分别显示：组合未来上涨概率 / 相对 WAIT 的增量收益 /
相对 WAIT 的风险变化 / 交易价值等级 —— 不得混成一个数字。
"""
from __future__ import annotations

BUY_CONDITIONS = (
    "样本外模型可用", "独立事件样本≥门槛", "相对WAIT正增量", "组合质量提升",
    "风险未异常恶化", "扣成本后仍为正", "非追高", "数据新鲜",
    "无关键宏观UNKNOWN", "买入权限已开放",
)


def safe_amount(amount: float, cfg, *, cash: float | None = None,
                gap: float | None = None, first_batch: bool = False) -> dict:
    """把建议金额压到安全上限内：绝不越界、绝不打光现金、单批不一次满仓。"""
    try:
        max_asset = float(cfg.get("simulation.max_amount_per_asset", 20000) or 20000)
    except Exception:  # noqa: BLE001
        max_asset = 20000.0
    caps: list[tuple[str, float]] = [("单资产上限", max_asset)]
    if cash is not None and cash > 0:
        if first_batch:
            try:
                pct = float(cfg.get("simulation.first_batch_cap_pct_of_cash", 33)
                            or 33) / 100.0
            except Exception:  # noqa: BLE001
                pct = 0.33
            caps.append(("首批现金占比上限", cash * pct))
        else:
            caps.append(("现金总额", cash))
    if first_batch and gap is not None and gap > 0:
        try:
            gpct = float(cfg.get("simulation.first_batch_cap_pct_of_gap", 50)
                         or 50) / 100.0
        except Exception:  # noqa: BLE001
            gpct = 0.5
        caps.append(("首批缺口占比上限", gap * gpct))
    amt = max(0.0, float(amount or 0.0))
    capped_by = "原始金额"
    for name, cap in caps:
        if amt > cap:
            amt = cap
            capped_by = name
    return {"amount": round(amt, 2), "capped_by": capped_by,
            "caps": [{"name": n, "cap": round(c, 2)} for n, c in caps]}


def trade_value_grade(ctx: dict) -> str:
    """交易价值等级（按增量证据强弱分档；与"上涨概率"完全独立）。"""
    inc = ctx.get("oos_increment_pp")
    n = int(ctx.get("independent_events") or 0)
    cost_ok = bool(ctx.get("cost_net_positive"))
    if inc is None:
        return "UNKNOWN（无样本外增量证据）"
    if inc > 0 and n >= 30 and cost_ok:
        return "A 有正增量（独立事件足够）"
    if inc > 0 and cost_ok:
        return f"B 有正增量但样本不足（n={n}<30）"
    if inc > 0:
        return "C 有增量但被交易成本吃掉"
    return "D 无正增量（相对 WAIT 无优势）"


def evaluate_buy(ctx: dict) -> dict:
    """返回 10 条件逐项结果 + 是否允许 BUY。ctx 见模块 docstring。"""
    n = int(ctx.get("independent_events") or 0)
    inc = ctx.get("oos_increment_pp")
    dq = ctx.get("delta_portfolio_quality")
    ddd = ctx.get("delta_max_drawdown")
    dvol = ctx.get("delta_volatility")

    checks: list[tuple[str, bool | None, str]] = []
    checks.append(("样本外模型可用", bool(ctx.get("oos_available")),
                   "存在独立事件去重 OOS 结果" if ctx.get("oos_available")
                   else "无样本外结果 → 只能研究"))
    checks.append(("独立事件样本≥门槛", n >= 30, f"独立事件 {n}（门槛 30）"))
    checks.append(("相对WAIT正增量", (inc is not None and inc > 0),
                   f"相对 WAIT 增量 {inc}pp" if inc is not None else "无增量数据"))
    checks.append(("组合质量提升", (dq is not None and dq > 0),
                   f"质量增量 {dq}" if dq is not None else "无质量数据"))
    risk_ok = True
    risk_detail = "风险未异常恶化"
    if ddd is not None and ddd < float(ctx.get("dd_tolerance_pp", -5.0)):
        risk_ok = False
        risk_detail = f"最大回撤恶化 {ddd}pp（超容差）"
    elif dvol is not None and dvol > float(ctx.get("vol_tolerance_pp", 3.0)):
        risk_ok = False
        risk_detail = f"波动恶化 {dvol}pp（超容差）"
    checks.append(("风险未异常恶化", risk_ok, risk_detail))
    checks.append(("扣成本后仍为正", bool(ctx.get("cost_net_positive")),
                   "扣成本后增量仍为正" if ctx.get("cost_net_positive")
                   else "扣成本后不再为正"))
    checks.append(("非追高", not bool(ctx.get("chasing_high")),
                   "无追高" if not ctx.get("chasing_high") else "处于高位/高乖离，禁止追涨"))
    checks.append(("数据新鲜", bool(ctx.get("freshness_ok")),
                   "数据新鲜" if ctx.get("freshness_ok") else "数据陈旧/缺失 → 禁止买入"))
    checks.append(("无关键宏观UNKNOWN", bool(ctx.get("macro_ok", True)),
                   "关键宏观数据齐全" if ctx.get("macro_ok", True)
                   else "存在关键宏观 UNKNOWN"))
    checks.append(("买入权限已开放", bool(ctx.get("gate_allowed")),
                   "可信等级达标" if ctx.get("gate_allowed")
                   else f"可信等级 {ctx.get('trust_level')} 未达门槛"))

    allowed = all(c[1] for c in checks)
    return {
        "allowed": allowed,
        "conditions": [{"name": nm, "ok": ok, "detail": dt} for nm, ok, dt in checks],
        "trade_value_grade": trade_value_grade(ctx),
        "prob_used_pct": ctx.get("prob_used_pct"),
        "increment_pp": inc,
        "risk_change": {"delta_max_drawdown": ddd, "delta_volatility": dvol},
        "note": ("上涨概率与交易价值是两个概念；即使概率高，只要相对 WAIT "
                 "没有正增量，也不允许 BUY。"),
    }
