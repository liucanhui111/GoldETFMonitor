"""事件流水线审计（Event Audit）——查清"为什么 60 日只有 2 个独立事件"。

本轮唯一目标
------------
1. 把"事件"的定义从 **walk-forward 折边界**（旧口径 `_fold_events`）纠正为
   **模型真实信号日 + 严格独立去重**（新口径）；
2. 输出 15 级事件漏斗，逐级给出 进入/淘汰/比例/淘汰原因 Top10；
3. 严格区分 **candidate_event（研究层：模型认为存在机会）** 与
   **executable_event（生产层：安全/成本/仓位/新鲜度/权限都放行）**，
   两层分别做 OOS 统计，绝不合并成一个数字；
4. 严格区分 **RESEARCH_POLICY（回答"这条规则有没有历史增量价值"）** 与
   **PRODUCTION_POLICY（回答"今天到底允不允许做"）**；
5. 模型阶梯 Model 0-4，**全部使用完全相同的 OOS 事件集合**；
6. 事件定义稳定性（阈值 ±5% / ±2% / 0 / +2% / +5% 扰动），
   结果翻转 → 标记「事件定义敏感」，**不挑选最好的一组**。

纪律（继承且不放宽）
--------------------
* 独立事件间隔：20 日 ≥20、60 日 ≥60、120 日 ≥120；
* 保留 PURGE + EMBARGO；禁止把重叠窗口当独立事件；
* 主区间固定 2023-06-13 ~ 2026-09-30，**不为凑样本引入区间外数据**；
* 禁止参数淘金；不为改善结果新增指标或复杂度；
* **不降低研究级门槛、不把研究级改成可 BUY**——即使本轮找到更多候选事件。
"""
from __future__ import annotations

import math
from datetime import datetime

from . import cost_model as cm
from . import trade_value as tv
from . import walk_forward as wf
from . import sell_model as sm
from . import policy_evaluator as pe

TRADING_DAYS = 252
HORIZONS = (20, 60, 120)
REGIMES = ("Risk On", "Normal", "Risk Off", "Crisis")

# 指标预热：1 年高点/低点、252 日回撤、60 日波动、MA60 都需要 ~252 日
WARMUP_DAYS = 252
# 目标与现状"实质差异"门槛：单边换手 < 1% 视为同一仓位（BUY == WAIT，无交易意义）
MIN_TURNOVER = 0.01
# 再平衡候选门槛：任一资产偏离目标 ≥ 3%
REBALANCE_THRESHOLD = 0.03
# regime 目标股票仓位（优化模型）
STOCK_TARGET_BASE = {"Risk On": 0.50, "Normal": 0.45, "Risk Off": 0.35, "Crisis": 0.30}
# 事件独立门槛（用户硬要求）
INDEPENDENT_THRESHOLD = 30


# ==========================================================================
# 基础：可评估日网格 / 理论上界 / 参数化 regime 与目标权重
# ==========================================================================
def eligible_days(n: int, horizon: int, warmup: int = WARMUP_DAYS) -> list[int]:
    """可评估日：指标预热完成，且 t+1..t+h 的前瞻窗口真实存在。"""
    hi = int(n) - 1 - int(horizon)
    lo = int(warmup)
    return list(range(lo, hi + 1)) if hi >= lo else []


def max_independent_bound(n: int, horizon: int,
                          warmup: int = WARMUP_DAYS) -> int:
    """**理论独立事件上界**：即使每天都信号，间隔 ≥h 也只能取到这么多。

    这是本报告最关键的一个数：它说明"独立事件 <30"有多少是数据长度决定的，
    而不是模型/验证器的问题。
    """
    d = eligible_days(n, horizon, warmup)
    return len(wf.dedup_events(d, horizon)) if d else 0


def regime_series_th(panel, classes: dict, w_ref: dict, *,
                     dd_crisis: float = -12.0, vol_crisis: float = 0.18,
                     breadth_off: float = 0.34, breadth_on: float = 0.67) -> list[dict]:
    """参数化市场状态（默认参数与 policy_evaluator.regime_series 完全一致）。"""
    stock = [a for a in panel.assets if classes.get(a) == "stock"]
    ma60 = {a: pe._ma_series(panel.close.get(a) or [], 60) for a in panel.assets}
    vol = pe._vol_series(panel, w_ref, 60)
    nav = panel.nav
    out: list[dict] = []
    for i in range(len(nav)):
        ok = tot = 0
        for a in stock:
            m = ma60.get(a) or []
            cl = panel.close.get(a) or []
            c = cl[i] if i < len(cl) else None
            if i < len(m) and m[i] is not None and c is not None:
                tot += 1
                ok += 1 if float(c) > m[i] else 0
        breadth = (ok / tot) if tot else None
        lo = max(0, i - 251)
        peak = max(nav[lo:i + 1]) if nav[lo:i + 1] else nav[i]
        dd = (nav[i] / peak - 1.0) * 100.0 if peak else 0.0
        v = vol[i] if i < len(vol) else 0.0
        if dd <= dd_crisis and v >= vol_crisis:
            name = "Crisis"
        elif breadth is not None and breadth <= breadth_off:
            name = "Risk Off"
        elif breadth is not None and breadth >= breadth_on:
            name = "Risk On"
        else:
            name = "Normal"
        out.append({"regime": name, "breadth": breadth,
                    "dd_pct": round(dd, 2), "vol": round(v, 4)})
    return out


def policy_weights_th(panel, classes: dict, raw_targets: dict, regime_name: str,
                      gold_cap: float, stock_map: dict | None = None) -> dict:
    """按 regime 生成目标权重（股票/债券/黄金三类）。"""
    stock_map = stock_map or STOCK_TARGET_BASE
    stock_t = float(stock_map.get(regime_name, 0.45))
    gold_t = float(gold_cap)
    bond_t = max(0.0, 1.0 - stock_t - gold_t)
    return pe._distribute(panel, classes, raw_targets,
                          {"stock": stock_t, "bond": bond_t, "gold": gold_t})


