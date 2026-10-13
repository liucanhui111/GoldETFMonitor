"""历史相似状态统计引擎。

这是整个系统"胜率 / 期望收益 / 盈亏比"的唯一来源——**不做指标加权凑数**，
而是：在历史中找到与"当前状态"最相似的交易日，统计它们其后 N 日的真实收益分布。

无未来函数保证（关键）：
1. 所有特征序列由 indicators.py 逐 bar 因果计算，不使用任何未来数据；
2. 在 t 时刻寻找邻居时，只允许使用 s 满足 **s + horizon <= t** 的历史日，
   即"那一天之后 N 日的结果，在 t 时刻已经真实发生过"；
3. 标准化所用均值/方差只在候选池内计算；
4. 邻居之间强制最小间隔 cooldown，降低重叠窗口导致的样本相关性；
5. 回测调用时同样的规则逐日推进，绝不整段回看。
"""
from __future__ import annotations

import bisect
import math
from typing import Sequence

from .scoring import confidence_interval, sample_grade


def build_feature_series(ind: dict, cfg) -> dict[str, list]:
    """把 ETF 指标结果转成"逐日特征矩阵"。"""
    from . import scoring

    n = ind["n"]
    f: dict[str, list] = {}
    bands = cfg.get("classification.relpos_bands", [20, 35, 65, 80])
    f["trend"] = [scoring.trend_score(ind, i) for i in range(n)]
    f["position"] = [scoring.position_score(ind, bands, i) for i in range(n)]
    f["shortterm"] = [scoring.short_term_score(ind, i) for i in range(n)]
    f["swing"] = [scoring.swing_score(ind, i) for i in range(n)]

    f["bias20"] = list(ind.get("bias20") or [None] * n)
    f["ret20"] = list(ind.get("ret20") or [None] * n)
    f["wr"] = list(ind.get("wr14") or [None] * n)
    f["rsi"] = list(ind.get("rsi14") or [None] * n)
    f["dd1y"] = list((ind.get("curdd_series") or {}).get("1y") or [None] * n)
    f["dd2y"] = list((ind.get("curdd_series") or {}).get("2y") or [None] * n)
    f["relpos1y"] = list((ind.get("relpos_series") or {}).get("1y") or [None] * n)
    f["relpos2y"] = list((ind.get("relpos_series") or {}).get("2y") or [None] * n)
    f["vol20"] = list(ind.get("vol20_series") or [None] * n)
    return f


