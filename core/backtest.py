"""组合回测与 Walk-Forward 样本外验证（严格无未来函数）。

关键纪律
--------
1. 每个再平衡日 t 的决策**只能**使用 panel 中 <= t 的数据；
2. 指标序列由 indicators.py 逐 bar 因果计算；"组合 60 日概率"序列也是
   逐点因果生成（每个 t 的候选池限定 s + horizon <= t），再 ffill 供模拟使用，
   这样模拟循环里没有任何"整段回看"的操作；
3. 交易成本按换手率计费，默认 0.20%；
4. Walk-Forward：把评估期切成若干测试块，每块起点的信号只用该点之前的数据
   生成，再用其后的真实收益评价——这才是样本外。

对比的策略（对应需求六十一）
    A 固定 50/45/5 配置
    B 动态配置（按市场状态调整股债比例，一次性调仓）
    C 动态配置 + 分批买入（每次只移动缺口的 1/4）
    D 动态配置 + 一次买满
    E 不同再平衡阈值（3% / 5% / 10%）
"""
from __future__ import annotations

import math
from datetime import datetime
from pathlib import Path

from . import indicators, optimizer, scoring
from . import portfolio as pf
from .config import get_config
from .histstats import SimilarStateEngine, build_feature_series

TRADING_DAYS = 252
HORIZON = 60


# --------------------------------------------------------------------------
# 基础
# --------------------------------------------------------------------------
def _mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def _std(xs):
    if len(xs) < 2:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def _load_data(cfg, progress=None):
    """一次性加载全部 ETF 数据与指标（指标本身是因果的）。"""
    from data_sources.etf_source import ETFDataSource

    ds = ETFDataSource(cfg)
    data: dict[str, dict] = {}
    inds: dict[str, dict] = {}
    for a in cfg.enabled_assets():
        if progress:
            progress(f"回测：加载并计算 {a['name']} …")
        raw = ds.fetch(a, use_cache_first=True)
        if not raw:
            continue
        data[a["id"]] = {"dates": raw["dates"], "close": raw["close"]}
        inds[a["id"]] = indicators.compute_all(
            raw["dates"], raw["open"], raw["high"], raw["low"], raw["close"],
            cfg, a.get("asset_class", "stock"))
    return data, inds


def _to_panel_dates(ind: dict, series: list, panel_dates: list[str]) -> list:
    m = {d: v for d, v in zip(ind["dates"], series) if v is not None}
    out: list = []
    last = None
    for d in panel_dates:
        v = m.get(d)
        if v is not None:
            last = float(v)
        out.append(last)
    return out


def _causal_prob_series(panel: pf.Panel, cfg, start: int, step: int = 21,
                        extra: dict[str, dict[str, list]] | None = None) -> list:
    """逐点因果地生成"组合未来60日历史盈利概率"序列（其余点 ffill）。"""
    feats = pf.build_portfolio_features(panel, {}, {}, cfg)
    for name, by_asset in (extra or {}).items():
        feats[name] = pf.weighted_avg(panel, by_asset)
    weights = dict(cfg.get("hist.feature_weights", {}))
    weights.update({"corr60": 0.8, "navrel1y": 1.0})
    weights = {k: v for k, v in weights.items() if k in feats}
    engine = SimilarStateEngine(feats, weights, cfg)
    n = len(panel.nav)
    out: list = [None] * n
    last = None
    for t in range(start, n):
        if (t - start) % step != 0:
            out[t] = last
            continue
        st = engine.stats_at(t, panel.nav, HORIZON, cfg.trade_cost())
        last = st.get("win_rate_pct")
        out[t] = last
    return out