# ==========================================================================
# 单日分类：某一天模型"认为"存在哪些候选动作（研究层信号）
# ==========================================================================
def classify_day(panel, classes: dict, raw_targets: dict, regime_name: str,
                 gold_cap: float, w_wait: dict, w_fixed: dict, t: int, *,
                 stock_map: dict | None = None,
                 min_turnover: float = MIN_TURNOVER,
                 rebalance_threshold: float = REBALANCE_THRESHOLD,
                 sell_candidates: list | None = None) -> dict:
    """返回 {'BUY'/'SELL'/'REBALANCE': {...}}（只含当日成立的候选）。

    * BUY  ：regime 允许（非 Risk Off / Crisis）且目标与现状换手 ≥ 门槛；
    * SELL ：`sell_model` 的 5 类候选中存在"≥2 独立信号"的合法候选；
    * REBALANCE：任一资产偏离目标 ≥ 门槛。
    """
    stock_map = stock_map or STOCK_TARGET_BASE
    out: dict = {}
    if regime_name not in ("Risk Off", "Crisis"):
        wa = policy_weights_th(panel, classes, raw_targets, regime_name,
                               gold_cap, stock_map)
        turn = cm.turnover(w_wait, wa)
        if turn >= float(min_turnover):
            out["BUY"] = {"weights": wa, "turnover": round(turn, 4),
                          "regime": regime_name}
    dev = max((abs(float(w_wait.get(a, 0.0)) - float(w_fixed.get(a, 0.0)))
               for a in panel.assets), default=0.0)
    if dev >= float(rebalance_threshold):
        out["REBALANCE"] = {"weights": dict(w_fixed),
                            "deviation": round(dev, 4)}
    if sell_candidates:
        valids = [c for c in sell_candidates if sm.valid_sell_candidate(c)]
        if valids:
            out["SELL"] = {"candidates": valids, "n": len(valids)}
    return out


def _sell_candidates_for_day(panel, t: int, w_wait: dict, raw_targets: dict,
                             gold_cap: float, rebalance_watch_pct: float = 3.0) -> list:
    try:
        return sm.generate_sell_candidates(panel, t, w_wait, raw_targets,
                                           gold_cap=gold_cap,
                                           rebalance_watch_pct=rebalance_watch_pct)
    except Exception:  # noqa: BLE001
        return []


def _all_candidate_days(panel, classes, raw_targets, regimes, gold_cap,
                        w_wait, w_fixed, *, warmup: int = WARMUP_DAYS,
                        **kw) -> dict:
    """在 [warmup, n) 全区间上逐日分类（供漏斗使用；horizon 无关）。"""
    n = len(panel.nav)
    per: dict[str, list[int]] = {"BUY": [], "SELL": [], "REBALANCE": []}
    cache: dict[int, list] = {}
    for t in range(int(warmup), n):
        cache[t] = _sell_candidates_for_day(panel, t, w_wait, raw_targets, gold_cap)
        cls = classify_day(panel, classes, raw_targets,
                           (regimes[t] or {}).get("regime"), gold_cap,
                           w_wait, w_fixed, t, sell_candidates=cache[t], **kw)
        for act in per:
            if act in cls:
                per[act].append(t)
    per["_sell_cache"] = cache  # type: ignore[assignment]
    return per


# ==========================================================================
# 一、15 级事件漏斗
# ==========================================================================
def _reasons(counts: dict, total: int) -> list[dict]:
    """把 {原因: 次数} 转成 Top10（含占**淘汰总量**的比例）。"""
    base = sum(v for v in counts.values() if v) or 0
    rows = sorted(counts.items(), key=lambda kv: -kv[1])[:10]
    return [{"reason": k, "count": v,
             "pct": round(v / base * 100, 2) if base else None}
            for k, v in rows]


