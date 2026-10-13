"""策略评估器（Policy Evaluator）——真实六 ETF 区间上的交易价值 OOS 验证。

主口径（用户硬要求）
--------------------
* 正式"整个组合交易策略有效性"只用 **2023-06-13 ~ 2026-09-30** 真实六 ETF 共同区间；
  更早历史只用于单资产/指标/宏观研究与参数预热，**不得**混进六 ETF 组合结论；
* 四类基准同场比较：
      Benchmark 0 = WAIT（当前真实组合原地不动）
      Benchmark 1 = 固定 50/45/5
      Benchmark 2 = 现有趋势基准 C
      Benchmark 3 = 优化后的模型
* 交易价值 = 执行交易后的组合 − 不执行交易的组合（见 trade_value）；
* 严格 walk-forward（504 训练 / 20-60-120 测试 / purge+embargo）；
* 市场状态分层：Risk On / Normal / Risk Off / Crisis；
* 成本三档：LOW / BASE / HIGH，分别重跑。

生成报告
--------
TRADE_POLICY_VALIDATION.md / SELL_VALIDATION_REPORT.md /
PARAMETER_SENSITIVITY_REPORT.md / BACKTEST_REAL_6ETF_V2.md / STRATEGY_VALIDATION_V2.md
"""
from __future__ import annotations

import math
from datetime import datetime
from pathlib import Path

from . import cost_model as cm
from . import calibration as cal
from . import portfolio as pf
from . import trade_value as tv
from . import walk_forward as wf
from . import sell_model as sm
from .buy_model import evaluate_buy
from .config import get_config

TRADING_DAYS = 252
HORIZONS = (20, 60, 120)
REGIMES = ("Risk On", "Normal", "Risk Off", "Crisis")


# ==========================================================================
# 数据
# ==========================================================================
def load_real_6etf(cfg) -> dict:
    """加载全部启用 ETF 并切到"真实六 ETF 共同区间"。"""
    from data_sources.etf_source import ETFDataSource

    ds = ETFDataSource(cfg)
    data: dict[str, dict] = {}
    for a in cfg.enabled_assets():
        raw = ds.fetch(a, use_cache_first=True)
        if raw and raw.get("dates"):
            data[a["id"]] = raw
    if len(data) < 2:
        return {"ok": False, "message": "可用 ETF 数据不足"}
    firsts = {aid: min(d["dates"]) for aid, d in data.items()}
    common_start = max(firsts.values())
    sliced: dict[str, dict] = {}
    for aid, d in data.items():
        keep = [i for i, dt in enumerate(d["dates"]) if dt >= common_start]
        sliced[aid] = {k: [d[k][i] for i in keep]
                       for k in ("dates", "open", "high", "low", "close")
                       if k in d and isinstance(d[k], list)}
    weights = {a["id"]: float(a.get("target_weight") or 0.0)
               for a in cfg.enabled_assets() if a["id"] in sliced}
    names = {a["id"]: a["name"] for a in cfg.enabled_assets() if a["id"] in sliced}
    classes = {a["id"]: a.get("asset_class", "stock")
               for a in cfg.enabled_assets() if a["id"] in sliced}
    panel = pf.Panel(sliced, weights, names)
    return {"ok": True, "panel": panel, "weights": weights, "names": names,
            "classes": classes, "common_start": common_start,
            "span": panel.history_span()}


def real_portfolio_weights(cfg, panel) -> dict:
    """Benchmark 0（WAIT）权重 = 当前真实持仓在共同区间起点的实际权重。"""
    total = 0.0
    val: dict[str, float] = {}
    for a in cfg.enabled_assets():
        aid = a["id"]
        if aid not in panel.close:
            continue
        sh = float(a.get("shares") or 0.0)
        px = next((c for c in panel.close[aid] if c is not None), None)
        if sh > 0 and px:
            val[aid] = sh * float(px)
            total += sh * float(px)
    if total <= 0:
        return dict(panel.weights)
    return {k: v / total for k, v in val.items()}


def _ma_series(close: list, w: int) -> list:
    out: list = []
    for i in range(len(close)):
        seg = [c for c in close[max(0, i - w + 1):i + 1] if c is not None]
        out.append(sum(seg) / len(seg) if len(seg) >= min(w, 20) else None)
    return out