# --------------------------------------------------------------------------
# 目标权重函数（只依赖 t 及以前的数据）
# --------------------------------------------------------------------------
class TargetBuilder:
    def __init__(self, cfg, inds, panel, trend_series, pos_series,
                 prob_series, stock_trend_series):
        self.cfg = cfg
        self.inds = inds
        self.panel = panel
        self.trend = trend_series
        self.position = pos_series
        self.prob60 = prob_series
        self.stock_trend = stock_trend_series
        self.fixed = {a["id"]: float(a.get("target_weight") or 0.0)
                      for a in cfg.enabled_assets()}
        self.cls = {a["id"]: a.get("asset_class") for a in cfg.enabled_assets()}

    def const(self, i: int, ctx: dict) -> dict:
        return dict(self.fixed)

    def dynamic(self, use_signal: bool):
        def fn(i: int, ctx: dict) -> dict:
            nav = ctx["nav"]
            rets = [nav[k] / nav[k - 1] - 1 for k in range(1, len(nav)) if nav[k - 1]]
            seg = nav[max(0, len(nav) - 252):]
            dd1 = (seg[-1] / max(seg) - 1.0) * 100 if seg and max(seg) else 0.0
            r20 = rets[-20:]
            vol = _std(r20) * math.sqrt(TRADING_DAYS) * 100 if len(r20) > 5 else 12.0
            st_trend = self.stock_trend[i] if i < len(self.stock_trend) else None
            risk_score = max(0.0, min(100.0,
                (-dd1) / 22 * 100 * 0.40
                + (vol - 4) / 22 * 100 * 0.25
                + (100 - (st_trend if st_trend is not None else 55)) * 0.25
                + (100 - (self.prob60[i] if self.prob60[i] is not None else 55)) * 0.10))
            state = ("Risk On" if risk_score < 30 else "Normal" if risk_score < 50
                     else "Risk Off" if risk_score < 70 else "Crisis")
            grade = "C"
            if use_signal and self.prob60[i] is not None:
                p = self.prob60[i]
                grade = ("S" if p >= 70 else "A" if p >= 65 else "B" if p >= 58
                         else "C" if p >= 50 else "D")
            targets = optimizer.dynamic_targets(state, grade, self.cfg)
            out: dict[str, float] = {}
            for cls in ("bond", "stock", "gold"):
                ids = [k for k, v in self.cls.items() if v == cls]
                if not ids:
                    continue
                base = sum(self.fixed.get(k, 0.0) for k in ids) or 1.0
                raw = {}
                for k in ids:
                    adj = 1.0
                    ts = self.trend.get(k)
                    ps = self.position.get(k)
                    if ts and i < len(ts) and ts[i] is not None:
                        adj += (ts[i] - 50) / 50 * 0.10
                    if ps and i < len(ps) and ps[i] is not None:
                        adj += (50 - ps[i]) / 50 * 0.10
                    raw[k] = max(0.02, (self.fixed.get(k, 0.0) / base) * adj)
                tot = sum(raw.values())
                for k, v in raw.items():
                    out[k] = v / tot * targets.get(cls, 0.0)
            tot = sum(out.values())
            return {k: v / tot for k, v in out.items()} if tot > 0 else dict(self.fixed)
        return fn


# --------------------------------------------------------------------------
# 模拟
# --------------------------------------------------------------------------
def simulate(panel: pf.Panel, builder: TargetBuilder, target_fn, cfg, *,
             rebalance_days: int = 21, threshold_pct: float = 5.0,
             batch: bool = False, batch_count: int = 4, start_idx: int = 300,
             label: str = "strategy") -> dict:
    # §37/§38：T日收盘出信号、T+1开盘成交；成本拆分为佣金+滑点，全部参数化
    cost_cfg = cfg.get("backtest.costs", {}) or {}
    default_cost = float(cfg.trade_cost() or 0.002)
    commission = float(cost_cfg.get("commission", default_cost / 2))
    slippage = float(cost_cfg.get("slippage", default_cost / 2))
    cost = commission + slippage
    n = len(panel.dates)
    start = max(start_idx, 1)

    base = {k: v for k, v in panel.weights.items() if v > 0}
    if not base:
        base = {a: 1.0 / len(panel.assets) for a in panel.assets}
    tot = sum(base.values()) or 1.0
    values = {a: base.get(a, 0.0) / tot for a in panel.assets}

    nav_curve = [1.0]
    rets: list[float] = []
    turnover_total = 0.0
    rebalance_log: list[dict] = []
    signal_idx: list[int] = []
    pending: dict | None = None          # §37：T+1 开盘待执行的目标权重

    for i in range(start, n):
        # ---- 1) 若有 T-1 日收盘信号：今日 T+1 开盘按开盘价成交 ----
        if pending is not None:
            tgt = pending
            pending = None
            # 开盘跳空收益归旧权重，开盘→收盘收益归新权重，成本按换手扣除
            r_exec = 0.0
            eod: dict[str, float] = {}
            for a in set(values) | set(tgt):
                ra = panel.ret[a][i]
                gap = panel.open_gap.get(a, [None] * n)[i] if panel.open_gap else None
                if ra is None:
                    ra = 0.0
                if gap is None:
                    gap = 0.0          # 缺开盘价：退化按收盘价成交（不再细分跳空）
                intra = (1.0 + ra) / (1.0 + gap) - 1.0
                r_exec += values.get(a, 0.0) * gap + tgt.get(a, 0.0) * intra
                eod[a] = tgt.get(a, 0.0) * (1.0 + intra)
            turn = sum(abs(tgt.get(a, 0.0) - values.get(a, 0.0))
                       for a in set(values) | set(tgt))
            turnover_total += turn
            r_exec -= turn * cost
            st = sum(eod.values())
            values = ({k: v / st for k, v in eod.items()} if st > 0
                      else {k: max(0.0, v) for k, v in tgt.items()})
            nav_curve.append(nav_curve[-1] * (1 + r_exec))
            rets.append(r_exec)
            continue

        # ---- 2) 普通日：收盘价收益累积，权重随之漂移 ----
        r = 0.0
        for a in panel.assets:
            ra = panel.ret[a][i]
            if ra is None:
                continue
            r += values.get(a, 0.0) * ra
            values[a] = values.get(a, 0.0) * (1 + ra)
        s = sum(values.values())
        if s > 0:
            values = {a: v / s for a, v in values.items()}
        nav_curve.append(nav_curve[-1] * (1 + r))
        rets.append(r)

        # ---- 3) T 日收盘检查信号（只记录，T+1 开盘执行；无未来函数） ----
        if (i - start) % rebalance_days == 0:
            ctx = {"nav": nav_curve, "i": i}
            tgt = target_fn(i, ctx)
            tt = sum(tgt.values())
            if tt > 0:
                tgt = {k: v / tt for k, v in tgt.items()}
            keys = set(values) | set(tgt)
            drift = max((abs(values.get(k, 0.0) - tgt.get(k, 0.0)) * 100 for k in keys),
                        default=0.0)
            if i == start or drift >= threshold_pct:
                if batch and i != start:
                    move = 1.0 / max(1, batch_count)
                    new = {k: values.get(k, 0.0)
                           + (tgt.get(k, 0.0) - values.get(k, 0.0)) * move for k in keys}
                else:
                    new = {k: tgt.get(k, 0.0) for k in keys}
                pending = new
                signal_idx.append(i)
                if i + 1 < n:
                    rebalance_log.append({"signal_date": panel.dates[i],
                                          "exec_date": panel.dates[i + 1],
                                          "drift_pct": round(drift, 2)})

    m = _metrics(nav_curve, rets, float(cfg.get("risk.risk_free_rate", 0.02)), label)
    m["nav"] = nav_curve
    m["dates"] = panel.dates[start:]
    m["rebalances"] = len(rebalance_log)
    m["rebalance_log"] = rebalance_log[-12:]
    m["total_turnover_pct"] = round(turnover_total * 100, 1)
    m["cost_drag_pct"] = round(turnover_total * cost * 100, 2)
    m["commission_drag_pct"] = round(turnover_total * commission * 100, 2)
    m["slippage_drag_pct"] = round(turnover_total * slippage * 100, 2)
    m["execution"] = {"model": "T+1 开盘成交", "commission": commission,
                      "slippage": slippage}
    m["yearly"] = _yearly(nav_curve[1:], panel.dates[start:])
    pos = list(m["yearly"].values())
    m["positive_year_ratio_pct"] = (
        round(sum(1 for v in pos if v > 0) / len(pos) * 100, 1) if pos else None)
    local_sig = [i - start for i in signal_idx]
    m["forward"] = {h: _forward_win_rate(nav_curve[1:], local_sig, h)
                    for h in (20, 60, 120)}
    return m


