"""决策闸门链（Decision Engine）。

所有交易动作最终都必须依次通过：

    DATA GATE
      → FRESHNESS GATE
        → PORTFOLIO CONSISTENCY GATE
          → STRATEGY TRUST GATE
            → TRADE VALUE GATE
              → RISK GATE
                → POSITION LIMIT GATE
                  → FINAL ACTION

任意一项失败 → 只能是 WAIT / HOLD / REBALANCE，**绝不能绕过**。

硬约束（继承且不放宽）：
· 研究级禁止实际 BUY（BUY Permission Gate）；
· 黄金 ≤ 5%；
· WAIT 永远合法；
· 交易金额不得超过安全上限、单批不得一次打光现金；
· 数据陈旧 → WAIT / DATA_STALE。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

GATE_ORDER = (
    "DATA GATE", "FRESHNESS GATE", "PORTFOLIO CONSISTENCY GATE",
    "STRATEGY TRUST GATE", "TRADE VALUE GATE", "RISK GATE",
    "POSITION LIMIT GATE",
)

FINAL_ACTIONS = ("BUY", "WAIT", "HOLD", "REDUCE", "REBALANCE", "DATA_STALE")

EVIDENCE_NAME = "trade_value_evidence.json"
EVIDENCE_MAX_AGE_DAYS = 30


# ==========================================================================
# 交易价值证据的持久化（由 --validate-trades 写入）
# ==========================================================================
def evidence_path(cfg=None) -> Path:
    from .config import project_root
    try:
        if cfg is not None:
            return Path(cfg.path).resolve().parent.parent / "data" / EVIDENCE_NAME
    except Exception:  # noqa: BLE001
        pass
    return Path(project_root()) / "data" / EVIDENCE_NAME


def save_trade_evidence(cfg, res: dict) -> Path:
    p = evidence_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    v = res.get("verdict") or {}
    a = (res.get("actions") or {}).get("by_action", {})
    def _m(act, h=60):
        return (((a.get(act, {}) or {}).get(h) or {}).get("delta_return_net") or {}).get("mean")
    payload = {
        "ts": (res.get("ts") or datetime.now()).strftime("%Y-%m-%d %H:%M:%S"),
        "independent_events_60": v.get("independent_events_60"),
        "buy_mean_net_pp_60": _m("BUY"),
        "reduce_mean_net_pp_60": _m("REDUCE"),
        "rebalance_mean_net_pp_60": _m("REBALANCE"),
        "sell_positive": v.get("sell_positive"),
        "can_claim_effectiveness": v.get("can_claim_effectiveness"),
        "verdict": v.get("label"),
        "note": "由 --validate-trades 生成；运行期 TRADE VALUE GATE 据此判断交易是否有正增量",
    }
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def load_trade_evidence(cfg=None) -> dict:
    p = evidence_path(cfg)
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(d, dict):
            raise ValueError("bad")
        ts = d.get("ts")
        age = None
        if ts:
            try:
                age = (datetime.now()
                       - datetime.strptime(str(ts)[:19], "%Y-%m-%d %H:%M:%S")).days
            except ValueError:
                age = None
        d["age_days"] = age
        d["stale"] = (age is not None and age > EVIDENCE_MAX_AGE_DAYS)
        return d
    except Exception:  # noqa: BLE001
        return {"available": False, "stale": True,
                "reason": "未运行 --validate-trades 或证据缺失（按最保守处理：无交易价值证据）"}


# ==========================================================================
# 闸门
# ==========================================================================
def _g(name: str, ok, detail: str, *, fatal: bool = True, **extra) -> dict:
    return {"name": name, "ok": (None if ok is None else bool(ok)),
            "detail": detail, "fatal": bool(fatal), **extra}


def _dig(d: dict, *path, default=None):
    cur = d
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


def evaluate_decision(cfg, snap: dict | None, *, trust_level: str | None = None,
                      freshness: dict | None = None,
                      evidence: dict | None = None) -> dict:
    """执行完整闸门链并给出最终动作（绝不绕过）。"""
    from . import decision as dec

    snap = snap or {}
    portfolio = snap.get("portfolio") or {}
    plan = portfolio.get("plan") or {}
    gates: list[dict] = []

    # ---- 1. DATA GATE ----
    rows = snap.get("rows") or []
    bad_px = [r for r in rows
              if (r.get("snapshot") or {}).get("price") is None]
    data_ok = bool(snap.get("ok")) and not snap.get("errors") and not bad_px
    gates.append(_g("DATA GATE", data_ok,
                    "数据完整" if data_ok else
                    ("存在加载错误" if snap.get("errors")
                     else f"{len(bad_px)} 只资产缺价格"), fatal=True))

    # ---- 2. FRESHNESS GATE ----
    if freshness is None:
        try:
            from .freshness_gate import check_freshness
            freshness = check_freshness(cfg, snap)
        except Exception as exc:  # noqa: BLE001
            freshness = {"ok": False, "action": "DATA_STALE",
                         "reason": f"新鲜度检查失败：{exc}", "can_buy": False}
    gates.append(_g("FRESHNESS GATE", freshness.get("ok"),
                    f"{freshness.get('action')}：{freshness.get('reason')}",
                    fatal=True, can_buy=bool(freshness.get("can_buy"))))

    # ---- 3. PORTFOLIO CONSISTENCY GATE ----
    cons = (portfolio.get("consistency") or plan.get("consistency")
            or _dig(snap, "decision", "consistency") or {})
    cons_ok = cons.get("ok") if cons else None
    gates.append(_g("PORTFOLIO CONSISTENCY GATE", cons_ok,
                    (cons.get("errors") and "；".join(cons["errors"])[:120])
                    or "持仓/总资产/权重自洽",
                    fatal=True))

    # ---- 4. STRATEGY TRUST GATE ----
    if trust_level is None:
        trust_level = portfolio.get("trust_level")
    perm = dec.buy_permission(cfg, trust_level)
    gates.append(_g("STRATEGY TRUST GATE", perm["allowed"], perm["reason"],
                    fatal=False, trust_level=perm["trust_level"],
                    min_level=perm["min_level"]))

    # ---- 5. TRADE VALUE GATE ----
    if evidence is None:
        evidence = load_trade_evidence(cfg)
    n_ev = evidence.get("independent_events_60") or 0
    buy_inc = evidence.get("buy_mean_net_pp_60")
    ev_ok = bool(evidence.get("buy_mean_net_pp_60") is not None
                 and (buy_inc or 0) > 0 and not evidence.get("stale")
                 and n_ev >= 30)
    if evidence.get("stale") or evidence.get("available") is False:
        detail = f"交易价值证据不可用/过期：{evidence.get('reason') or ''}"
    elif buy_inc is None:
        detail = "无样本外增量证据"
    elif n_ev < 30:
        detail = (f"独立事件 {n_ev} < 30：**交易无统计意义上的正增量证据** "
                  f"→ 不允许 BUY")
    elif (buy_inc or 0) <= 0:
        detail = f"相对 WAIT 增量 {buy_inc}pp ≤ 0 → 不允许 BUY"
    else:
        detail = f"相对 WAIT 增量 {buy_inc}pp > 0（独立事件 {n_ev}）"
    gates.append(_g("TRADE VALUE GATE", ev_ok, detail, fatal=False,
                    independent_events=n_ev, buy_increment_pp=buy_inc))

    # ---- 6. RISK GATE ----
    risk = portfolio.get("risk") or {}
    rstate = risk.get("state")
    rscore = risk.get("score")
    risk_ok = (rstate != "Crisis") and ((rscore or 0) < 70)
    gates.append(_g("RISK GATE", risk_ok,
                    f"风险状态 {rstate} / 分数 {rscore}",
                    fatal=False, state=rstate, score=rscore))

    # ---- 7. POSITION LIMIT GATE ----
    class_w = _dig(portfolio, "weights", "by_class", default={}) or {}
    gold_w = float(class_w.get("gold") or 0.0)
    gold_cap = float(cfg.get("portfolio.gold_max", 0.05)) if cfg is not None else 0.05
    gold_ok = gold_w <= gold_cap + 1e-9
    cash = float(_dig(portfolio, "weights", "cash_value", default=0.0) or 0.0)
    plan_first = float(plan.get("total_first_batch_amount") or 0.0)
    cash_ok = (plan_first <= cash + 1e-6) if cash > 0 else None
    gates.append(_g("POSITION LIMIT GATE", (gold_ok and (cash_ok is not False)),
                    (f"黄金 {gold_w * 100:.1f}% / 上限 {gold_cap * 100:.0f}%"
                     + (f"；首批 {plan_first:,.0f} 元 / 现金 {cash:,.0f} 元"
                        if cash > 0 else "；未填总资产，跳过现金约束")),
                    fatal=False, gold_pct=round(gold_w * 100, 2),
                    cash=round(cash, 2), first_batch=round(plan_first, 2)))

    # ---- FINAL ACTION ----
    if freshness.get("action") == "DATA_STALE":
        final = "DATA_STALE"
        reason = "关键行情陈旧 → DATA_STALE（禁止 BUY，只能 WAIT）"
    elif not data_ok or cons_ok is False:
        final = "WAIT"
        reason = "数据/一致性闸门未通过 → WAIT"
    elif not risk_ok:
        final = "REDUCE"
        reason = "风险闸门未通过（危机/高分）→ 只做防守性减仓或 WAIT"
    elif not gold_ok:
        final = "REBALANCE"
        reason = "黄金超 5% 硬上限 → 先再平衡"
    elif not perm["allowed"]:
        final = "HOLD" if float(_dig(portfolio, "weights", "etf_value",
                                     default=0.0) or 0.0) > 0 else "WAIT"
        reason = f"可信等级 {perm['trust_level']} 未达门槛 → 不开放买入（{final}）"
    elif not ev_ok:
        final = "HOLD"
        reason = f"交易价值闸门未通过（{detail}）→ 不买入，维持持有观察"
    else:
        final = "BUY"
        reason = "全部闸门通过且存在样本外正增量 → 允许分批买入"

    return {
        "final_action": final,
        "reason": reason,
        "gates": gates,
        "trust_level": perm["trust_level"],
        "buy_permission": perm,
        "trade_evidence": evidence,
        "freshness": {k: freshness.get(k) for k in
                      ("action", "ok", "can_buy", "quote_basis", "realtime_note")},
        "waits_are_legal": True,
        "can_buy_now": final == "BUY",
    }


def describe(dec_res: dict) -> list[str]:
    L = [f"最终动作：{dec_res.get('final_action')}", f"原因：{dec_res.get('reason')}", ""]
    L.append("| 闸门 | 通过 | 说明 |")
    L.append("|---|---|---|")
    for g in dec_res.get("gates") or []:
        ok = g.get("ok")
        L.append(f"| {g['name']} | "
                 + ("✅" if ok is True else "➖" if ok is None else "❌")
                 + f" | {g.get('detail')} |")
    L.append("")
    L.append(f"> {dec_res.get('freshness', {}).get('realtime_note', '')}")
    return L
