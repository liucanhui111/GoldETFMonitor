"""组合层模型：把 6 只 ETF 合成"一个组合"，在组合层面做统计。

设计要点
--------
1. **历史日历对齐**：各 ETF 上市时间不同（如 30 年国债 ETF 2023 年才上市）。
   本模块取所有 ETF 交易日的并集作为组合日历，对未上市/缺数据的时段
   把该 ETF 的权重**按比例摊到当天可用的资产上**，避免"因为一只 ETF
   成立晚就把 5 年历史砍成 2 年"。

2. **无未来函数**：组合净值按当日权重（用户当下持有的权重，属于已知信息）
   逐日复利得到；任何滚动统计都只用截止当日的数据。

3. **组合级历史概率**：不是把 6 只 ETF 的胜率平均，而是先在组合净值序列上
   构造"组合状态特征"，再调用与单 ETF 同一套 SimilarStateEngine，
   统计历史相似状态下组合未来 5/20/60/120 日的真实收益分布。

4. **风险贡献**：w_i·(Σw)_i / (wᵗΣw)，衡量"仓位贡献"与"风险贡献"的差异。
"""
from __future__ import annotations

import math
from collections import deque
from typing import Sequence

from .indicators import max_drawdown_of

TRADING_DAYS = 252


# --------------------------------------------------------------------------
# 基础统计
# --------------------------------------------------------------------------
def _mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _std(xs: Sequence[float]) -> float:
    if len(xs) < 2:
        return 0.0
    mu = _mean(xs)
    return math.sqrt(sum((x - mu) ** 2 for x in xs) / (len(xs) - 1))