def _metrics(nav: list[float], rets: list[float], rf: float, label: str) -> dict:
    out: dict = {"name": label, "bars": len(rets)}
    if len(nav) < 30 or not rets:
        out["available"] = False
        return out
    out["available"] = True
    n = len(rets)
    out["cumulative_return_pct"] = round((nav[-1] / nav[0] - 1) * 100, 2)
    years = n / TRADING_DAYS
    ann = (nav[-1] / nav[0]) ** (1 / years) - 1 if years > 0 and nav[-1] > 0 else None
    out["annual_return_pct"] = round(ann * 100, 2) if ann is not None else None
    vol = _std(rets) * math.sqrt(TRADING_DAYS)
    out["annual_vol_pct"] = round(vol * 100, 2)
    out["max_drawdown_pct"] = round(pf.max_drawdown_of(nav), 2)
    down = [r for r in rets if r < 0]
    dn = _std(down) * math.sqrt(TRADING_DAYS) if len(down) > 2 else None
    out["sharpe"] = round((ann - rf) / vol, 2) if (ann is not None and vol > 1e-9) else None
    out["sortino"] = round((ann - rf) / dn, 2) if (ann is not None and dn and dn > 1e-9) else None
    out["calmar"] = (round(ann / abs(out["max_drawdown_pct"] / 100), 2)
                     if (ann is not None and out["max_drawdown_pct"]) else None)
    streak = cur = 0
    for r in rets:
        cur = cur + 1 if r < 0 else 0
        streak = max(streak, cur)
    out["max_losing_streak"] = streak
    uw = pf._underwater(nav)
    out["recovery_days"] = uw["longest_days"]
    out["recovered"] = uw["recovered"]
    return out


def _yearly(nav: list[float], dates: list[str]) -> dict[str, float]:
    out: dict[str, float] = {}
    m = len(nav)
    for y in sorted({d[:4] for d in dates}):
        idx = [k for k, d in enumerate(dates) if d[:4] == y and k < m]
        if len(idx) < 20:
            continue
        v0 = nav[idx[0] - 1] if idx[0] > 0 else nav[0]
        out[y] = round((nav[idx[-1]] / v0 - 1) * 100, 2) if v0 else 0.0
    return out