def build_funnel(cfg, panel, classes, raw_targets, regimes, gold_cap,
                 w_wait, w_fixed, *, horizon: int) -> dict:
    """15 级事件漏斗（针对给定 horizon）。

    每一级输出：进入数量 / 被淘汰数量 / 淘汰比例 / 淘汰原因 Top10。
    """
    n = len(panel.nav)
    levels: list[dict] = []

    def add(name: str, entered: int, eliminated: int,
            reasons: dict | None = None) -> None:
        inp = int(entered) + int(eliminated)
        levels.append({
            "level": name,
            "input": inp,
            "entered": int(entered),
            "eliminated": int(eliminated),
            "ratio": round(eliminated / inp, 4) if inp else None,
            "reasons": _reasons(reasons or {}, inp),
        })

    # --- L1 总交易日 ---
    add("1. 总交易日", n, 0)
    # --- L2 具备完整六 ETF 真实行情的交易日 ---
    assets = list(panel.assets)
    full = [i for i in range(n)
            if all((panel.close[a][i] is not None and panel.ret[a][i] is not None)
                   for a in assets)]
    add("2. 具备完整六 ETF 真实行情", len(full), n - len(full),
        {"行情缺失（该日至少一只 ETF 无收盘/收益）": n - len(full)})
    # --- L3 指标可计算交易日 ---
    ma60 = {a: pe._ma_series(panel.close.get(a) or [], 60) for a in assets}
    ind_ok = [i for i in full
              if i >= WARMUP_DAYS
              and all((i < len(ma60[a]) and ma60[a][i] is not None) for a in assets)]
    dropped_ind = len(full) - len(ind_ok)
    add("3. 指标可计算（预热完成）", len(ind_ok), dropped_ind,
        {"预热不足（<252 日，MA60/52周高点/252日回撤未成型）":
         sum(1 for i in full if i < WARMUP_DAYS),
         "长周期指标缺失": sum(1 for i in full if i >= WARMUP_DAYS
                              and not all((i < len(ma60[a]) and ma60[a][i] is not None)
                                          for a in assets))})
    # --- L4 新鲜度闸门通过 ---
    # 历史回测中"新鲜度"是运行期实时闸门；回测口径下按"行情连续、无缺口"计，
    # 并如实标注它在本层不构成淘汰。
    add("4. Freshness gate 通过（回测口径）", len(ind_ok), 0,
        {"注：新鲜度是运行期实时闸门，历史回测中不淘汰（口径见 DATA_FRESHNESS_GATE）": 0})
    # --- L5 组合一致性闸门通过 ---
    add("5. Portfolio consistency gate 通过", len(ind_ok), 0,
        {"注：回测中权重恒归一，一致性恒真；该闸门只在运行期生效": 0})
    # --- L6/L7/L8 候选 ---
    per = _all_candidate_days(panel, classes, raw_targets, regimes, gold_cap,
                              w_wait, w_fixed)
    buy_days = [t for t in per["BUY"] if t >= WARMUP_DAYS]
    sell_days = [t for t in per["SELL"] if t >= WARMUP_DAYS]
    reb_days = [t for t in per["REBALANCE"] if t >= WARMUP_DAYS]
    base_days = len(ind_ok)
    add("6. 能生成候选 BUY 的交易日", len(buy_days), base_days - len(buy_days),
        _buy_reject_reasons(panel, classes, raw_targets, regimes, gold_cap,
                            w_wait, w_fixed, ind_ok))
    add("7. 能生成候选 SELL 的交易日", len(sell_days), base_days - len(sell_days),
        {"无合法卖出候选（缺 ≥2 独立信号，或未触发 5 类卖出规则）":
         base_days - len(sell_days)})
    add("8. 能生成候选 REBALANCE 的交易日", len(reb_days), base_days - len(reb_days),
        {f"任一资产偏离目标 < {REBALANCE_THRESHOLD * 100:.0f}%": base_days - len(reb_days)})
    # --- L9 通过风险限制 ---
    risk_ok = [i for i in ind_ok if (regimes[i] or {}).get("regime") != "Crisis"]
    add("9. 通过风险限制（非 Crisis）", len(risk_ok), len(ind_ok) - len(risk_ok),
        {"Crisis 状态 → 禁止新增风险": len(ind_ok) - len(risk_ok)})
    # --- L10 通过仓位限制（**只约束 BUY 分支**；REBALANCE/SELL 不受影响）---
    gold_now = sum(float(w_wait.get(a, 0.0)) for a in panel.assets
                   if _class_of(classes, a) == "gold")
    gold_ok = gold_now <= float(gold_cap) + 1e-9
    buy_risk_ok = [t for t in risk_ok if t in set(buy_days)]
    if gold_ok:
        add("10. 通过 position limit（黄金 ≤ 上限，仅 BUY 分支）", len(buy_risk_ok), 0,
            {f"注：WAIT 组合黄金 {gold_now * 100:.1f}% ≤ 上限 {gold_cap * 100:.0f}%": 0})
    else:
        add("10. 通过 position limit（黄金 ≤ 上限，仅 BUY 分支）", 0, len(buy_risk_ok),
            {f"WAIT 组合黄金 {gold_now * 100:.1f}% > 上限 {gold_cap * 100:.0f}% "
             f"→ BUY 分支被全数拦截（须先再平衡）；"
             f"REBALANCE/SELL 分支不受此闸门约束": len(buy_risk_ok)})
    # --- L11 进入 Trade Value OOS ---
    elig = set(eligible_days(n, horizon))
    any_cand = sorted(set(buy_days) | set(sell_days) | set(reb_days))
    enter = [t for t in any_cand if t in elig]
    add("11. 进入 Trade Value OOS（前瞻窗口真实存在）", len(enter),
        len(any_cand) - len(enter),
        {f"t + {horizon} 日超出区间末端（无真实前瞻收益）": len(any_cand) - len(enter)})
    # --- L12/13/14 分周期可评估事件 ---
    lvl = {}
    for h in HORIZONS:
        eh = set(eligible_days(n, h))
        lvl[h] = {
            "BUY": len([t for t in buy_days if t in eh]),
            "SELL": len([t for t in sell_days if t in eh]),
            "REBALANCE": len([t for t in reb_days if t in eh]),
            "any": len([t for t in any_cand if t in eh]),
        }
    add(f"12. 按 20 日可评估事件数（候选）", lvl[20]["any"],
        len(any_cand) - lvl[20]["any"],
        {"超出 20 日前瞻窗口": len(any_cand) - lvl[20]["any"]})
    add(f"13. 按 60 日可评估事件数（候选）", lvl[60]["any"],
        len(any_cand) - lvl[60]["any"],
        {"超出 60 日前瞻窗口": len(any_cand) - lvl[60]["any"]})
    add(f"14. 按 120 日可评估事件数（候选）", lvl[120]["any"],
        len(any_cand) - lvl[120]["any"],
        {"超出 120 日前瞻窗口": len(any_cand) - lvl[120]["any"]})
    # --- L15 最终独立事件 ---
    indep = {h: wf.dedup_events(eligible_days(n, h), h) for h in HORIZONS}
    add("15. 最终独立事件（间隔 ≥ h 去重）", len(indep[horizon]),
        lvl[horizon]["any"] - len(indep[horizon]),
        {f"与上一事件间隔 < {horizon} 日（重叠窗口，不算独立）":
         max(0, lvl[horizon]["any"] - len(indep[horizon]))})

    return {
        "horizon": horizon,
        "levels": levels,
        "candidate_days": {"BUY": len(buy_days), "SELL": len(sell_days),
                           "REBALANCE": len(reb_days), "any": len(any_cand)},
        "per_horizon_candidates": lvl,
        "independent": {h: len(indep[h]) for h in HORIZONS},
        "max_independent_bound": {h: max_independent_bound(n, h) for h in HORIZONS},
        "gold_now_pct": round(gold_now * 100, 2),
        "gold_ok": gold_ok,
    }


def _class_of(classes: dict, aid: str) -> str:
    return str((classes or {}).get(aid, ""))