class SimilarStateEngine:
    """基于真实历史收益分布的统计引擎。"""

    def __init__(self, features: dict[str, list], weights: dict[str, float],
                 cfg) -> None:
        self.f = features
        self.w = {k: float(v) for k, v in (weights or {}).items()}
        self.cfg = cfg
        hist_cfg = cfg.get("hist", {})
        self.horizons = [int(h) for h in hist_cfg.get("horizons", [5, 20, 60, 120])]
        self.min_train = int(hist_cfg.get("min_train_days", 300))
        self.n_neighbors = int(hist_cfg.get("n_neighbors", 150))
        self.cooldown = int(hist_cfg.get("cooldown_days", 60))
        self.bands = hist_cfg.get("sample_grade", {"severe": 20, "low": 50,
                                                   "fair": 100, "good": 200})
        self.oos_window = int(cfg.get("hist.oos_window_days", 378))

    # ------------------------------------------------------------------
    def _usable_features(self, t: int) -> list[str]:
        out = []
        for k in self.w:
            seq = self.f.get(k)
            if seq is None or t >= len(seq):
                continue
            if seq[t] is not None and math.isfinite(float(seq[t])):
                out.append(k)
        return out

    def _pool(self, t: int, horizon: int) -> list[int]:
        """候选池：s 满足 s + horizon <= t（结果已知）且特征齐备。"""
        start = max(self.min_train, 1)
        end = t - horizon
        if end <= start:
            return []
        return list(range(start, end + 1))

    def _standardize(self, feats: list[str], pool: list[int]) -> dict[str, tuple[float, float]]:
        stats: dict[str, tuple[float, float]] = {}
        for k in feats:
            seq = self.f[k]
            vals = [float(seq[s]) for s in pool
                    if seq[s] is not None and math.isfinite(float(seq[s]))]
            if len(vals) < 10:
                continue
            mu = sum(vals) / len(vals)
            var = sum((v - mu) ** 2 for v in vals) / max(1, len(vals) - 1)
            sd = math.sqrt(var) or 1.0
            stats[k] = (mu, sd)
        return stats

    def _select(self, t: int, horizon: int) -> tuple[list[int], int, str, str]:
        feats = self._usable_features(t)
        pool = self._pool(t, horizon)
        if not feats or not pool:
            return [], 0, "严重不足", "低"
        stats = self._standardize(feats, pool)
        feats = [k for k in feats if k in stats]
        if not feats:
            return [], 0, "严重不足", "低"

        target = [(float(self.f[k][t]) - stats[k][0]) / stats[k][1] for k in feats]
        wsum = sum(self.w.get(k, 1.0) for k in feats) or 1.0

        scored: list[tuple[float, int]] = []
        for s in pool:
            acc = 0.0
            ok = 0
            for j, k in enumerate(feats):
                v = self.f[k][s]
                if v is None or not math.isfinite(float(v)):
                    continue
                z = (float(v) - stats[k][0]) / stats[k][1]
                acc += self.w.get(k, 1.0) * (z - target[j]) ** 2
                ok += 1
            if ok < max(3, len(feats) // 2):
                continue
            scored.append((math.sqrt(acc / wsum * (len(feats) / max(1, ok))), s))
        if not scored:
            return [], 0, "严重不足", "低"
        scored.sort()
        dists = [x[0] for x in scored]

        # 相对距离阈值：丢弃离当前状态过远的"凑数样本"。
        # 【硬上限纪律】实盘默认硬上限 = hist.distance_cap_mult（3.0×）：
        # 绝不为了凑够最少样本而自动放宽；3× 以内样本不足时如实返回
        # 不足样本并把可信度标为 UNKNOWN（"样本不足/UNKNOWN"）。
        # 只有显式配置 hist.allow_distance_relax=true 才允许手动放宽，
        # 且放宽后的结果必须标注「本次概率使用了超常距离样本，可信度降低」，
        # 并禁止被判为高可信（见 stats_from_neighbors 的 relaxed 标记）。
        cap_mult = float(self.cfg.get("hist.distance_cap_mult", 3.0))
        allow_relax = bool(self.cfg.get("hist.allow_distance_relax", False))
        min_need = int(self.cfg.get("hist.min_samples", 50))
        d0 = max(1e-9, dists[0])
        mult = max(1e-6, cap_mult)
        limit = d0 * mult
        end = bisect.bisect_right(dists, limit)
        chosen = self._cooldown_pick(scored[:end])
        self._last_mult = round(mult, 2)
        self._last_relaxed = False
        if len(chosen) < min_need and allow_relax:
            # 手动放宽：仅当配置显式允许时才逐步放宽（默认禁止）。
            for _ in range(12):
                mult *= 1.6
                end = bisect.bisect_right(dists, d0 * mult)
                chosen = self._cooldown_pick(scored[:end])
                if len(chosen) >= min_need or end >= len(scored) or mult >= 30.0:
                    break
            self._last_mult = round(mult, 2)
            self._last_relaxed = mult > cap_mult
        chosen.sort()
        grade, cred = sample_grade(len(chosen), self.bands)
        if len(chosen) < min_need:
            # 3×（或放宽后）以内样本仍不足最小要求 → 如实标注，
            # 不为了生成概率而强行使用更远的历史。
            cred = "UNKNOWN"
            if self._last_relaxed:
                cred = "UNKNOWN（超常距离放宽后仍不足）"
        elif self._last_relaxed:
            cred = "低（使用了超常距离样本，可信度降低）"
        return chosen, end, grade, cred

    def _cooldown_pick(self, cand: list[tuple[float, int]]) -> list[int]:
        """在候选集中按最小间隔挑选邻居（二分保证 O(n log k)）。"""
        out: list[int] = []
        cd = self.cooldown
        for _dist, s in cand:
            i = bisect.bisect_left(out, s)
            if i > 0 and s - out[i - 1] < cd:
                continue
            if i < len(out) and out[i] - s < cd:
                continue
            out.insert(i, s)
            if len(out) >= self.n_neighbors:
                break
        return out

    # ------------------------------------------------------------------
    def neighbors_at(self, t: int, horizon: int) -> list[int]:
        """当前状态在 t 时刻、未来 horizon 日的【历史相似样本下标】。

        组合边际模拟用它把"相似状态识别"与"组合构成比较"解耦：
        先用【当前真实组合】识别出一批历史相似起点，再在这批**同一窗口**上
        评估不同买入方案 —— 这样方案之间的差异来自组合构成，而不是邻居被重新挑选
        带来的抽样噪声。
        """
        chosen, _pool, _grade, _cred = self._select(t, horizon)
        return chosen

    def stats_from_neighbors(self, chosen: Sequence[int], prices: Sequence[float],
                             horizon: int, cost: float = 0.0, *, t: int | None = None,
                             pool_size: int = 0, grade: str | None = None,
                             cred: str | None = None) -> dict:
        """在【指定的】历史相似样本集上，统计未来 horizon 交易日的收益分布。

        与 stats_at 的唯一区别是邻居集由外部给定（用于同一个相似状态、
        不同组合构成之间的公平比较）。统计口径与 stats_at 完全一致。
        """
        if t is None:
            t = len(prices) - 1
        out: dict = {
            "horizon": horizon,
            "samples": len(chosen),
            "pool_size": pool_size,
            "sample_grade": grade,
            "credibility": cred,
            "available": len(chosen) > 0,
            "distance_mult": getattr(self, "_last_mult", None),
            "distance_relaxed": bool(getattr(self, "_last_relaxed", False)),
            "distance_cap_mult": float(self.cfg.get("hist.distance_cap_mult", 3.0)),
        }
        if getattr(self, "_last_relaxed", False):
            out["distance_note"] = ("本次概率使用了超常距离样本（阈值已手动放宽至 "
                                    f"{getattr(self, '_last_mult', None)}×），可信度降低；"
                                    "不得标为高可信。")
        if len(chosen) < int(self.cfg.get("hist.min_samples", 50)):
            out["distance_note"] = (out.get("distance_note") or "")
            if out["distance_note"]:
                out["distance_note"] += " "
            out["distance_note"] += ("相似距离硬上限内样本不足"
                                     f"（{len(chosen)} < {int(self.cfg.get('hist.min_samples', 50))}），"
                                     "样本不足/UNKNOWN，未使用更远的历史强行凑样本。")
        if not chosen:
            out["message"] = "历史样本不足"
            return out

        rets: list[float] = []
        mdd_list: list[float] = []
        oos_rets: list[float] = []
        for s in chosen:
            if s + horizon >= len(prices):
                continue
            p0 = prices[s]
            p1 = prices[s + horizon]
            if not p0:
                continue
            rets.append(p1 / p0 - 1.0)
            peak = p0
            mdd = 0.0
            for k in range(s, s + horizon + 1):
                if k >= len(prices):
                    break
                peak = max(peak, prices[k])
                if peak:
                    mdd = min(mdd, prices[k] / peak - 1.0)
            mdd_list.append(mdd * 100.0)
            if s >= t - self.oos_window:
                oos_rets.append(p1 / p0 - 1.0)

        base_all: list[float] = []
        base_recent: list[float] = []
        recent_cut = len(prices) - 1260
        for s in self._pool(t, horizon):
            if s + horizon >= len(prices):
                continue
            p0, p1 = prices[s], prices[s + horizon]
            if not p0:
                continue
            r = p1 / p0 - 1.0
            base_all.append(r)
            if s >= recent_cut:
                base_recent.append(r)

        if not rets:
            out["available"] = False
            out["message"] = "历史样本不足"
            return out

        n = len(rets)
        wins = [r for r in rets if r > 0]
        losses = [r for r in rets if r <= 0]
        avg_win = sum(wins) / len(wins) if wins else 0.0
        avg_loss = abs(sum(losses) / len(losses)) if losses else 0.0
        win_rate = len(wins) / n * 100.0
        loss_rate = 100.0 - win_rate

        def pct_ge(threshold: float) -> float:
            return sum(1 for r in rets if r >= threshold) / n * 100.0

        def pct_le(threshold: float) -> float:
            return sum(1 for r in rets if r <= threshold) / n * 100.0

        srt = sorted(rets)
        median = (srt[n // 2] if n % 2 else (srt[n // 2 - 1] + srt[n // 2]) / 2) * 100.0
        ci_lo, ci_hi = confidence_interval(win_rate, n)

        out.update({
            "win_rate_pct": round(win_rate, 1),
            "win_rate_net_pct": round(sum(1 for r in rets if r > cost) / n * 100.0, 1),
            "ci95_low": ci_lo, "ci95_high": ci_hi,
            "avg_return_pct": round(sum(rets) / n * 100.0, 2),
            "median_return_pct": round(median, 2),
            "avg_win_pct": round(avg_win * 100.0, 2),
            "avg_loss_pct": round(-avg_loss * 100.0, 2),
            "max_win_pct": round(max(rets) * 100.0, 2),
            "max_loss_pct": round(min(rets) * 100.0, 2),
            "payoff_ratio": round(avg_win / avg_loss, 2) if avg_loss > 1e-9 else None,
            "expected_return_pct": round((win_rate / 100 * avg_win - loss_rate / 100 * avg_loss) * 100.0, 2),
            "max_drawdown_pct": round(min(mdd_list), 2) if mdd_list else None,
            "avg_max_drawdown_pct": round(sum(mdd_list) / len(mdd_list), 2) if mdd_list else None,
            "prob_ge_3_pct": round(pct_ge(0.03), 1),
            "prob_ge_5_pct": round(pct_ge(0.05), 1),
            "prob_ge_10_pct": round(pct_ge(0.10), 1),
            "prob_ge_20_pct": round(pct_ge(0.20), 1),
            "prob_le_minus5_pct": round(pct_le(-0.05), 1),
            "prob_le_minus10_pct": round(pct_le(-0.10), 1),
            "prob_le_minus20_pct": round(pct_le(-0.20), 1),
            "oos_samples": len(oos_rets),
            "oos_win_rate_pct": round(sum(1 for r in oos_rets if r > 0) / len(oos_rets) * 100.0, 1)
            if oos_rets else None,
            "all_history_win_rate_pct": round(sum(1 for r in base_all if r > 0) / len(base_all) * 100.0, 1)
            if base_all else None,
            "recent_5y_win_rate_pct": round(sum(1 for r in base_recent if r > 0) / len(base_recent) * 100.0, 1)
            if base_recent else None,
            "baseline_samples": len(base_all),
            "cost_pct": round(cost * 100.0, 2),
        })
        if out.get("all_history_win_rate_pct") is not None:
            out["edge_vs_baseline_pct"] = round(
                win_rate - out["all_history_win_rate_pct"], 1)
        else:
            out["edge_vs_baseline_pct"] = None
        out["expected_return_net_pct"] = round(
            out["expected_return_pct"] - cost * 100.0, 2)
        return out

    def stats_at(self, t: int, prices: Sequence[float], horizon: int,
                 cost: float = 0.0) -> dict:
        """t 时刻、未来 horizon 交易日的收益分布统计。"""
        chosen, pool_size, grade, cred = self._select(t, horizon)
        return self.stats_from_neighbors(chosen, prices, horizon, cost, t=t,
                                         pool_size=pool_size, grade=grade, cred=cred)

    def similarity_detail(self, t: int, horizon: int) -> dict:
        """解释『为什么这些历史日被认为与今天相似』（验收 §7/§8）。

        输出每个特征：今天的 z 值、相似样本平均 z 值、平均差距（越小越像）；
        以及距离阈值使用情况：原始倍数 vs 实际放宽倍数。
        """
        chosen, pool_size, grade, cred = self._select(t, horizon)
        out: dict = {
            "available": bool(chosen), "samples": len(chosen),
            "pool_size": pool_size, "grade": grade, "credibility": cred,
            "distance_mult": getattr(self, "_last_mult", None),
            "distance_relaxed": bool(getattr(self, "_last_relaxed", False)),
            "raw_cap_mult": float(self.cfg.get("hist.distance_cap_mult", 3.0)),
            "min_samples": int(self.cfg.get("hist.min_samples", 50)),
        }
        if getattr(self, "_last_relaxed", False):
            out["message"] = ("本次概率使用了超常距离样本，可信度降低；不得标为高可信。")
        if not chosen:
            out["message"] = "历史样本不足"
            return out
        feats = self._usable_features(t)
        stats = self._standardize(feats, self._pool(t, horizon))
        rows: list[dict] = []
        for k in feats:
            seq = self.f[k]
            if seq[t] is None or k not in stats:
                continue
            tz = (float(seq[t]) - stats[k][0]) / stats[k][1]
            nz = [(float(seq[s]) - stats[k][0]) / stats[k][1]
                  for s in chosen
                  if seq[s] is not None and math.isfinite(float(seq[s]))]
            if not nz:
                continue
            rows.append({
                "feature": k,
                "weight": self.w.get(k, 1.0),
                "target_z": round(tz, 2),
                "neighbor_z": round(sum(nz) / len(nz), 2),
                "avg_gap": round(sum(abs(z - tz) for z in nz) / len(nz), 2),
            })
        rows.sort(key=lambda r: -r["avg_gap"])
        out["features"] = rows
        return out

    def all_horizons(self, t: int, prices: Sequence[float], cost: float = 0.0) -> dict:
        return {h: self.stats_at(t, prices, h, cost) for h in self.horizons}

    def all_horizons_shared(self, t: int, prices: Sequence[float], cost: float = 0.0,
                            neighbors: dict | None = None) -> dict:
        """用**同一批**历史相似样本评估所有周期（边际组合比较用）。"""
        nb = neighbors if neighbors is not None else {h: self.neighbors_at(t, h)
                                                      for h in self.horizons}
        return {h: self.stats_from_neighbors(nb.get(h, []), prices, h, cost, t=t)
                for h in self.horizons}