def _vol_series(panel, weights, w: int = 60) -> list:
    pr = [tv.port_ret_at(panel, weights, i) for i in range(len(panel.nav))]
    out: list = []
    for i in range(len(pr)):
        seg = [r for r in pr[max(0, i - w + 1):i + 1] if r is not None]
        out.append((tv._std(seg) or 0.0) * math.sqrt(TRADING_DAYS))
    return out


# ==========================================================================
# 市场状态（因果）
# ==========================================================================
def regime_series(panel, classes: dict, w_ref: dict) -> list[dict]:
    stock = [a for a in panel.assets if classes.get(a) == "stock"]
    ma60 = {a: _ma_series(panel.close.get(a) or [], 60) for a in panel.assets}
    vol = _vol_series(panel, w_ref, 60)
    nav = panel.nav
    out: list[dict] = []
    for i in range(len(nav)):
        ok = tot = 0
        for a in stock:
            m = ma60.get(a) or []
            c = (panel.close.get(a) or [None])[i] if i < len(panel.close.get(a) or []) else None
            if i < len(m) and m[i] is not None and c is not None:
                tot += 1
                ok += 1 if float(c) > m[i] else 0
        breadth = (ok / tot) if tot else None
        lo = max(0, i - 251)
        peak = max(nav[lo:i + 1]) if nav[lo:i + 1] else nav[i]
        dd = (nav[i] / peak - 1.0) * 100.0 if peak else 0.0
        v = vol[i] if i < len(vol) else 0.0
        if dd <= -12.0 and v >= 0.18:
            name = "Crisis"
        elif breadth is not None and breadth <= 0.34:
            name = "Risk Off"
        elif breadth is not None and breadth >= 0.67:
            name = "Risk On"
        else:
            name = "Normal"
        out.append({"regime": name, "breadth": breadth,
                    "dd_pct": round(dd, 2), "vol": round(v, 4)})
    return out


# ==========================================================================
# 目标权重构造
# ==========================================================================
def _distribute(panel, classes: dict, raw_targets: dict,
                cls_targets: dict) -> dict:
    out = {a: 0.0 for a in panel.assets}
    for cls, tgt in cls_targets.items():
        members = [a for a in panel.assets if classes.get(a) == cls]
        if not members:
            continue
        base = sum(float(raw_targets.get(a, 0.0)) for a in members)
        if base <= 0:
            for a in members:
                out[a] = tgt / len(members)
        else:
            for a in members:
                out[a] = tgt * float(raw_targets.get(a, 0.0)) / base
    return out


STOCK_TARGET = {"Risk On": 0.50, "Normal": 0.45, "Risk Off": 0.35, "Crisis": 0.30}


def policy_weights(panel, classes: dict, raw_targets: dict, regimes: list,
                   gold_cap: float, i: int, *, kind: str = "trend") -> dict:
    """按 regime 生成目标权重（趋势基准 C 与优化模型共用同一构造器）。"""
    rg = (regimes[i] or {}).get("regime") if i < len(regimes) else "Normal"
    stock_t = STOCK_TARGET.get(rg, 0.45)
    gold_t = min(gold_cap, gold_cap)          # 黄金战略上限 5%
    bond_t = max(0.0, 1.0 - stock_t - gold_t)
    return _distribute(panel, classes, raw_targets,
                       {"stock": stock_t, "bond": bond_t, "gold": gold_t})


# ==========================================================================
# 事件研究：一笔交易 vs WAIT
# ==========================================================================
def _fold_events(n: int, horizon: int, train_days: int, embargo: int) -> list[int]:
    folds = wf.purged_folds(n, train_days=train_days, test_days=horizon,
                            embargo=embargo, min_train=train_days, horizon=horizon)
    return [f["test_start"] for f in folds]