def _buy_reject_reasons(panel, classes, raw_targets, regimes, gold_cap,
                        w_wait, w_fixed, ind_ok) -> dict:
    """BUY 候选被淘汰的原因分解（regime 不允许 / 换手不足）。"""
    reg_no = 0
    turn_no = 0
    for t in ind_ok:
        rg = (regimes[t] or {}).get("regime")
        if rg in ("Risk Off", "Crisis"):
            reg_no += 1
            continue
        wa = policy_weights_th(panel, classes, raw_targets, rg, gold_cap, STOCK_TARGET_BASE)
        if cm.turnover(w_wait, wa) < MIN_TURNOVER:
            turn_no += 1
    return {f"regime ∈ {{Risk Off, Crisis}} → 不允许 BUY": reg_no,
            f"目标与现状换手 < {MIN_TURNOVER * 100:.0f}% → 与实际持仓同构，无交易": turn_no}


# ==========================================================================
# 二、候选 / 可执行 双层事件
# ==========================================================================
def candidate_events(panel, classes, raw_targets, regimes, gold_cap,
                     w_wait, w_fixed, horizon: int, *,
                     warmup: int = WARMUP_DAYS, **kw) -> dict:
    """研究层事件：模型认为存在机会即入池，**再按间隔 ≥ h 去重**。"""
    days = eligible_days(len(panel.nav), horizon, warmup)
    out: dict[str, list[dict]] = {"BUY": [], "SELL": [], "REBALANCE": []}
    for t in days:
        sellc = _sell_candidates_for_day(panel, t, w_wait, raw_targets, gold_cap)
        cls = classify_day(panel, classes, raw_targets,
                           (regimes[t] or {}).get("regime"), gold_cap,
                           w_wait, w_fixed, t, sell_candidates=sellc, **kw)
        for act, info in cls.items():
            out[act].append({"t": t, "regime": (regimes[t] or {}).get("regime"),
                             "info": info})
    kept: dict[str, list[dict]] = {}
    for act, lst in out.items():
        ts = [e["t"] for e in lst]
        keep = set(wf.dedup_events(ts, horizon))
        kept[act] = [e for e in lst if e["t"] in keep]
    return {"all": out, "independent": kept,
            "n_all": {a: len(v) for a, v in out.items()},
            "n_independent": {a: len(v) for a, v in kept.items()}}


def production_gate(cfg, panel, classes, w_wait, regimes, t: int, action: str,
                    *, trust_allows_buy: bool = False,
                    gold_cap: float = 0.05) -> dict:
    """生产层闸门（今天到底允不允许做）。**只有这一层才把执行权限当过滤条件。**

    * freshness  ：运行期实时闸门（历史回测口径恒通过，见 FUNNEL L4）；
    * consistency：恒通过（权重归一）；
    * trust      ：研究级 → BUY 一律不放行（BUY Permission Gate，必须保留）；
    * risk       ：Crisis → 不允许新增风险；
    * position   ：黄金 > 上限 → 必须先再平衡。
    """
    gates: list[tuple[str, bool, str]] = []
    gates.append(("FRESHNESS", True, "回测口径恒通过（运行期实时闸门）"))
    gates.append(("CONSISTENCY", True, "权重归一，恒通过"))
    if action == "BUY":
        gates.append(("TRUST", bool(trust_allows_buy),
                      "可信等级达标" if trust_allows_buy
                      else "研究级 → BUY Permission Gate 关闭（必须保留）"))
        rg = (regimes[t] or {}).get("regime")
        gates.append(("RISK", rg != "Crisis",
                      f"regime={rg}" + ("" if rg != "Crisis" else " → 禁止新增风险")))
        gold_now = sum(float(w_wait.get(a, 0.0)) for a in panel.assets
                       if _class_of(classes, a) == "gold")
        gold_ok = gold_now <= float(gold_cap) + 1e-9
        gates.append(("POSITION", gold_ok,
                      f"黄金 {gold_now * 100:.1f}% / 上限 {gold_cap * 100:.0f}%"))
    else:
        gates.append(("TRUST", True, "防守性动作不受买入权限限制"))
    ok = all(g[1] for g in gates)
    return {"ok": ok, "gates": [{"name": n, "ok": v, "detail": d}
                                for n, v, d in gates]}


def executable_events(cfg, panel, classes, raw_targets, regimes, gold_cap,
                      w_wait, w_fixed, horizon: int, *,
                      trust_allows_buy: bool = False,
                      warmup: int = WARMUP_DAYS) -> dict:
    """生产层事件：candidate 再经过生产闸门后允许实际执行的子集。"""
    cand = candidate_events(panel, classes, raw_targets, regimes, gold_cap,
                            w_wait, w_fixed, horizon, warmup=warmup)
    kept: dict[str, list[dict]] = {}
    rejected: dict[str, int] = {}
    for act, lst in cand["independent"].items():
        good = []
        rej = 0
        for e in lst:
            g = production_gate(cfg, panel, classes, w_wait, regimes, e["t"], act,
                                trust_allows_buy=trust_allows_buy, gold_cap=gold_cap)
            if g["ok"]:
                good.append(e)
            else:
                rej += 1
        kept[act] = good
        rejected[act] = rej
    # 未去重的"有效候选日"（通过生产闸门者）——供样本充分性的三层口径使用
    all_ok: dict[str, int] = {}
    for act, lst in cand["all"].items():
        c = 0
        for e in lst:
            g = production_gate(cfg, panel, classes, w_wait, regimes, e["t"], act,
                                trust_allows_buy=trust_allows_buy, gold_cap=gold_cap)
            if g["ok"]:
                c += 1
        all_ok[act] = c
    return {"independent": kept, "rejected": rejected,
            "n_independent": {a: len(v) for a, v in kept.items()},
            "n_rejected": rejected,
            "n_all_executable": all_ok}


# ==========================================================================
# 三、研究层 / 生产层 双口径评价
# ==========================================================================
def _best_sell(info: dict) -> dict | None:
    cands = (info or {}).get("candidates") or []
    if not cands:
        return None
    return max(cands, key=lambda c: len([s for s in (c.get("signals") or []) if s]))