def _corr(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    n = min(len(xs), len(ys))
    if n < 5:
        return None
    a, b = list(xs[-n:]), list(ys[-n:])
    ma, mb = _mean(a), _mean(b)
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((y - mb) ** 2 for y in b)
    if va <= 0 or vb <= 0:
        return None
    cov = sum((a[i] - ma) * (b[i] - mb) for i in range(n))
    return cov / math.sqrt(va * vb)


def _pct_rank(vals: Sequence[float], v: float) -> float | None:
    xs = [x for x in vals if x is not None]
    if len(xs) < 20:
        return None
    return sum(1 for x in xs if x <= v) / len(xs) * 100.0


# --------------------------------------------------------------------------
# 组合面板
# --------------------------------------------------------------------------
class Panel:
    """组合日历上的对齐数据（含净值与各 ETF 日收益）。"""

    def __init__(self, datasets: dict[str, dict], weights: dict[str, float],
                 names: dict[str, str] | None = None) -> None:
        self.names = names or {}
        self.weights = {k: float(v) for k, v in weights.items()}
        usable = [a for a in datasets
                  if datasets[a] and datasets[a].get("dates")
                  and datasets[a].get("close")]
        self.assets: list[str] = usable
        date_sets = [set(datasets[a]["dates"]) for a in usable]
        union: set[str] = set()
        for s in date_sets:
            union |= s
        self.dates: list[str] = sorted(union)
        self.date_index = {d: i for i, d in enumerate(self.dates)}

        n = len(self.dates)
        self.close: dict[str, list] = {}
        self.open: dict[str, list] = {}
        self.ret: dict[str, list] = {}
        for a in usable:
            m = dict(zip(datasets[a]["dates"], datasets[a]["close"]))
            mo = dict(zip(datasets[a]["dates"], datasets[a].get("open") or []))
            seq: list = []
            oseq: list = []
            last = None
            lasto = None
            for d in self.dates:
                v = m.get(d)
                if v is not None:
                    last = float(v)
                seq.append(last)
                o = mo.get(d)
                if o is not None:
                    lasto = float(o)
                oseq.append(lasto)
            self.close[a] = seq
            self.open[a] = oseq
            r: list = [None] * n
            for i in range(1, n):
                if seq[i] is not None and seq[i - 1]:
                    r[i] = seq[i] / seq[i - 1] - 1.0
            self.ret[a] = r
        # 开盘跳空（T+1 开盘执行用，§37）：open[i] / close[i-1] - 1
        self.open_gap: dict[str, list] = {}
        for a in usable:
            g: list = [None] * n
            for i in range(1, n):
                if (self.open[a][i] is not None and self.close[a][i - 1]):
                    g[i] = self.open[a][i] / self.close[a][i - 1] - 1.0
            self.open_gap[a] = g

        # 每日实际参与计算的资产（该日已有数据的资产）
        self.active: list[list[str]] = []
        for i in range(n):
            self.active.append([a for a in usable if self.ret[a][i] is not None])

        # 组合日收益（权重在当日可用资产上重新归一）
        self.port_ret: list = [None] * n
        for i in range(n):
            act = self.active[i]
            if not act:
                continue
            tot = 0.0
            acc = 0.0
            for a in act:
                w = self.weights.get(a, 0.0)
                if w <= 0:
                    continue
                tot += w
                acc += w * self.ret[a][i]
            if tot > 0:
                self.port_ret[i] = acc / tot

        self.nav: list[float] = []
        v = 1.0
        for r in self.port_ret:
            if r is not None:
                v *= (1.0 + r)
            self.nav.append(v)

        self.actual_weights = self._renormalized_weights()
        self.cost_share = self._cost_share()

    # ------------------------------------------------------------------
    def _renormalized_weights(self) -> dict[str, float]:
        """把配置权重在"当前仍有效的资产"上归一，得到组合真实权重。"""
        live = [a for a in self.assets if self.close[a][-1] is not None]
        tot = sum(self.weights.get(a, 0.0) for a in live)
        if tot <= 0:
            return {}
        return {a: self.weights.get(a, 0.0) / tot for a in live}

    def _cost_share(self) -> dict[str, float]:
        """名义权重占比（未归一时），用于展示目标结构。"""
        tot = sum(self.weights.values()) or 1.0
        return {k: v / tot for k, v in self.weights.items()}

    def history_span(self) -> dict:
        if not self.dates:
            return {"available": False}
        first_multi = next(
            (i for i in range(len(self.dates)) if len(self.active[i]) >= 2), 0)
        return {
            "available": True,
            "start": self.dates[0],
            "end": self.dates[-1],
            "bars": len(self.dates),
            "multi_asset_start": self.dates[first_multi],
            "assets": len(self.assets),
            "years": round(len(self.dates) / TRADING_DAYS, 2),
        }

    # ------------------------------------------------------------------
    def window_returns(self, window: int) -> dict[str, list[float]]:
        """最近 window 日各资产收益（用于协方差/相关性）。"""
        out: dict[str, list[float]] = {}
        for a in self.assets:
            seg = [r for r in self.ret[a][-window:] if r is not None]
            if len(seg) >= max(5, window // 3):
                out[a] = seg
        return out

    def nav_window(self, window: int) -> dict:
        seg = self.nav[-window:] if window > 0 else self.nav
        if len(seg) < 2:
            return {"available": False}
        peak = seg[0]
        mdd = 0.0
        for v in seg:
            peak = max(peak, v)
            if peak:
                mdd = min(mdd, v / peak - 1.0)
        hi, lo = max(seg), min(seg)
        return {
            "available": True,
            "highest": hi,
            "lowest": lo,
            "max_drawdown_pct": mdd * 100.0,
            "current_drawdown_pct": (seg[-1] / hi - 1.0) * 100.0,
            "bars": len(seg),
        }


# --------------------------------------------------------------------------
# 组合统计
# --------------------------------------------------------------------------
def portfolio_stats(panel: Panel, cfg, *, nav: Sequence[float] | None = None,
                    rets: Sequence | None = None) -> dict:
    """组合统计。

    nav / rets 可传入"真实组合净值"（含现金 0 收益）覆盖默认的 ETF 满仓口径；
    不传时行为与旧版完全一致。
    """
    nav = panel.nav if nav is None else nav
    port_ret = panel.port_ret if rets is None else rets
    rf = float(cfg.get("risk.risk_free_rate", 0.02))
    dd_windows = cfg.get("indicators.drawdown_windows", {"1y": 252, "2y": 504, "5y": 1260})

    out: dict = {"span": panel.history_span()}
    dd = {}
    for label, w in dd_windows.items():
        dd[label] = _nav_window_of(nav, int(w))
    out["dd_windows"] = dd

    rlist = [r for r in port_ret if r is not None]
    n = len(rlist)
    out["bars"] = n
    if n >= 30:
        ann_ret = (nav[-1] ** (TRADING_DAYS / n) - 1.0) if nav[-1] > 0 else None
        vol_ann = _std(rlist) * math.sqrt(TRADING_DAYS)
        down = [r for r in rlist if r < 0]
        dn_vol = _std(down) * math.sqrt(TRADING_DAYS) if len(down) > 2 else None
        out["annual_return_pct"] = round(ann_ret * 100, 2) if ann_ret is not None else None
        out["annual_vol_pct"] = round(vol_ann * 100, 2)
        out["sharpe"] = round((ann_ret - rf) / vol_ann, 2) if (ann_ret is not None and vol_ann > 1e-9) else None
        out["sortino"] = round((ann_ret - rf) / dn_vol, 2) if (ann_ret is not None and dn_vol and dn_vol > 1e-9) else None
        out["max_drawdown_full_pct"] = round(max_drawdown_of(nav), 2)
        out["var95_daily_pct"] = round(sorted(rlist)[max(0, int(n * 0.05) - 1)] * 100, 2)
        # 最长水下期
        out["underwater"] = _underwater(nav)
        # 最大连续亏损
        out["max_losing_streak"] = _max_streak(port_ret, negative=True)
        out["max_gain_streak"] = _max_streak(port_ret, negative=False)
        out["annual_returns"] = _annual_returns(panel, nav=nav)
        pos = [v for v in out["annual_returns"].values() if v is not None]
        out["positive_year_ratio_pct"] = round(sum(1 for v in pos if v > 0) / len(pos) * 100, 1) if pos else None
    else:
        out["annual_return_pct"] = None
        out["annual_vol_pct"] = None
        out["sharpe"] = None
        out["sortino"] = None

    if len(nav) > 21:
        seg = [r for r in port_ret[-20:] if r is not None]
        out["vol20_annual_pct"] = round(_std(seg) * math.sqrt(TRADING_DAYS) * 100, 2)
    else:
        out["vol20_annual_pct"] = None
    return out


def _nav_window_of(nav: Sequence[float], window: int) -> dict:
    """任意净值序列的窗口回撤（Panel.nav_window 的通用版本）。"""
    seg = list(nav[-window:]) if window > 0 else list(nav)
    if len(seg) < 2:
        return {"available": False}
    peak = seg[0]
    mdd = 0.0
    for v in seg:
        peak = max(peak, v)
        if peak:
            mdd = min(mdd, v / peak - 1.0)
    hi = max(seg)
    return {
        "available": True,
        "highest": hi,
        "lowest": min(seg),
        "max_drawdown_pct": mdd * 100.0,
        "current_drawdown_pct": (seg[-1] / hi - 1.0) * 100.0,
        "bars": len(seg),
    }


def _underwater(nav: Sequence[float]) -> dict:
    peak = None
    peak_idx = 0
    longest = 0
    longest_end = 0
    cur_start = None
    worst = 0.0
    worst_span = (0, 0)
    for i, v in enumerate(nav):
        if peak is None or v >= peak:
            if cur_start is not None:
                span = i - cur_start
                if span > longest:
                    longest, longest_end = span, i
                cur_start = None
            peak = v
            peak_idx = i
        else:
            if cur_start is None:
                cur_start = peak_idx
            span = i - cur_start
            if span > longest:
                longest, longest_end = span, i
            if v / peak - 1.0 < worst:
                worst = v / peak - 1.0
                worst_span = (cur_start, i)
    return {
        "longest_days": longest,
        "longest_end": longest_end,
        "longest_end_date": None,
        "max_depth_pct": round(worst * 100, 2),
        "max_depth_span": worst_span,
        "recovered": cur_start is None,
    }


def _max_streak(rets: Sequence, negative: bool) -> int:
    best = cur = 0
    for r in rets:
        if r is None:
            continue
        hit = (r < 0) if negative else (r > 0)
        cur = cur + 1 if hit else 0
        best = max(best, cur)
    return best


def _annual_returns(panel: Panel, nav: Sequence[float] | None = None) -> dict[str, float | None]:
    nav = panel.nav if nav is None else nav
    out: dict[str, float | None] = {}
    if not panel.dates:
        return out
    years = sorted({d[:4] for d in panel.dates})
    for y in years:
        idx = [i for i, d in enumerate(panel.dates) if d[:4] == y]
        if len(idx) < 20:
            continue
        v0 = nav[idx[0] - 1] if idx[0] > 0 else 1.0
        v1 = nav[idx[-1]]
        out[y] = round((v1 / v0 - 1.0) * 100, 2) if v0 else None
    return out


# --------------------------------------------------------------------------
# 相关性
# --------------------------------------------------------------------------
def correlation_matrices(panel: Panel, cfg) -> dict:
    windows = cfg.get("risk.correlation_windows",
                      {"20d": 20, "60d": 60, "120d": 120, "1y": 252})
    labels = [panel.names.get(a, a) for a in panel.assets]
    out: dict = {"labels": labels, "ids": list(panel.assets), "matrices": {}}
    for label, w in windows.items():
        wr = panel.window_returns(int(w))
        ids = [a for a in panel.assets if a in wr]
        m: list[list[float | None]] = []
        for a in panel.assets:
            row: list[float | None] = []
            for b in panel.assets:
                if a == b:
                    row.append(1.0)
                elif a in wr and b in wr:
                    row.append(round(_corr(wr[a], wr[b]) or 0.0, 3) if _corr(wr[a], wr[b]) is not None else None)
                else:
                    row.append(None)
            m.append(row)
        pairs = []
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                c = _corr(wr[ids[i]], wr[ids[j]])
                if c is not None:
                    pairs.append((abs(c), c, ids[i], ids[j]))
        pairs.sort(reverse=True)
        avg = _mean([p[1] for p in pairs]) if pairs else None
        out["matrices"][label] = {
            "window": int(w),
            "matrix": m,
            "avg_corr": round(avg, 3) if avg is not None else None,
            "highest_pairs": [
                {"a": panel.names.get(p[2], p[2]), "b": panel.names.get(p[3], p[3]),
                 "corr": round(p[1], 3)} for p in pairs[:3]
            ],
            "lowest_pairs": [
                {"a": panel.names.get(p[2], p[2]), "b": panel.names.get(p[3], p[3]),
                 "corr": round(p[1], 3)} for p in reversed(pairs[-3:])
            ],
        }
    # 分散度：平均相关性越低越分散
    c60 = out["matrices"].get("60d", {}).get("avg_corr")
    if c60 is None:
        c60 = out["matrices"].get("20d", {}).get("avg_corr")
    if c60 is None:
        out["diversification_score"] = None
    else:
        out["diversification_score"] = round(max(0.0, min(100.0, (0.9 - c60) / 0.9 * 100)), 1)
    return out


# --------------------------------------------------------------------------
# 风险贡献
# --------------------------------------------------------------------------
def risk_contribution(panel: Panel, window: int = 120) -> dict:
    """仓位贡献 vs 风险贡献。"""
    wr = panel.window_returns(window)
    ids = [a for a in panel.assets if a in wr]
    if len(ids) < 2:
        return {"available": False}
    w = [panel.actual_weights.get(a, 0.0) for a in ids]
    tot_w = sum(w)
    if tot_w <= 0:
        return {"available": False}
    w = [x / tot_w for x in w]
    n = min(len(wr[a]) for a in ids)
    rets = {a: wr[a][-n:] for a in ids}
    cov: list[list[float]] = []
    for i in ids:
        row = []
        for j in ids:
            row.append(_covariance(rets[i], rets[j]))
        cov.append(row)
    sw = [sum(cov[i][j] * w[j] for j in range(len(ids))) for i in range(len(ids))]
    var = sum(w[i] * sw[i] for i in range(len(ids)))
    if var <= 0:
        return {"available": False}
    rows = []
    for i, a in enumerate(ids):
        rows.append({
            "id": a,
            "name": panel.names.get(a, a),
            "weight_pct": round(w[i] * 100, 2),
            "risk_contribution_pct": round(w[i] * sw[i] / var * 100, 2),
            "vol_annual_pct": round(_std(rets[a]) * math.sqrt(TRADING_DAYS) * 100, 2),
        })
    for r in rows:
        r["excess_pct"] = round(r["risk_contribution_pct"] - r["weight_pct"], 2)
        r["flag"] = ("风险贡献高于仓位贡献" if r["excess_pct"] > 3
                     else "风险贡献低于仓位贡献" if r["excess_pct"] < -3 else "基本匹配")
    rows.sort(key=lambda x: -x["risk_contribution_pct"])
    return {
        "available": True,
        "window": window,
        "portfolio_vol_annual_pct": round(math.sqrt(var) * math.sqrt(TRADING_DAYS) * 100, 2),
        "rows": rows,
        "effective_assets": round(1.0 / sum(x * x for x in w), 2),
    }


def _covariance(a: Sequence[float], b: Sequence[float]) -> float:
    n = min(len(a), len(b))
    if n < 5:
        return 0.0
    x, y = list(a[-n:]), list(b[-n:])
    mx, my = _mean(x), _mean(y)
    return sum((x[i] - mx) * (y[i] - my) for i in range(n)) / (n - 1)


# --------------------------------------------------------------------------
# 组合状态特征（供 SimilarStateEngine 使用）
# --------------------------------------------------------------------------
def weighted_avg(panel: Panel, series_by_asset: dict[str, list]) -> list:
    """按组合权重把"各资产的逐日序列"合成一条组合级序列。"""
    n = len(panel.dates)
    out: list = [None] * n
    ids = [a for a in panel.assets if a in series_by_asset]
    w = panel.actual_weights
    for i in range(n):
        tot = 0.0
        acc = 0.0
        for a in ids:
            seq = series_by_asset[a]
            if i >= len(seq):
                continue
            v = seq[i]
            if v is None:
                continue
            wt = w.get(a, 0.0)
            tot += wt
            acc += wt * float(v)
        if tot > 0:
            out[i] = acc / tot
    return out


def build_portfolio_features(panel: Panel, asset_inds: dict[str, dict],
                             asset_scores: dict[str, dict], cfg, *,
                             nav: Sequence[float] | None = None,
                             port_ret: Sequence | None = None) -> dict[str, list]:
    """在组合日历上构造组合级状态特征序列（全部因果）。

    nav / port_ret 可传入"真实组合净值/收益"（含现金 0 收益）；
    不传时使用 Panel 的 ETF 满仓口径（与旧版一致）。
    """
    n = len(panel.dates)
    ids = [a for a in panel.assets if a in asset_inds]
    w = panel.actual_weights
    _nav = panel.nav if nav is None else nav
    _ret = panel.port_ret if port_ret is None else port_ret

    def aligned_avg(key_path: tuple[str, str] | None, score_key: str | None) -> list:
        """按日期加权平均：优先用逐日评分序列，缺失则用当日快照。"""
        out: list = [None] * n
        per_asset: dict[str, dict[str, float]] = {}
        for a in ids:
            ind = asset_inds[a]
            m: dict[str, float] = {}
            series = None
            if key_path is not None:
                series = (ind.get(key_path[0]) or {}).get(key_path[1])
            if series is not None:
                for d, v in zip(ind["dates"], series):
                    if v is not None:
                        m[d] = float(v)
            per_asset[a] = m
        for i, d in enumerate(panel.dates):
            tot = 0.0
            acc = 0.0
            for a in ids:
                v = per_asset[a].get(d)
                if v is None:
                    continue
                wt = w.get(a, 0.0)
                tot += wt
                acc += wt * v
            if tot > 0:
                out[i] = acc / tot
        return out

    features: dict[str, list] = {}
    # 逐 bar 因果评分序列：直接由原始指标重算（与 histstats.build_feature_series
    # 同一套公式），再按日期对齐加权——不依赖"当日快照"（回测/逐日 OOS 时
    # 快照尚未产生，此前传空 dict 会导致 trend/position 等整列 None，
    # 组合模型实际上只用到 5 个特征——已修复，验收 §59）。
    from .histstats import build_feature_series

    per_asset_feats: dict[str, dict[str, list]] = {
        a: build_feature_series(asset_inds[a], cfg) for a in ids
    }

    def aligned_feat(key: str) -> list:
        out: list = [None] * n
        maps: dict[str, dict[str, float]] = {}
        for a in ids:
            seq = per_asset_feats[a].get(key) or []
            maps[a] = {d: float(v) for d, v in zip(asset_inds[a]["dates"], seq)
                       if v is not None}
        for i, d in enumerate(panel.dates):
            tot = 0.0
            acc = 0.0
            for a in ids:
                v = maps[a].get(d)
                if v is None:
                    continue
                wt = w.get(a, 0.0)
                tot += wt
                acc += wt * v
            if tot > 0:
                out[i] = acc / tot
        return out

    features["trend"] = aligned_feat("trend")
    features["position"] = aligned_feat("position")
    features["shortterm"] = aligned_feat("shortterm")
    features["swing"] = aligned_feat("swing")
    for k in ("bias20", "ret20", "wr", "rsi", "relpos1y", "relpos2y"):
        features[k] = aligned_feat(k)

    # 组合自身回撤 / 波动 / 相关性
    dd_windows = cfg.get("indicators.drawdown_windows", {"1y": 252, "2y": 504})
    features["dd1y"] = _rolling_dd(_nav, int(dd_windows.get("1y", 252)))
    features["dd2y"] = _rolling_dd(_nav, int(dd_windows.get("2y", 504)))
    features["vol20"] = _rolling_vol(_ret, 20)
    features["corr60"] = _rolling_avg_corr(panel, 60)
    features["navrel1y"] = _rolling_relpos(_nav, 252)
    return features


def _rolling_dd(nav: Sequence[float], window: int) -> list:
    """滚动回撤（%）。单调队列求区间最大值 → O(n)，结果与逐窗口 max() 完全一致。"""
    n = len(nav)
    out: list = [None] * n
    dq: deque[int] = deque()          # 递减队列，队首为窗口内最大值下标
    for i in range(n):
        v = nav[i]
        while dq and nav[dq[-1]] <= v:
            dq.pop()
        dq.append(i)
        left = i - window + 1
        while dq and dq[0] < left:
            dq.popleft()
        if i >= window - 1:
            hi = nav[dq[0]]
            if hi:
                out[i] = (v / hi - 1.0) * 100.0
    return out


def _rolling_vol(rets: Sequence, window: int) -> list:
    """滚动年化波动率（%）。前缀和 O(n)，等价于对窗口内非空收益求样本标准差。"""
    n = len(rets)
    out: list = [None] * n
    cnt = [0] * (n + 1)
    s1 = [0.0] * (n + 1)
    s2 = [0.0] * (n + 1)
    for i in range(n):
        r = rets[i]
        if r is None:
            cnt[i + 1] = cnt[i]
            s1[i + 1] = s1[i]
            s2[i + 1] = s2[i]
        else:
            f = float(r)
            cnt[i + 1] = cnt[i] + 1
            s1[i + 1] = s1[i] + f
            s2[i + 1] = s2[i] + f * f
    half = window // 2
    scale = math.sqrt(TRADING_DAYS) * 100.0
    for i in range(window, n):
        j = i - window + 1
        c = cnt[i + 1] - cnt[j]
        if c >= half and c >= 2:
            s = s1[i + 1] - s1[j]
            q = s2[i + 1] - s2[j]
            var = (q - s * s / c) / (c - 1)
            out[i] = math.sqrt(var if var > 0 else 0.0) * scale
    return out


def _rolling_relpos(nav: Sequence[float], window: int) -> list:
    """滚动相对价位（0-100）。双单调队列同时取区间最大/最小 → O(n)。"""
    n = len(nav)
    out: list = [None] * n
    dq_hi: deque[int] = deque()
    dq_lo: deque[int] = deque()
    for i in range(n):
        v = nav[i]
        while dq_hi and nav[dq_hi[-1]] <= v:
            dq_hi.pop()
        dq_hi.append(i)
        while dq_lo and nav[dq_lo[-1]] >= v:
            dq_lo.pop()
        dq_lo.append(i)
        left = i - window + 1
        while dq_hi and dq_hi[0] < left:
            dq_hi.popleft()
        while dq_lo and dq_lo[0] < left:
            dq_lo.popleft()
        if i >= window - 1:
            hi, lo = nav[dq_hi[0]], nav[dq_lo[0]]
            out[i] = 50.0 if hi - lo <= 0 else (v - lo) / (hi - lo) * 100.0
    return out


def _rolling_avg_corr(panel: Panel, window: int) -> list:
    """组合平均两两相关性（每 5 日计算一次，其余 ffill）。

    性能：窗口内两只资产都无缺失时走前缀和 O(1) 公式（与 _corr 数学等价）；
    窗口内存在缺失时回退到 _corr，保证与旧实现逐点一致。
    """
    n = len(panel.dates)
    out: list = [None] * n
    ids = panel.assets
    if len(ids) < 2 or n <= window:
        return out

    cnt: dict[str, list[int]] = {}
    s1: dict[str, list[float]] = {}
    s2: dict[str, list[float]] = {}
    for a in ids:
        c = [0] * (n + 1)
        p1 = [0.0] * (n + 1)
        p2 = [0.0] * (n + 1)
        seq = panel.ret[a]
        for i in range(n):
            r = seq[i]
            if r is None:
                c[i + 1], p1[i + 1], p2[i + 1] = c[i], p1[i], p2[i]
            else:
                f = float(r)
                c[i + 1] = c[i] + 1
                p1[i + 1] = p1[i] + f
                p2[i + 1] = p2[i] + f * f
        cnt[a], s1[a], s2[a] = c, p1, p2

    xy: dict[str, list[float]] = {}
    for x in range(len(ids)):
        for y in range(x + 1, len(ids)):
            A, B = panel.ret[ids[x]], panel.ret[ids[y]]
            acc = [0.0] * (n + 1)
            run = 0.0
            for i in range(n):
                u, v = A[i], B[i]
                if u is not None and v is not None:
                    run += float(u) * float(v)
                acc[i + 1] = run
            xy[f"{ids[x]}|{ids[y]}"] = acc

    half = window // 2
    last = None
    for i in range(window, n):
        if (i - window) % 5 != 0:
            out[i] = last
            continue
        j = i - window + 1
        pairs: list[float] = []
        for x in range(len(ids)):
            for y in range(x + 1, len(ids)):
                aid, bid = ids[x], ids[y]
                ca, cb = cnt[aid], cnt[bid]
                na = ca[i + 1] - ca[j]
                nb = cb[i + 1] - cb[j]
                if na < half or nb < half:
                    continue
                if na == window and nb == window:
                    sx = s1[aid][i + 1] - s1[aid][j]
                    sy = s1[bid][i + 1] - s1[bid][j]
                    qx = s2[aid][i + 1] - s2[aid][j]
                    qy = s2[bid][i + 1] - s2[bid][j]
                    sxy = xy[f"{aid}|{bid}"][i + 1] - xy[f"{aid}|{bid}"][j]
                    W = float(window)
                    cov = sxy - sx * sy / W
                    va = qx - sx * sx / W
                    vb = qy - sy * sy / W
                else:
                    a_seg = [r for r in panel.ret[aid][j:i + 1] if r is not None]
                    b_seg = [r for r in panel.ret[bid][j:i + 1] if r is not None]
                    m = min(len(a_seg), len(b_seg))
                    if m < half:
                        continue
                    a_seg, b_seg = a_seg[-m:], b_seg[-m:]
                    ma, mb = _mean(a_seg), _mean(b_seg)
                    cov = sum((a_seg[k] - ma) * (b_seg[k] - mb) for k in range(m))
                    va = sum((v - ma) ** 2 for v in a_seg)
                    vb = sum((v - mb) ** 2 for v in b_seg)
                if va > 0 and vb > 0:
                    pairs.append(cov / math.sqrt(va * vb))
        if pairs:
            last = _mean(pairs) * 100.0
            out[i] = last
        else:
            out[i] = last
    return out

# --------------------------------------------------------------------------
# 组合风险评分序列（供图表使用，逐点因果）
# --------------------------------------------------------------------------
def _map01(v, lo, hi):
    if v is None:
        return None
    if hi == lo:
        return 50.0
    return max(0.0, min(100.0, (float(v) - lo) / (hi - lo) * 100.0))


def risk_score_series(panel: Panel, cfg, window: int = 252) -> list:
    """逐 bar 的组合风险评分（0-100，越高越危险），只使用截止当日的数据。"""
    dd = _rolling_dd(panel.nav, window)
    vol = _rolling_vol(panel.port_ret, 20)
    corr = _rolling_avg_corr(panel, 60)
    rel = _rolling_relpos(panel.nav, window)
    n = len(panel.nav)
    out: list = [None] * n
    for i in range(n):
        parts = []
        if dd[i] is not None:
            parts.append((_map01(dd[i], 0.0, -22.0), 0.35))
        if vol[i] is not None:
            parts.append((_map01(vol[i], 4.0, 26.0), 0.30))
        if corr[i] is not None:
            parts.append((_map01(corr[i], 5.0, 75.0), 0.15))
        if rel[i] is not None:
            parts.append((100.0 - rel[i], 0.20))
        if not parts:
            continue
        tot = sum(w for _, w in parts)
        out[i] = sum(v * w for v, w in parts) / tot
    return out