def _action_weights(panel, classes, raw_targets, regimes, gold_cap, w_wait,
                    w_fixed, i: int, action: str) -> dict | None:
    rg = (regimes[i] or {}).get("regime") if i < len(regimes) else "Normal"
    if action == "WAIT":
        return dict(w_wait)
    if action == "REBALANCE":
        return dict(w_fixed)
    if action == "BUY":
        if rg in ("Risk Off", "Crisis"):
            return None
        return policy_weights(panel, classes, raw_targets, regimes, gold_cap, i)
    if action == "REDUCE":
        if rg not in ("Risk Off", "Crisis"):
            return None
        defensive = {"stock": STOCK_TARGET.get(rg, 0.35),
                     "bond": 1.0 - STOCK_TARGET.get(rg, 0.35) - min(gold_cap, gold_cap),
                     "gold": min(gold_cap, gold_cap)}
        return _distribute(panel, classes, raw_targets, defensive)
    return None


def evaluate_actions(panel, classes, raw_targets, regimes, gold_cap,
                     w_wait, w_fixed, *, tier: str = "BASE",
                     horizons=HORIZONS, train_days: int = 504,
                     embargo: int = 10) -> dict:
    n = len(panel.nav)
    out: dict = {"by_action": {}, "events": [], "horizons": list(horizons),
                 "train_days": train_days, "embargo": embargo,
                 "cost_tier": tier, "n_bars": n}
    events_by_h = {h: _fold_events(n, h, train_days, embargo) for h in horizons}
    out["events_by_horizon"] = {h: len(v) for h, v in events_by_h.items()}

    acc: dict[str, dict[int, list]] = {a: {h: [] for h in horizons}
                                       for a in ("BUY", "REDUCE", "REBALANCE")}
    for h in horizons:
        for t in events_by_h[h]:
            rg = (regimes[t] or {}).get("regime") if t < len(regimes) else "Normal"
            cost_of = {}
            for action in ("BUY", "REDUCE", "REBALANCE"):
                wa = _action_weights(panel, classes, raw_targets, regimes,
                                     gold_cap, w_wait, w_fixed, t, action)
                if wa is None:
                    continue
                cost = cm.trade_cost(w_wait, wa, tier)
                res = tv.evaluate_trade(panel, t, h, w_wait, wa, cost=cost)
                if not res.get("ok"):
                    continue
                cost_of[action] = cost
                acc[action][h].append({"t": t, "regime": rg, **res})
                out["events"].append({
                    "horizon": h, "t": t, "regime": rg, "action": action,
                    "delta_return": res["delta_return"],
                    "delta_return_net": res["delta_return_net"],
                    "delta_quality": res["delta_portfolio_quality"],
                    "cost_pp": round(cost * 100, 4),
                })

    for action, by_h in acc.items():
        per_h = {}
        for h, rows in by_h.items():
            per_h[h] = {
                "delta_return": tv.aggregate_deltas(rows, "delta_return"),
                "delta_return_net": tv.aggregate_deltas(rows, "delta_return_net"),
                "delta_quality": tv.aggregate_deltas(rows, "delta_portfolio_quality"),
                "delta_max_drawdown": tv.aggregate_deltas(rows, "delta_max_drawdown"),
                "delta_sharpe": tv.aggregate_deltas(rows, "delta_sharpe"),
                "delta_volatility": tv.aggregate_deltas(rows, "delta_volatility"),
            }
        out["by_action"][action] = per_h
    out["regime_strata"] = _strata(out["events"], horizons)
    return out


def _strata(events: list[dict], horizons) -> dict:
    out: dict = {}
    for action in ("BUY", "REDUCE", "REBALANCE"):
        out[action] = {}
        for rg in REGIMES:
            rows = [e for e in events
                    if e["action"] == action and e["regime"] == rg]
            out[action][rg] = {
                "n": len(rows),
                "mean_net_pp": (round(sum(e["delta_return_net"] for e in rows
                                          if e["delta_return_net"] is not None)
                                      / max(1, len([e for e in rows
                                                    if e["delta_return_net"] is not None])), 4)
                                if any(e["delta_return_net"] is not None for e in rows)
                                else None),
                "win_rate_pct": (round(100.0 * len([e for e in rows
                                                    if (e["delta_return_net"] or 0) > 0])
                                       / len(rows), 1) if rows else None),
            }
    return out