def _forward_win_rate(nav: list[float], signal_days: list[int], horizon: int) -> dict:
    rets = []
    for t in signal_days:
        if t + horizon >= len(nav) or not nav[t]:
            continue
        rets.append(nav[t + horizon] / nav[t] - 1.0)
    if not rets:
        return {"samples": 0, "win_rate_pct": None, "avg_return_pct": None}
    return {
        "samples": len(rets),
        "win_rate_pct": round(sum(1 for r in rets if r > 0) / len(rets) * 100, 1),
        "avg_return_pct": round(_mean(rets) * 100, 2),
        "median_return_pct": round(sorted(rets)[len(rets) // 2] * 100, 2),
        "max_loss_pct": round(min(rets) * 100, 2),
    }


# --------------------------------------------------------------------------
# Walk-Forward
# --------------------------------------------------------------------------
def walk_forward(panel: pf.Panel, cfg, prob_series: list, progress=None) -> dict:
    wf = cfg.get("backtest.walk_forward", {"train_days": 756, "test_days": 126,
                                           "step_days": 126})
    train_days = int(wf.get("train_days", 756))
    step = int(wf.get("step_days", 126))
    nav = panel.nav
    n = len(nav)

    blocks: list[dict] = []
    t = max(train_days, 300)
    while t + HORIZON < n:
        if progress:
            progress(f"Walk-Forward 测试块 {panel.dates[t]} …")
        prob = prob_series[t] if t < len(prob_series) else None
        grade = ("S" if (prob or 0) >= 70 else "A" if (prob or 0) >= 65
                 else "B" if (prob or 0) >= 58 else "C" if (prob or 0) >= 50 else "D")
        f60 = nav[min(t + 60, n - 1)] / nav[t] - 1.0 if nav[t] else None
        f120 = nav[min(t + 120, n - 1)] / nav[t] - 1.0 if nav[t] else None
        blocks.append({
            "date": panel.dates[t], "prob60": prob, "grade": grade,
            "realized_60_pct": round(f60 * 100, 2) if f60 is not None else None,
            "realized_120_pct": round(f120 * 100, 2) if f120 is not None else None,
        })
        t += step

    buy = [b for b in blocks if b["grade"] in ("S", "A", "B")]
    wait = [b for b in blocks if b["grade"] in ("C", "D")]

    # ---- 概率校准度检验：预测概率 vs 真实结局 --------------------------
    # 对每个 Walk-Forward 测试块：系统给出的 prob60 是"60 日后上涨的概率"，
    # 用它之后 60 日的真实涨跌（0/1）检验预测是否可信。
    # Brier 仅作为概率误差指标展示；正式校准必须与同一测试集的
    # Naive Base-rate 基线比较（见 STRATEGY_VALIDATION_REPORT），
    # 禁止使用 Brier<0.25 作为"校准良好"判据。此处同时输出分桶频率表。
    cal_pairs = [(b["prob60"] / 100.0, 1.0 if (b.get("realized_60_pct") or 0) > 0 else 0.0)
                 for b in blocks
                 if b.get("prob60") is not None and b.get("realized_60_pct") is not None]
    calibration: dict = {"samples": len(cal_pairs), "brier": None, "buckets": []}
    if cal_pairs:
        calibration["brier"] = round(_mean(
            [(p - y) ** 2 for p, y in cal_pairs]), 4)
        edges = [(0.0, 0.5, "<50%"), (0.5, 0.6, "50-60%"),
                 (0.6, 0.7, "60-70%"), (0.7, 0.8, "70-80%"), (0.8, 1.01, "≥80%")]
        for lo, hi, label in edges:
            ys = [y for p, y in cal_pairs if lo <= p < hi]
            ps = [p for p, y in cal_pairs if lo <= p < hi]
            if ys:
                calibration["buckets"].append({
                    "bucket": label,
                    "samples": len(ys),
                    "avg_predicted_pct": round(_mean(ps) * 100, 1),
                    "realized_win_rate_pct": round(_mean(ys) * 100, 1),
                    "gap_pct": round((_mean(ys) - _mean(ps)) * 100, 1),
                })

    def agg(rows, key):
        vals = [r[key] for r in rows if r.get(key) is not None]
        if not vals:
            return {"samples": 0, "win_rate_pct": None, "avg_pct": None,
                    "max_loss_pct": None}
        return {
            "samples": len(vals),
            "win_rate_pct": round(sum(1 for v in vals if v > 0) / len(vals) * 100, 1),
            "avg_pct": round(_mean(vals), 2),
            "max_loss_pct": round(min(vals), 2),
        }

    return {
        "blocks": blocks,
        "calibration": calibration,
        "all_blocks": {"n60": agg(blocks, "realized_60_pct"),
                       "n120": agg(blocks, "realized_120_pct")},
        "buy_signal": {"n60": agg(buy, "realized_60_pct"),
                       "n120": agg(buy, "realized_120_pct")},
        "wait_signal": {"n60": agg(wait, "realized_60_pct"),
                        "n120": agg(wait, "realized_120_pct")},
        "config": {"train_days": train_days, "step_days": step},
        "note": ("每个测试块起点的信号只用该点及以前的数据生成，"
                 "再用之后 60/120 交易日的真实收益评价——不存在未来函数。"),
    }


# --------------------------------------------------------------------------
# 主入口
# --------------------------------------------------------------------------
def _core_backtest(panel, inds, cfg, progress=None, start: int = 300) -> dict:
    """在给定 panel 上跑完整策略对比 + Walk-Forward（run_backtest 的核心）。"""

    def step(msg):
        if progress:
            progress(msg)

    weights = panel.weights
    step("预计算逐日因果的各类评分序列…")
    trend_series, pos_series = {}, {}
    bands = cfg.get("classification.relpos_bands", [20, 35, 65, 80])
    for aid, ind in inds.items():
        trend_series[aid] = _to_panel_dates(ind, [scoring.trend_score(ind, i)
                                                  for i in range(ind["n"])], panel.dates)
        pos_series[aid] = _to_panel_dates(ind, [scoring.position_score(ind, bands, i)
                                                for i in range(ind["n"])], panel.dates)

    stock_ids = [a["id"] for a in cfg.enabled_assets()
                 if a.get("asset_class") == "stock" and a["id"] in panel.close]
    sw = sum(weights.get(i, 0.0) for i in stock_ids) or 1.0
    stock_trend = []
    for k in range(len(panel.dates)):
        vals = [(trend_series[i][k], weights.get(i, 0.0) / sw) for i in stock_ids
                if trend_series.get(i) and trend_series[i][k] is not None]
        stock_trend.append(_mean([v for v, _ in vals]) if vals else None)

    step("预计算组合 60 日历史盈利概率序列（逐点因果）…")
    prob_series = _causal_prob_series(panel, cfg, start, step=21,
                                      extra={"trend": trend_series,
                                             "position": pos_series})

    builder = TargetBuilder(cfg, inds, panel, trend_series, pos_series,
                            prob_series, stock_trend)

    bt = cfg.get("backtest", {})
    freq = int(bt.get("rebalance_freq_days", 21))
    batch_count = int(cfg.get("portfolio.batch_count", 4))

    specs = [
        (f"A 固定50/45/5（{freq}日检查，阈值5%）", builder.const, False),
        ("B 动态配置（按市场状态调整股债）", builder.dynamic(False), False),
        ("C 动态配置 + 分批买入（每次移动缺口1/4）", builder.dynamic(True), True),
        ("D 动态配置 + 一次买满", builder.dynamic(True), False),
    ]
    results = []
    for label, fn, batch in specs:
        step(f"回测：{label} …")
        results.append(simulate(panel, builder, fn, cfg, rebalance_days=freq,
                                threshold_pct=5.0, batch=batch,
                                batch_count=batch_count, start_idx=start, label=label))

    thresholds = []
    for th in (3.0, 5.0, 10.0):
        step(f"回测：再平衡阈值 {th:.0f}% …")
        thresholds.append(simulate(
            panel, builder, builder.const, cfg, rebalance_days=freq,
            threshold_pct=th, batch=False, batch_count=batch_count, start_idx=start,
            label=f"E 固定配置 · 再平衡阈值 {th:.0f}%"))

    wf = walk_forward(panel, cfg, prob_series, progress)
    return {
        "strategies": results,
        "thresholds": thresholds,
        "walk_forward": wf,
        "panel": panel,
        "rebalance_freq_days": freq,
    }


def run_backtest(cfg=None, progress=None) -> dict:
    cfg = cfg or get_config()

    def step(msg):
        if progress:
            progress(msg)

    data, inds = _load_data(cfg, progress)
    if len(data) < 2:
        return {"ok": False, "message": "可用 ETF 数据不足，无法回测"}

    weights = {a["id"]: float(a.get("target_weight") or 0.0)
               for a in cfg.enabled_assets() if a["id"] in data}
    names = {a["id"]: a["name"] for a in cfg.enabled_assets() if a["id"] in data}
    panel = pf.Panel(data, weights, names)
    if len(panel.dates) < 400:
        return {"ok": False,
                "message": f"可回测历史仅 {len(panel.dates)} 个交易日，不足以回测"}

    core = _core_backtest(panel, inds, cfg, progress)
    freq = core["rebalance_freq_days"]

    # ---- §35/§36：真实六 ETF 共同区间（最晚上市的 ETF 起才全部有真实历史） ----
    first_dates = {aid: min(d["dates"]) for aid, d in data.items()
                   if d.get("dates")}
    common_start = max(first_dates.values()) if first_dates else None
    real_sec: dict = {"common_start": common_start, "available": False}
    if common_start and common_start > panel.dates[0]:
        step(f"真实六 ETF 区间回测（自 {common_start}）…")
        sliced = {}
        for aid, d in data.items():
            keep = [i for i, dt in enumerate(d["dates"]) if dt >= common_start]
            sliced[aid] = {k: [d[k][i] for i in keep]
                           for k in ("dates", "open", "high", "low", "close",
                                     "volume", "turnover") if k in d}
        real_panel = pf.Panel(sliced, weights, names)
        if len(real_panel.dates) >= 400:
            real_core = _core_backtest(real_panel, inds, cfg, progress, start=300)
            real_sec.update({
                "available": True,
                "span": real_panel.history_span(),
                "strategies": real_core["strategies"],
                "thresholds": real_core["thresholds"],
                "walk_forward": real_core["walk_forward"],
                "rebalance_freq_days": real_core["rebalance_freq_days"],
            })
        else:
            real_sec["message"] = (f"共同区间仅 {len(real_panel.dates)} 个交易日，"
                                   "不足以回测")
    return {
        "ok": True,
        "ts": datetime.now(),
        "span": panel.history_span(),
        "rebalance_freq_days": freq,
        "strategies": core["strategies"],
        "thresholds": core["thresholds"],
        "walk_forward": core["walk_forward"],
        "panel": panel,
        "asset_names": names,
        "asset_ids": list(data),
        "first_dates": first_dates,
        "real_6etf": real_sec,
    }


def render_real_6etf_md(res: dict) -> str:
    """§35/§36：真实六 ETF 共同区间回测报告（BACKTEST_REAL_6ETF.md）。"""
    L: list[str] = []
    L.append("# 真实六 ETF 组合回测报告（BACKTEST_REAL_6ETF）")
    L.append("")
    real = res.get("real_6etf") or {}
    if not res.get("ok"):
        return "# 真实六 ETF 组合回测报告\n\n回测无法执行。\n"
    L.append(f"生成时间：{res['ts']:%Y-%m-%d %H:%M:%S}")
    L.append("")
    L.append("> 【代理 vs 真实】说明（验收 §35）：全历史面板里，晚上市 ETF 的"
             "早期数据是缺失的，组合收益在该段实际上是『其余资产重新归一』的口径，"
             "不能当成完整六 ETF 的真实历史。本报告只从**所有 ETF 都有真实数据的"
             "共同起始日**开始，绝不含代理填充。")
    L.append("")
    fd = res.get("first_dates") or {}
    if fd:
        L.append("各 ETF 真实数据起始日：")
        for aid, d in sorted(fd.items(), key=lambda x: x[1]):
            L.append(f"- {res['asset_names'].get(aid, aid)}（{aid}）：{d}")
        L.append("")
    if not real.get("available"):
        L.append(f"共同区间不可回测：{real.get('message') or '所有 ETF 起始日相同'}。")
        return "\n".join(L)
    sp = real.get("span") or {}
    L.append(f"**共同区间：{sp.get('start')} ~ {sp.get('end')}"
             f"（{sp.get('bars')} 个交易日，约 {sp.get('years')} 年）**")
    L.append("")
    L.append("## 策略对比（真实六 ETF 区间）")
    L.append("")
    L.append("| 策略 | 累计收益 | 年化收益 | 最大回撤 | 年化波动 | Sharpe | "
             "Sortino | Calmar | 最长水下(日) | 交易次数 | 换手率 | 成本拖累 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for m in real.get("strategies", []) + real.get("thresholds", []):
        if not m.get("available"):
            continue
        uw = (f"{m['recovery_days']}天"
              + ("（已收复）" if m.get("recovered") else "（未收复）")
              if m.get("recovery_days") is not None else "—")
        L.append(f"| {m['name']} | {m['cumulative_return_pct']:+.2f}% | "
                 f"{m['annual_return_pct']:+.2f}% | {m['max_drawdown_pct']:.2f}% | "
                 f"{_f(m.get('annual_vol_pct'))}% | {_f(m.get('sharpe'))} | "
                 f"{_f(m.get('sortino'))} | {_f(m.get('calmar'))} | {uw} | "
                 f"{m.get('rebalances', '—')} | {m.get('total_turnover_pct', 0):.0f}% | "
                 f"{m.get('cost_drag_pct', 0):.2f}pp |")
    L.append("")
    L.append("> 最长水下=净值处于水下（低于前高）的最长持续交易日；"
             "交易次数=再平衡触发次数（信号 T 日收盘产生、T+1 开盘成交）；"
             "成本拖累=佣金+滑点对全期收益的累计拖累（pp）。")
    wf = real.get("walk_forward") or {}
    b60 = (wf.get("buy_signal") or {}).get("n60") or {}
    a60 = (wf.get("all_blocks") or {}).get("n60") or {}
    if b60.get("win_rate_pct") is not None and a60.get("win_rate_pct") is not None:
        L.append("## Walk-Forward（真实区间，体系 A）")
        L.append("")
        L.append(f"- 基准 60 日胜率 {a60['win_rate_pct']:.1f}%"
                 f"（样本 {a60.get('samples')}）；"
                 f"买入信号 {b60['win_rate_pct']:.1f}%"
                 f"（样本 {b60.get('samples')}）。")
        diff = (b60.get("win_rate_pct") or 0) - (a60.get("win_rate_pct") or 0)
        L.append(f"- 信号相对基准提升：**{diff:+.1f}pp**。")
        L.append("")
    return "\n".join(L)


def render_backtest_md(res: dict) -> str:
    if not res.get("ok"):
        return f"# 组合回测报告\n\n回测无法执行：{res.get('message')}\n"
    L: list[str] = []
    L.append("# 组合回测报告（BACKTEST_REPORT）")
    L.append("")
    L.append(f"生成时间：{res['ts']:%Y-%m-%d %H:%M:%S}")
    sp = res["span"]
    L.append(f"回测区间：{sp.get('start')} ~ {sp.get('end')}"
             f"（{sp.get('bars')} 个交易日，约 {sp.get('years')} 年，"
             f"前 300 个交易日作为指标预热期不计入）")
    L.append(f"再平衡频率：每 {res['rebalance_freq_days']} 个交易日检查一次")
    L.append("")
    L.append("> 执行口径（验收 §37/§38）：t 日收盘生成信号，**t+1 交易日开盘价成交**；"
             "成本拆分为佣金 + 滑点（可在 backtest.costs 配置），按换手率计费。"
             "所有信号在 t 时刻只使用 t 及以前的数据，无未来函数。")
    L.append("")
    L.append("## 一、策略对比")
    L.append("")
    L.append("| 策略 | 累计收益 | 年化收益 | 最大回撤 | 年化波动 | Sharpe | Sortino | Calmar | 正收益年份 | 换手率 | 成本拖累 | 最长水下 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for m in res["strategies"] + res["thresholds"]:
        if not m.get("available"):
            L.append(f"| {m['name']} | 数据不足 | | | | | | | | | | |")
            continue
        L.append(
            f"| {m['name']} | {m['cumulative_return_pct']:+.2f}% | {m['annual_return_pct']:+.2f}% | "
            f"{m['max_drawdown_pct']:.2f}% | {m['annual_vol_pct']:.2f}% | "
            f"{_f(m['sharpe'])} | {_f(m['sortino'])} | {_f(m['calmar'])} | "
            f"{_f(m['positive_year_ratio_pct'], 1)}% | {m['total_turnover_pct']:.0f}% | "
            f"{m['cost_drag_pct']:.2f}% | {m.get('recovery_days', '-')}日 |")
    L.append("")
    L.append("## 二、买入信号发出后的未来表现（信号日统计）")
    L.append("")
    L.append("| 策略 | 20日胜率 | 20日均值 | 60日胜率 | 60日均值 | 120日胜率 | 120日均值 |")
    L.append("|---|---|---|---|---|---|---|")
    for m in res["strategies"] + res["thresholds"]:
        if not m.get("available"):
            continue
        f = m["forward"]
        L.append(f"| {m['name']} | {_fs(f[20])} | {_fa(f[20])} | {_fs(f[60])} | "
                 f"{_fa(f[60])} | {_fs(f[120])} | {_fa(f[120])} |")
    L.append("")
    L.append("## 三、体系 A：Walk-Forward 样本外验证（块状，与逐日 OOS 分开）")
    L.append("")
    wf = res["walk_forward"]
    L.append(f"配置：训练窗口 {wf['config']['train_days']} 日 / 步长 "
             f"{wf['config']['step_days']} 日。{wf['note']}")
    L.append("")
    b60, w60 = wf["buy_signal"]["n60"], wf["wait_signal"]["n60"]
    b120, w120 = wf["buy_signal"]["n120"], wf["wait_signal"]["n120"]
    a60 = wf["all_blocks"]["n60"]
    L.append(f"- **基准**（全部测试块，不看信号）后 60 日胜率："
             f"{_f(a60['win_rate_pct'], 1)}%（样本 {a60['samples']}，"
             f"平均 {_f(a60['avg_pct'])}%）")
    L.append(f"- 买入信号（S/A/B）后 60 日：胜率 **{_f(b60['win_rate_pct'], 1)}%**"
             f"（样本 {b60['samples']}，平均 {_f(b60['avg_pct'])}%，最差 {_f(b60['max_loss_pct'])}%）")
    L.append(f"- 等待信号（C/D）后 60 日：胜率 {_f(w60['win_rate_pct'], 1)}%"
             f"（样本 {w60['samples']}，平均 {_f(w60['avg_pct'])}%）")
    L.append(f"- 买入信号后 120 日：胜率 {_f(b120['win_rate_pct'], 1)}%"
             f"；等待信号后 120 日：胜率 {_f(w120['win_rate_pct'], 1)}%")
    L.append("")
    L.append("| 测试块起点 | 60日概率 | 等级 | 实际60日 | 实际120日 |")
    L.append("|---|---|---|---|---|")
    for b in wf["blocks"]:
        L.append(f"| {b['date']} | {_f(b['prob60'], 1)}% | {b['grade']} | "
                 f"{_f(b['realized_60_pct'])}% | {_f(b['realized_120_pct'])}% |")
    L.append("")

    # ---- 概率校准度检验 ----
    cal = wf.get("calibration") or {}
    L.append("### 概率校准度检验（预测概率 vs 真实结局）")
    L.append("")
    if cal.get("brier") is not None:
        L.append(f"- 样本 {cal['samples']} 个测试块；**Brier 分数 {cal['brier']}**。")
        L.append("- Brier 仅作为概率误差指标展示。")
        L.append("- 正式概率校准必须与同一测试集的 Naive Base-rate 基线比较。")
        L.append("- 禁止使用 Brier<0.25 作为『校准良好』判据。")
        L.append("")
        L.append("| 预测概率区间 | 块数 | 平均预测概率 | 实际上涨频率 | 偏差 |")
        L.append("|---|---|---|---|---|")
        for bk in cal.get("buckets", []):
            L.append(f"| {bk['bucket']} | {bk['samples']} | "
                     f"{bk['avg_predicted_pct']}% | {bk['realized_win_rate_pct']}% | "
                     f"{bk['gap_pct']:+.1f}pp |")
        L.append("")
        L.append("- 读法：把「系统说 70% 的那些日子」挑出来，看真实上涨频率是否接近 70%。"
                 "接近=分桶频率与预测一致；系统性偏高/偏低=概率有偏，仅供参考"
                 "（正式校准以 STRATEGY_VALIDATION_REPORT 中与 Naive Base-rate 基线的比较为准）。")
    else:
        L.append("- 样本不足，无法做校准检验（需要足够的 Walk-Forward 测试块）。")
    L.append("")

    L.append("## 四、结论")
    L.append("")
    if b60["win_rate_pct"] is not None and a60["win_rate_pct"] is not None:
        gain = b60["win_rate_pct"] - a60["win_rate_pct"]
        L.append(f"- 买入信号相对**基准**的 60 日胜率差异：**{gain:+.1f} 个百分点**。"
                 + ("信号具备正区分度，但仍需警惕样本量不足。" if gain > 0 else
                    "信号在该样本上**不具备正区分度**：高胜率来自组合本身长期上涨，"
                    "而不是信号识别能力。请把 60 日概率理解为历史频率"
                    "（决策口径＝独立事件去重样本外，见 STRATEGY_VALIDATION_REPORT），"
                    "不要当成择时能力。"))
    if w60["win_rate_pct"] is not None and b60["win_rate_pct"] is not None:
        L.append(f"- 买入/等待两组 60 日胜率差："
                 f"{b60['win_rate_pct'] - w60['win_rate_pct']:+.1f} 个百分点。")
    L.append("- 样本块数量有限时，以上结论仅供参考，不构成任何收益保证。")
    L.append("- 严禁把回测结果当成未来收益预期；本系统只输出概率与风险，不做收益承诺。")
    L.append("")
    return "\n".join(L)


def _f(v, nd=2):
    if v is None:
        return "暂无数据"
    return f"{v:.{nd}f}"


def _fs(d):
    return "暂无数据" if d.get("win_rate_pct") is None else f"{d['win_rate_pct']:.1f}%"


def _fa(d):
    return "暂无数据" if d.get("avg_return_pct") is None else f"{d['avg_return_pct']:+.2f}%"


def run_backtest_cli(args) -> int:
    cfg = get_config(refresh=True)

    def progress(msg):
        print(f"  · {msg}", flush=True)

    res = run_backtest(cfg, progress)
    md = render_backtest_md(res)
    from .config import project_root
    out = Path(getattr(args, "out", "") or (project_root() / "BACKTEST_REPORT.md"))
    out.write_text(md, encoding="utf-8")
    print(md)
    print(f"\n已写入：{out}")
    real_md = render_real_6etf_md(res)
    real_out = out.parent / "BACKTEST_REAL_6ETF.md"
    real_out.write_text(real_md, encoding="utf-8")
    print(f"已写入：{real_out}")
    return 0 if res.get("ok") else 2