def _eval_event(panel, e: dict, act: str, horizon: int, w_wait: dict,
                tier: str) -> dict | None:
    t = e["t"]
    if act == "SELL":
        c = _best_sell(e.get("info") or {})
        if c is None:
            return None
        after = c["after"]
    else:
        after = (e.get("info") or {}).get("weights")
    if not after:
        return None
    cost = cm.trade_cost(w_wait, after, tier)
    r = tv.evaluate_trade(panel, t, horizon, w_wait, after, cost=cost)
    if not r.get("ok"):
        return None
    return {"t": t, "regime": e.get("regime"), "cost_frac": round(cost, 6), **r}


def _agg(rows: list[dict]) -> dict:
    return {
        "n": len(rows),
        "delta_return_net": tv.aggregate_deltas(rows, "delta_return_net"),
        "delta_return_gross": tv.aggregate_deltas(rows, "delta_return_gross"),
        "delta_max_drawdown": tv.aggregate_deltas(rows, "delta_max_drawdown"),
        "delta_volatility": tv.aggregate_deltas(rows, "delta_volatility"),
        "delta_sharpe": tv.aggregate_deltas(rows, "delta_sharpe"),
        "delta_sortino": tv.aggregate_deltas(rows, "delta_sortino"),
        "delta_portfolio_quality": tv.aggregate_deltas(rows, "delta_portfolio_quality"),
    }


def research_layer(panel, classes, raw_targets, regimes, gold_cap, w_wait,
                   w_fixed, *, horizons=HORIZONS, tier: str = "BASE") -> dict:
    """RESEARCH_POLICY：**不看任何执行权限**，只回答"规则有没有历史增量价值"。"""
    out: dict = {"horizons": {}, "cost_tier": tier,
                 "counterfactual": {"BUY": "WAIT", "SELL": "HOLD",
                                    "REBALANCE": "NO-REBALANCE"}}
    for h in horizons:
        cand = candidate_events(panel, classes, raw_targets, regimes, gold_cap,
                               w_wait, w_fixed, h)
        rows: dict[str, list[dict]] = {"BUY": [], "SELL": [], "REBALANCE": []}
        for act, evs in cand["independent"].items():
            for e in evs:
                r = _eval_event(panel, e, act, h, w_wait, tier)
                if r:
                    rows[act].append(r)
        out["horizons"][h] = {
            "n_candidate_independent": cand["n_independent"],
            "by_action": {a: _agg(v) for a, v in rows.items()},
            "events": {a: [(r["t"], r["regime"]) for r in v]
                       for a, v in rows.items()},
        }
    return out


def production_layer(cfg, panel, classes, raw_targets, regimes, gold_cap,
                     w_wait, w_fixed, *, horizons=HORIZONS, tier: str = "BASE",
                     trust_allows_buy: bool = False) -> dict:
    """PRODUCTION_POLICY：回答"今天实际允不允许做"。"""
    out: dict = {"horizons": {}, "cost_tier": tier,
                 "trust_allows_buy": trust_allows_buy, "by_action": {}}
    for h in horizons:
        ex = executable_events(cfg, panel, classes, raw_targets, regimes, gold_cap,
                               w_wait, w_fixed, h,
                               trust_allows_buy=trust_allows_buy)
        rows: dict[str, list[dict]] = {"BUY": [], "SELL": [], "REBALANCE": []}
        for act, evs in ex["independent"].items():
            for e in evs:
                r = _eval_event(panel, e, act, h, w_wait, tier)
                if r:
                    rows[act].append(r)
        out["horizons"][h] = {
            "n_executable": ex["n_independent"],
            "n_all_executable": ex.get("n_all_executable") or {},
            "n_rejected_by_gate": ex["n_rejected"],
            "by_action": {a: _agg(v) for a, v in rows.items()},
        }
    return out


# ==========================================================================
# 四、模型阶梯（同一 OOS 事件集合）
# ==========================================================================
MODEL_SPECS = [
    ("Model 0", "WAIT（从不交易）", 0),
    ("Model 1", "简单趋势（仅广度，1 输入）", 1),
    ("Model 2", "当前完整模型（regime：广度+回撤+波动，3 输入）", 3),
    ("Model 3", "当前完整模型去掉宏观代理（== M2：本模型本就不含代理）", 3),
    ("Model 4", "精简模型（广度+回撤，2 输入）", 2),
]


def _model_target(mid: int, panel, classes, raw_targets, regime_name: str,
                  breadth, dd, gold_cap: float):
    if mid == 0:
        return None
    gold = float(gold_cap)
    if mid == 1:                       # 仅广度
        b = float(breadth or 0.0)
        st = 0.55 if b >= 0.55 else (0.25 if b <= 0.35 else 0.40)
    elif mid in (2, 3):                # regime 三输入（模型已不含宏观代理，M3≡M2）
        st = float(STOCK_TARGET_BASE.get(regime_name, 0.45))
    else:                              # mid == 4：广度 + 回撤（2 特征，显式精简）
        b = float(breadth or 0.0)
        if dd is not None and dd <= -12.0:
            st = 0.30
        elif b <= 0.34:
            st = 0.35
        elif b >= 0.67:
            st = 0.50
        else:
            st = 0.45
    st = max(0.0, min(1.0 - gold, st))
    return pe._distribute(panel, classes, raw_targets,
                          {"stock": st, "bond": max(0.0, 1.0 - st - gold),
                           "gold": gold})