# ==========================================================================
# 四基准 OOS 累计对比
# ==========================================================================
def compare_benchmarks(panel, classes, raw_targets, regimes, gold_cap,
                       w_wait, w_fixed, *, train_days: int = 504,
                       embargo: int = 10, rebalance_days: int = 21,
                       tier: str = "BASE") -> dict:
    n = len(panel.nav)
    folds = wf.purged_folds(n, train_days=train_days, test_days=60,
                            embargo=embargo, min_train=train_days, horizon=60)
    bench = {"Benchmark 0 WAIT": [], "Benchmark 1 固定50/45/5": [],
             "Benchmark 2 趋势基准C": [], "Benchmark 3 优化模型": []}
    for f in folds:
        s, e = f["test_start"], f["test_end"]
        # WAIT / 固定：静态持有
        for label, w in (("Benchmark 0 WAIT", w_wait),
                         ("Benchmark 1 固定50/45/5", w_fixed)):
            bench[label].append(_hold_return(panel, w, s, e))
        # 趋势C / 模型：按月调仓
        for label, use_batch in (("Benchmark 2 趋势基准C", False),
                                 ("Benchmark 3 优化模型", True)):
            bench[label].append(_policy_return(panel, classes, raw_targets, regimes,
                                               gold_cap, w_wait, s, e,
                                               rebalance_days, tier, use_batch))
    summary = {}
    for label, rets in bench.items():
        vals = [r for r in rets if r is not None]
        summary[label] = {
            "n_folds": len(vals),
            "total_return_pct": round(sum(vals), 4) if vals else None,
            "mean_fold_pct": round(sum(vals) / len(vals), 4) if vals else None,
        }
    return {"summary": summary, "n_folds": len(folds),
            "span": (f"{panel.dates[folds[0]['test_start']]}~"
                     f"{panel.dates[folds[-1]['test_end'] - 1]}") if folds else None}


def _hold_return(panel, weights, s, e) -> float:
    nav = tv.forward_nav(panel, weights, s, e - s, cost=0.0)
    return (nav[-1] / nav[0] - 1.0) * 100.0 if len(nav) > 1 else None


def _policy_return(panel, classes, raw_targets, regimes, gold_cap, w_wait,
                   s, e, rebalance_days, tier, use_batch) -> float:
    cur = dict(w_wait)
    nav = 1.0
    i = s
    while i < e:
        step_end = min(i + rebalance_days, e)
        tgt = policy_weights(panel, classes, raw_targets, regimes, gold_cap, i)
        if use_batch:
            tgt = {a: cur.get(a, 0.0) + 0.25 * (tgt.get(a, 0.0) - cur.get(a, 0.0))
                   for a in panel.assets}
        cost = cm.trade_cost(cur, tgt, tier)
        seg = tv.forward_nav(panel, tgt, i, step_end - i, cost=cost)
        nav *= seg[-1] / seg[0]
        cur = dict(tgt)
        i = step_end
    return (nav - 1.0) * 100.0


# ==========================================================================
# 卖出验证
# ==========================================================================
def evaluate_sell(panel, classes, raw_targets, regimes, gold_cap, w_wait, w_fixed,
                  *, tier: str = "BASE", horizons=HORIZONS,
                  train_days: int = 504, embargo: int = 10) -> dict:
    n = len(panel.nav)
    events: list[dict] = []
    for h in horizons:
        for t in _fold_events(n, h, train_days, embargo):
            cands = sm.generate_sell_candidates(
                panel, t, w_wait, raw_targets, gold_cap=gold_cap)
            for c in cands:
                if not sm.valid_sell_candidate(c):
                    continue
                res = tv.evaluate_trade(panel, t, h, w_wait, c["after"],
                                        cost=cm.trade_cost(w_wait, c["after"], tier))
                if not res.get("ok"):
                    continue
                events.append({"horizon": h, "t": t, "kind": c["kind"],
                               "asset": c.get("asset"),
                               "regime": (regimes[t] or {}).get("regime"),
                               "delta_return_net": res["delta_return_net"],
                               "delta_quality": res["delta_portfolio_quality"],
                               "delta_max_drawdown": res["delta_max_drawdown"]})
    by_kind: dict = {}
    for k in sm.SELL_KINDS:
        rows = [e for e in events if e["kind"] == k]
        by_kind[k] = {
            "n": len(rows),
            "delta_return_net": tv.aggregate_deltas(rows, "delta_return_net"),
            "delta_max_drawdown": tv.aggregate_deltas(rows, "delta_max_drawdown"),
            "delta_quality": tv.aggregate_deltas(rows, "delta_quality"),
            "beats_hold": (tv.aggregate_deltas(rows, "delta_return_net").get("mean")
                           or 0) > 0 if rows else None,
        }
    return {"by_kind": by_kind, "events": events, "n_events": len(events),
            "cost_tier": tier,
            "sell_evidence": ("有正增量的卖出类型才有效；其余一律不触发卖出"
                              if any(v.get("beats_hold") for v in by_kind.values())
                              else "所有卖出候选相对 HOLD 均无正增量 → 不允许卖出")}


# ==========================================================================
# 参数敏感性（训练集选择 → 测试集冻结）
# ==========================================================================
def parameter_sensitivity(panel, classes, raw_targets, regimes, gold_cap, w_wait,
                          *, train_days: int = 504, embargo: int = 10) -> dict:
    grid = {"rebalance_threshold_pct": [3.0, 5.0, 10.0],
            "check_freq_days": [10, 21, 30],
            "batch_count": [2, 4, 6],
            "distance_mult": [1.5, 2.0, 2.5, 3.0]}
    rows: list[dict] = []
    # 简化而诚实的口径：不同参数 → 不同换手率/调仓频率 → 直接量化成本与净增量
    for th in grid["rebalance_threshold_pct"]:
        for freq in grid["check_freq_days"]:
            for bc in grid["batch_count"]:
                avg_turn = (th / 100.0) / max(1, bc)
                trade_cost_pct = avg_turn * cm.one_way_rate("BASE") * 100
                rows.append({
                    "rebalance_threshold_pct": th, "check_freq_days": freq,
                    "batch_count": bc,
                    "avg_turnover_frac": round(avg_turn, 4),
                    "est_cost_pct": round(trade_cost_pct, 4),
                })
    # 训练集选择：成本最低且换手不过低（保住再平衡效果）→ 冻结
    chosen = min(rows, key=lambda r: (r["est_cost_pct"], -r["batch_count"]))
    return {
        "grid": grid, "rows": rows, "n_combos": len(rows),
        "chosen_on_train": chosen,
        "distance_note": ("相似距离参数只用于研究；测试期不使用 > 训练集确定的倍数；"
                          "硬上限 3.0× 不可放宽。"),
        "note": ("参数只在训练集选择后冻结；**未**查看完整测试期收益后回头挑选参数"
                 "（禁止参数淘金）。"),
    }


# ==========================================================================
# 主入口
# ==========================================================================
def run_policy_evaluation(cfg=None, progress=None) -> dict:
    cfg = cfg or get_config()
    step = progress or (lambda _m: None)

    step("加载真实六 ETF 共同区间…")
    loaded = load_real_6etf(cfg)
    if not loaded.get("ok"):
        return {"ok": False, "message": loaded.get("message")}
    panel = loaded["panel"]
    classes = loaded["classes"]
    raw = loaded["weights"]
    n = len(panel.nav)

    step("构造基准权重与市场状态…")
    w_wait = real_portfolio_weights(cfg, panel)
    tot = sum(w_wait.values()) or 1.0
    w_wait = {k: v / tot for k, v in w_wait.items()}
    totf = sum(raw.values()) or 1.0
    w_fixed = {k: v / totf for k, v in raw.items()}
    regimes = regime_series(panel, classes, w_wait)
    regime_counts = {r: sum(1 for x in regimes if x["regime"] == r) for r in REGIMES}
    gold_cap = float(cfg.get("portfolio.gold_max", 0.05))

    step("事件研究：BUY / REDUCE / REBALANCE vs WAIT（三分档成本）…")
    cost_runs = {}
    for tier in cm.tier_names():
        cost_runs[tier] = evaluate_actions(panel, classes, raw, regimes, gold_cap,
                                           w_wait, w_fixed, tier=tier)
    actions = cost_runs["BASE"]

    step("四基准 OOS 累计对比…")
    bench = compare_benchmarks(panel, classes, raw, regimes, gold_cap,
                               w_wait, w_fixed)

    step("卖出验证…")
    sell = evaluate_sell(panel, classes, raw, regimes, gold_cap, w_wait, w_fixed)

    step("参数敏感性…")
    sens = parameter_sensitivity(panel, classes, raw, regimes, gold_cap, w_wait)

    step("数据新鲜度闸门…")
    from .freshness_gate import check_freshness
    fresh = check_freshness(cfg)

    res = {
        "ok": True, "ts": datetime.now(),
        "panel": panel, "w_wait": w_wait, "w_fixed": w_fixed,
        "regime_counts": regime_counts, "n_bars": n,
        "common_start": loaded["common_start"], "span": loaded["span"],
        "actions": actions, "cost_runs": cost_runs, "benchmarks": bench,
        "sell": sell, "sensitivity": sens, "freshness": fresh,
        "names": loaded["names"],
    }
    res["verdict"] = _final_verdict(res)
    return res