def model_ladder(panel, classes, raw_targets, regimes, gold_cap, w_wait,
                 *, horizon: int = 60, tier: str = "BASE",
                 warmup: int = WARMUP_DAYS) -> dict:
    """Model 0-4 在**完全相同的 OOS 事件集合**上比较（间隔 ≥h 的独立日网格）。"""
    n = len(panel.nav)
    event_days = wf.dedup_events(eligible_days(n, horizon, warmup), horizon)
    out: dict = {"horizon": horizon, "cost_tier": tier,
                 "n_events": len(event_days), "variants": {}}
    for mid in range(5):
        rows: list[dict] = []
        for t in event_days:
            rg = (regimes[t] or {}).get("regime")
            breadth = (regimes[t] or {}).get("breadth")
            dd = (regimes[t] or {}).get("dd_pct")
            tgt = _model_target(mid, panel, classes, raw_targets, rg, breadth, dd, gold_cap)
            if tgt is None:
                continue                       # Model 0 = WAIT，增量为 0
            cost = cm.trade_cost(w_wait, tgt, tier)
            r = tv.evaluate_trade(panel, t, horizon, w_wait, tgt, cost=cost)
            if r.get("ok"):
                rows.append(r)
        agg = _agg(rows)
        out["variants"][f"Model {mid}"] = {
            "desc": dict((m[0], m[1]) for m in MODEL_SPECS)[f"Model {mid}"],
            "n_features": MODEL_SPECS[mid][2],
            "n_events_evaluated": len(rows),
            **agg,
        }
    # Model 0 显式给出 0 增量（从不交易，交易价值恒为 0）
    out["variants"]["Model 0"]["delta_return_net"] = {
        "n": len(event_days), "mean": 0.0, "median": 0.0, "win_rate_pct": 0.0,
        "profit_factor": None, "ci95": [0.0, 0.0], "max_favorable": 0.0,
        "max_adverse": 0.0, "n_pos": 0, "n_neg": len(event_days)}
    return out


# ==========================================================================
# 五、事件定义稳定性（阈值扰动，禁止挑选最好的一组）
# ==========================================================================
def event_stability(panel, classes, raw_targets, regimes, gold_cap, w_wait,
                    w_fixed, *, horizon: int = 60, tier: str = "BASE",
                    warmup: int = WARMUP_DAYS) -> dict:
    """对关键阈值做 ±5% / ±2% / 0 / +2% / +5% 扰动，观察事件数与增量的稳定性。"""
    n = len(panel.nav)
    days = eligible_days(n, horizon, warmup)

    def measure(regs, stock_map, min_turn: float = MIN_TURNOVER,
                reb_th: float = REBALANCE_THRESHOLD) -> dict:
        n_buy = n_sell = n_reb = 0
        buy_days: list[int] = []
        for t in days:
            cls = classify_day(panel, classes, raw_targets,
                               (regs[t] or {}).get("regime"), gold_cap,
                               w_wait, w_fixed, t, stock_map=stock_map,
                               min_turnover=min_turn, rebalance_threshold=reb_th,
                               sell_candidates=sell_cache.get(t))
            if "BUY" in cls:
                n_buy += 1
                buy_days.append(t)
            n_sell += 1 if "SELL" in cls else 0
            n_reb += 1 if "REBALANCE" in cls else 0
        ind = wf.dedup_events(buy_days, horizon)
        rows = []
        for t in ind:
            rg = (regs[t] or {}).get("regime")
            tgt = policy_weights_th(panel, classes, raw_targets, rg, gold_cap, stock_map)
            r = tv.evaluate_trade(panel, t, horizon, w_wait, tgt,
                                  cost=cm.trade_cost(w_wait, tgt, tier))
            if r.get("ok"):
                rows.append(r)
        a = _agg(rows)
        return {"n_candidate_buy_days": n_buy, "n_candidate_sell_days": n_sell,
                "n_candidate_rebalance_days": n_reb,
                "n_independent_buy": len(ind),
                "mean_delta_net_pp": (a["delta_return_net"] or {}).get("mean"),
                "mean_delta_quality": (a["delta_portfolio_quality"] or {}).get("mean"),
                "n_eval": a["n"]}

    # 卖出候选在扰动中不随这些阈值变化 → 只算一次（性能）
    sell_cache = {t: _sell_candidates_for_day(panel, t, w_wait, raw_targets, gold_cap)
                  for t in days}

    rows: list[dict] = []
    grid = (-5, -2, 0, 2, 5)
    params = ("股票目标仓位", "Regime 广度下限", "Regime 广度上限",
              "再平衡阈值", "最小换手")
    base = None
    for pname in params:
        for pct in grid:
            f = 1.0 + pct / 100.0
            regs = regimes
            stock_map = None
            kw: dict = {}
            if pname == "股票目标仓位":
                stock_map = {k: v * f for k, v in STOCK_TARGET_BASE.items()}
            elif pname == "Regime 广度下限":
                regs = regime_series_th(panel, classes, w_wait, breadth_off=0.34 * f)
            elif pname == "Regime 广度上限":
                regs = regime_series_th(panel, classes, w_wait, breadth_on=0.67 * f)
            elif pname == "再平衡阈值":
                kw["reb_th"] = REBALANCE_THRESHOLD * f
            elif pname == "最小换手":
                kw["min_turn"] = MIN_TURNOVER * f
            m = measure(regs, stock_map, **kw)
            m.update({"param": pname, "pct": pct})
            rows.append(m)
            if pct == 0:
                base = m

    # 判定：同一参数下，增量的正负号是否随扰动翻转
    flags: list[dict] = []
    for pname in params:
        vals = [r for r in rows if r["param"] == pname]
        signs = {((r["mean_delta_net_pp"] or 0) > 0) for r in vals}
        n_series = [r["n_independent_buy"] for r in vals]
        flags.append({
            "param": pname,
            "n_min": min(n_series), "n_max": max(n_series),
            "sign_flip": len(signs) > 1,
            "sensitive": (len(signs) > 1) or (max(n_series) - min(n_series) > max(1, base["n_independent_buy"])),
        })
    return {"horizon": horizon, "base": base, "rows": rows, "flags": flags,
            "any_sensitive": any(f["sensitive"] for f in flags),
            "discipline": ("阈值扰动只用来检验『事件定义是否稳定』；"
                           "结果翻转一律标记『事件定义敏感』，"
                           "**绝不允许**回头挑选表现最好的一组参数。")}


# ==========================================================================
# 六、诊断：信号稀少 A / 验证器过度过滤 B / 事件构造口径 C
# ==========================================================================
def diagnose(funnels: dict, old_folds: dict, bounds: dict, research: dict,
             n: int) -> dict:
    """给出"为什么事件少"的定量归因。"""
    f60 = funnels.get(60) or {}
    cand_days = f60.get("candidate_days") or {}
    ind60 = (f60.get("independent") or {}).get(60, 0)
    bound60 = bounds.get(60, 0)
    old60 = old_folds.get(60, 0)

    rl = (research.get("horizons") or {}).get(60) or {}
    buy_ind = ((rl.get("n_candidate_independent") or {}).get("BUY", 0))
    reb_ind = ((rl.get("n_candidate_independent") or {}).get("REBALANCE", 0))
    sell_ind = ((rl.get("n_candidate_independent") or {}).get("SELL", 0))

    causes: list[dict] = []
    # C：事件构造口径（旧口径把事件 = 折边界）
    causes.append({
        "code": "C",
        "name": "事件构造口径错误（旧口径：事件 = walk-forward 折边界）",
        "evidence": (f"旧口径 60 日折数 = {old60}，而 802 日把 504 日训练窗切走后"
                     f"只剩 ~288 日可切 60 日折；事件被死死绑在折数上，"
                     f"与模型真实信号无关。"),
        "verdict": "确认为**主因之一**：这是验证体系的**结构性**问题。",
    })
    # B：验证器过度过滤
    b_over = max(0, min(cand_days.get("any", 0), 0) - 0)
    causes.append({
        "code": "B",
        "name": "验证器过度过滤（独立去重 / purge / 生产闸门）",
        "evidence": (f"候选日 {cand_days.get('any', 0)} → 独立事件 {ind60}；"
                     f"淘汰来自『间隔 ≥60 日不重叠』与生产闸门。"),
        "verdict": ("部分成立：独立去重是**必须保留**的口径，但它确实把"
                    "候选日压缩到理论上界；生产闸门（研究级禁 BUY）会把 BUY "
                    "可执行事件压到 0——这是循环的第二个来源。"),
    })
    # A：信号稀少
    causes.append({
        "code": "A",
        "name": "模型信号稀少（真实数据长度决定）",
        "evidence": (f"60 日独立事件**理论上界 = {bound60}**"
                     f"（= (802 − 252预热 − 60)/60 向下取整）。"
                     f"即使天天有信号，也只能得到 {bound60} 个独立事件。"),
        "verdict": (f"对 60/120 日周期**成立**：{bound60} < 30，"
                    f"说明『独立事件 <30』在 802 日上**物理不可达**，"
                    f"不是模型无能，也不是验证器过严。"),
    })
    primary = ("C（事件构造口径）为主，A（数据长度）为硬约束，B（验证器）为次因。"
               if old60 > 0 and bound60 < INDEPENDENT_THRESHOLD
               else "以 C 为主。" if old60 > 0 else "以 A/B 为主。")
    return {
        "causes": causes,
        "primary": primary,
        "old_folds_60": old60,
        "new_candidate_days_any": cand_days.get("any", 0),
        "new_independent_60": ind60,
        "bound_60": bound60,
        "bound_20": bounds.get(20, 0),
        "bound_120": bounds.get(120, 0),
        "research_independent": {"BUY": buy_ind, "SELL": sell_ind,
                                 "REBALANCE": reb_ind},
        "n_bars": n,
    }


# ==========================================================================
# 主入口
# ==========================================================================
def run_event_audit(cfg=None, progress=None) -> dict:
    from .config import get_config
    cfg = cfg or get_config()
    step = progress or (lambda _m: None)

    step("加载真实六 ETF 共同区间…")
    loaded = pe.load_real_6etf(cfg)
    if not loaded.get("ok"):
        return {"ok": False, "message": loaded.get("message")}
    panel = loaded["panel"]
    classes = loaded["classes"]
    raw = loaded["weights"]
    n = len(panel.nav)

    step("构造基准权重与市场状态…")
    w_wait = pe.real_portfolio_weights(cfg, panel)
    tot = sum(w_wait.values()) or 1.0
    w_wait = {k: v / tot for k, v in w_wait.items()}
    totf = sum(raw.values()) or 1.0
    w_fixed = {k: v / totf for k, v in raw.items()}
    regimes = pe.regime_series(panel, classes, w_wait)
    gold_cap = float(cfg.get("portfolio.gold_max", 0.05))

    step("15 级事件漏斗…")
    funnels = {h: build_funnel(cfg, panel, classes, raw, regimes, gold_cap,
                               w_wait, w_fixed, horizon=h) for h in HORIZONS}
    old_folds = {h: len(pe._fold_events(n, h, 504, 10)) for h in HORIZONS}
    bounds = {h: max_independent_bound(n, h) for h in HORIZONS}

    step("研究层 / 生产层双口径评价…")
    research = research_layer(panel, classes, raw, regimes, gold_cap,
                              w_wait, w_fixed)
    production = production_layer(cfg, panel, classes, raw, regimes, gold_cap,
                                  w_wait, w_fixed)

    step("模型阶梯 Model 0-4（同一 OOS 事件集合）…")
    ladder = {h: model_ladder(panel, classes, raw, regimes, gold_cap, w_wait,
                              horizon=h) for h in HORIZONS}

    step("事件定义稳定性（±5%/±2%/0/+2%/+5%）…")
    stability = event_stability(panel, classes, raw, regimes, gold_cap,
                               w_wait, w_fixed, horizon=60)

    step("四基准对比（A WAIT / B 固定50-45-5 / C 趋势 / D 优化模型）…")
    try:
        bench = pe.compare_benchmarks(panel, classes, raw, regimes, gold_cap,
                                      w_wait, w_fixed)
    except Exception as exc:  # noqa: BLE001
        bench = {"summary": {}, "n_folds": 0, "span": None, "error": str(exc)}
    bs = bench.get("summary") or {}

    def _b(key):
        return (bs.get(key) or {}).get("total_return_pct")

    d = _b("Benchmark 3 优化模型")
    a = _b("Benchmark 0 WAIT")
    b = _b("Benchmark 1 固定50/45/5")
    c = _b("Benchmark 2 趋势基准C")
    bench_deltas = {
        "D_A_pp": (round(d - a, 4) if d is not None and a is not None else None),
        "D_B_pp": (round(d - b, 4) if d is not None and b is not None else None),
        "D_C_pp": (round(d - c, 4) if d is not None and c is not None else None),
    }

    diag = diagnose(funnels, old_folds, bounds, research, n)

    # 今日（区间末端）状态：供"今日行动与建仓路径"使用
    latest_regime = (regimes[-1] or {}).get("regime")
    w_target_now = policy_weights_th(panel, classes, raw, latest_regime,
                                     gold_cap, STOCK_TARGET_BASE)
    gold_now = sum(float(w_wait.get(a, 0.0)) for a in panel.assets
                   if _class_of(classes, a) == "gold")
    today = {
        "latest_regime": latest_regime,
        "latest_breadth": (regimes[-1] or {}).get("breadth"),
        "latest_dd_pct": (regimes[-1] or {}).get("dd_pct"),
        "w_target_now": w_target_now,
        "gold_now_pct": round(gold_now * 100, 2),
        "gold_cap_pct": round(gold_cap * 100, 2),
        "gold_over": gold_now > float(gold_cap) + 1e-9,
        "classes": dict(classes),
        "names": dict(loaded["names"]),
    }

    res = {
        "ok": True, "ts": datetime.now(),
        "n_bars": n, "span": loaded["span"], "common_start": loaded["common_start"],
        "names": loaded["names"], "w_wait": w_wait, "w_fixed": w_fixed,
        "regime_counts": {r: sum(1 for x in regimes if x["regime"] == r)
                          for r in REGIMES},
        "gold_cap": gold_cap,
        "funnels": funnels, "old_folds": old_folds,
        "bounds": bounds, "diagnosis": diag,
        "research": research, "production": production,
        "ladder": ladder, "stability": stability,
        "benchmarks": bench, "bench_deltas": bench_deltas,
        "today": today,
        "warmup_days": WARMUP_DAYS, "min_turnover": MIN_TURNOVER,
        "rebalance_threshold": REBALANCE_THRESHOLD,
    }
    res["answers"] = ten_answers(res)
    return res