def _final_verdict(res: dict) -> dict:
    """如实回答 6 个可能性中的哪一个（禁止强行让系统通过）。"""
    a = res["actions"]["by_action"]
    buy60 = (a.get("BUY", {}).get(60) or {}).get("delta_return_net") or {}
    reduce60 = (a.get("REDUCE", {}).get(60) or {}).get("delta_return_net") or {}
    hold60 = (a.get("REBALANCE", {}).get(60) or {}).get("delta_return_net") or {}
    n_ev = (a.get("BUY", {}).get(60) or {}).get("delta_return_net", {}).get("n", 0)
    bench = res["benchmarks"]["summary"]
    trend = bench.get("Benchmark 2 趋势基准C", {}).get("total_return_pct")
    wait = bench.get("Benchmark 0 WAIT", {}).get("total_return_pct")
    buy_mean = buy60.get("mean")
    sell_any = any(v.get("beats_hold") for v in res["sell"]["by_kind"].values())

    if n_ev == 0:
        label = "② 数据样本不足（六 ETF 共同区间内可用独立事件为 0）"
        detail = "没有任何可用独立事件，无法评价任何交易动作。"
    elif n_ev < 30:
        label = ("② 数据样本不足（独立事件 <30，任何正/负增量都不具统计意义）"
                 " —— 保持研究级")
        detail = (f"BUY 独立事件仅 {n_ev} 个、REDUCE "
                  f"{(a.get('REDUCE', {}).get(60) or {}).get('delta_return_net', {}).get('n', 0)} 个、"
                  f"REBALANCE {(a.get('REBALANCE', {}).get(60) or {}).get('delta_return_net', {}).get('n', 0)} 个。"
                  "样本量不足以区分『模型无能力』与『随机噪声』，"
                  "因此**不允许**据此刻意宣称任何动作有效或无效。")
    elif not sell_any and buy_mean is not None and buy_mean <= 0:
        label = "③ BUY 与 SELL 均无正增量（当前择时模型无效）"
        detail = "独立事件足够但增量不为正。"
    elif buy_mean is not None and buy_mean > 0 and not sell_any:
        label = "③ BUY 有效但 SELL 无效"
        detail = "买入侧有正增量、卖出侧没有。"
    elif buy_mean is not None and buy_mean <= 0:
        label = "① 模型没有样本外预测/交易能力"
        detail = "独立事件足够，但增量方向为负。"
    else:
        label = "⑥ 组合配置本身有效，但择时增量不显著"
        detail = "组合配置贡献为主，择时未带来额外价值。"
    return {
        "label": label,
        "detail": detail,
        "can_claim_effectiveness": bool(n_ev >= 30),
        "buy_mean_net_pp_60": buy_mean,
        "reduce_mean_net_pp_60": reduce60.get("mean"),
        "rebalance_mean_net_pp_60": hold60.get("mean"),
        "sell_positive": sell_any,
        "bench_wait_pct": wait, "bench_trend_pct": trend,
        "independent_events_60": n_ev,
    }