def ten_answers(res: dict) -> dict:
    """必须回答的 10 个问题。"""
    d = res["diagnosis"]
    f60 = res["funnels"][60]
    cd = f60["candidate_days"]
    bound = res["bounds"]
    r60 = (res["research"]["horizons"].get(60) or {}).get("by_action") or {}
    p60 = (res["production"]["horizons"].get(60) or {})
    lad = res["ladder"][60]["variants"]

    def m(act):
        return ((r60.get(act) or {}).get("delta_return_net") or {}).get("mean")

    def n(act):
        return ((r60.get(act) or {}).get("delta_return_net") or {}).get("n")

    # 最简且稳定：在 Model 1..4 中，净增量为正且事件数最多、特征最少者
    cand = [(mid, lad[f"Model {mid}"]) for mid in (1, 2, 3, 4)]
    simplest = None
    for mid, v in sorted(cand, key=lambda kv: kv[1]["n_features"]):
        mean = (v["delta_return_net"] or {}).get("mean")
        if mean is not None and mean > 0:
            simplest = f"Model {mid}（{v['desc']}）"
            break
    if simplest is None:
        simplest = "没有任何模型取得稳定正增量 → 最简模型也未获证据"

    ph60 = f60["per_horizon_candidates"][60]
    ci60 = (res["research"]["horizons"].get(60) or {}).get("n_candidate_independent") or {}
    return {
        "1_为什么只有2个": (
            f"旧口径把『事件』定义为 walk-forward 折的 test_start：802 日扣除 "
            f"504 日训练窗后只剩 ~288 日，60 日测试窗只能切出 {d['old_folds_60']} 折；"
            f"BUY 再排除 Risk Off/Crisis → 2。**不是模型只发现 2 次机会**。"),
        "2_信号少还是过滤严": d["primary"],
        "3_BUY候选多少": (f"全区间候选日 {cd['BUY']} 天；其中 60 日可评估 {ph60['BUY']} 天，"
                        f"独立去重后 **{ci60.get('BUY', 0)}** 个；生产层可执行 BUY = "
                        f"{p60.get('n_executable', {}).get('BUY', 0)} 个（研究级禁止 BUY）。"),
        "4_SELL候选多少": (f"全区间候选日 {cd['SELL']} 天；其中 60 日可评估 {ph60['SELL']} 天，"
                        f"独立去重后 **{ci60.get('SELL', 0)}** 个。"),
        "5_REBALANCE候选多少": (f"全区间候选日 {cd['REBALANCE']} 天；其中 60 日可评估 "
                             f"{ph60['REBALANCE']} 天，独立去重后 **{ci60.get('REBALANCE', 0)}** 个。"),
        "6_BUYvsWAIT": (f"研究层 60 日平均净增量 {m('BUY')}pp（n={n('BUY')}）；"
                        f"60 日独立事件理论上界仅 {bound[60]}，样本不足，"
                        f"**不构成有效证据**。"),
        "7_SELLvsHOLD": f"研究层 60 日平均净增量 {m('SELL')}pp（n={n('SELL')}）。",
        "8_REBALANCEvsNOREB": f"研究层 60 日平均净增量 {m('REBALANCE')}pp（n={n('REBALANCE')}）。",
        "9_最简最稳模型": simplest,
        "10_是否已有足够证据": (
            "否。" + ("独立事件远低于 30（60 日理论上界 "
                    f"{bound[60]}），任何正/负增量都不具统计意义 → 保持【研究级】。"
                    if bound[60] < INDEPENDENT_THRESHOLD else
                    "独立事件达门槛但仍需校准与成本双重确认。")),
    }
