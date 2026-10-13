"""组合边际模拟引擎（第三阶段核心）。

设计哲学（对齐需求第六、九、十、十一、三十六、五十九、六十条）
------------------------------------------------------------
**ETF 只是工具，组合才是最终决策对象。**

本模块唯一回答的问题：

    "如果今天把 X 元从现金换入某只 ETF，整个组合的风险收益结构会变好还是变坏？
     变好多少？买多少才最好？"

三个必须守住的口径
------------------
1. **真实组合，不是静态目标组合**。
   组合净值 = 各 ETF【当前真实权重】× 该 ETF 当日真实收益 **+ 现金（收益 0）**。
   19.9% 的待投资现金必须以 0 收益参与历史统计；尚未买入的 ETF 绝不提前计入。
2. **相似历史状态只识别一次**。
   先用【当前真实组合】的状态特征在历史上找出一批相似起点，再在这批**同一窗口**上
   评估各买入方案。若每换一个金额就重新挑邻居，0.9% 的权重变化会让 60 日胜率
   跳动 ±4 个百分点 —— 那是抽样噪声，会让"最优金额"变成随机数。
3. **最优金额由边际组合贡献决定**，不再机械"第一批永远 25%"：
   · 若 0 元（不交易）最好 → 输出 WAIT，允许"第一批 0 元"；
   · 若 3000 元最好 → 第一批建议 3000 元；
   · 同等效果取更省钱的方案（不一次把现金打光）。

为什么除了 Portfolio Quality 还要"边际改善分"
----------------------------------------------
Portfolio Quality 是绝对分。对于债券占 44%、现金占 20% 的低波组合，
60 日盈利概率常年 90%+，Quality 会"顶格饱和"（84 分上下几乎不动），
无法分辨 1 个百分点的改善。因此额外计算
**组合边际改善分（50 = 与不交易完全一样）**：把
"概率 / 期望收益 / 回撤 / 波动 / Sharpe / 分散度 / 目标缺口"的变化量
按可配置权重合成为 0-100 分，专门用来挑选金额与排序方案。

无未来函数
----------
邻居筛选规则来自 ``SimilarStateEngine``（只使用"结果已经发生"的历史样本），
本模块只是在这批已确定的历史窗口上按新权重重算组合收益，不引入任何时间穿越。
"""
from __future__ import annotations

import math
from typing import Iterable, Sequence

from . import portfolio as pf
from .histstats import SimilarStateEngine
from .scoring import wilson_ci

CLASS_LABEL = {"bond": "债券", "stock": "股票", "gold": "黄金", "cash": "现金/待投资"}
TRADING_DAYS = 252

# 边际改善分：各项变化的"满分跨度"（超过即记满分/零分）
DEFAULT_SPANS = {
    "prob60": 8.0,      # ±8 个百分点
    "exp60": 2.5,       # ±2.5 个百分点
    "dd1y": 2.5,        # ±2.5 个百分点（回撤变小 = 改善）
    "vol": 3.0,         # ±3 个百分点（波动变小 = 改善）
    "sharpe": 0.40,     # ±0.4
    "div": 8.0,         # ±8 分
    "gap": 5.0,         # ±5 个百分点（离战略目标更近 = 改善）
}
DEFAULT_WEIGHTS = {
    "prob60": 0.28, "exp60": 0.22, "dd1y": 0.18,
    "vol": 0.10, "sharpe": 0.10, "div": 0.07, "gap": 0.05,
}


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------
def _std(xs: Sequence[float]) -> float:
    if len(xs) < 2:
        return 0.0
    mu = sum(xs) / len(xs)
    return math.sqrt(sum((x - mu) ** 2 for x in xs) / (len(xs) - 1))


def _r(x, nd: int = 2):
    if x is None:
        return None
    try:
        return round(float(x), nd)
    except (TypeError, ValueError):
        return None


def nav_with_cash(panel: "pf.Panel", share_etf: float) -> list[float]:
    """把"ETF 占组合 share_etf、其余为现金（收益 0）"真实反映到组合净值上。

    这是【当前真实组合】的口径：只含今天真实持有的资产（含待投资现金），
    不含任何尚未买入的 ETF。
    """
    nav: list[float] = []
    v = 1.0
    for r in panel.port_ret:
        if r is not None:
            v *= (1.0 + r * share_etf)
        nav.append(v)
    return nav


def scaled_rets(panel: "pf.Panel", share_etf: float) -> list:
    """组合日收益（现金部分形成拖累系数 share_etf）。"""
    return [None if r is None else r * share_etf for r in panel.port_ret]


def _nav_of(rets: Sequence) -> list[float]:
    nav: list[float] = []
    v = 1.0
    for r in rets:
        if r is not None:
            v *= (1.0 + r)
        nav.append(v)
    return nav


def _norm(delta, span: float, invert: bool = False) -> float | None:
    """把变化量映射到 0-100，50 = 没有变化。"""
    if delta is None or not span:
        return None
    x = -float(delta) if invert else float(delta)
    return max(0.0, min(100.0, 50.0 + x / span * 50.0))


# --------------------------------------------------------------------------
# 主类
# --------------------------------------------------------------------------
class PortfolioSimulator:
    """在【真实组合 + 现金】口径下评估任意买卖方案对【整个组合】的影响。"""

    def __init__(self, datasets: dict, names: dict, asset_inds: dict,
                 class_of: dict, cfg, *, base_mv: dict, total_assets: float,
                 cost: float, targets_class: dict,
                 feature_weights: dict | None = None,
                 horizons: Iterable[int] = (5, 20, 60, 120),
                 target_mv: dict | None = None,
                 raw_prices: dict | None = None) -> None:
        self.datasets = datasets
        self.names = names
        self.asset_inds = asset_inds
        self.class_of = dict(class_of)
        self.cfg = cfg
        self.base_mv = {k: float(v) for k, v in (base_mv or {}).items()}
        self.total_assets = float(total_assets or 0.0)
        self.cost = float(cost or 0.0)
        self.targets_class = dict(targets_class or {})
        self.targets_with_cash = {**self.targets_class, "cash": 0.0}
        # 【最终战略目标组合】各 ETF 目标市值（仅供参照，绝不当作"当前组合"）
        self._target_mv = {k: float(v) for k, v in (target_mv or {}).items()
                           if float(v or 0) > 0}
        # 真实未复权交易价（§16/§17）：份数/金额换算专用，绝不使用前复权价
        self.raw_prices = {k: float(v) for k, v in (raw_prices or {}).items()
                           if float(v or 0) > 0}
        self.feature_weights = dict(feature_weights or cfg.get("hist.feature_weights", {}))
        self.feature_weights.update({"corr60": 0.8, "navrel1y": 1.0})
        self.horizons = tuple(int(h) for h in horizons)
        self.rf = float(cfg.get("risk.risk_free_rate", 0.02))
        self._cache: dict = {}
        self.n_eval = 0
        self._ready = False
        self._analogs: dict[int, list[int]] = {}
        self._t = 0
        self._engine: SimilarStateEngine | None = None
        self._panel: "pf.Panel | None" = None
        self._nav: list[float] = []
        self._share: float = 1.0
        self._dates: list[str] = []
        self._asset_ids: list[str] = []
        self._asset_ret: dict[str, list] = {}
        self._c60: float | None = None
        self.prepare_error: str | None = None
        self._ctor = dict(
            datasets=datasets, names=names, asset_inds=asset_inds,
            class_of=class_of, cfg=cfg, total_assets=total_assets, cost=cost,
            targets_class=targets_class, feature_weights=feature_weights,
            horizons=horizons, target_mv=target_mv, raw_prices=raw_prices)
        self._prepare()

    # ---------------------------------------------------------------- 交易单位
    def lots_for_amount(self, aid: str, amount: float) -> tuple[int | None, float | None]:
        """理论金额 → 100 份整数倍（四舍五入，至少 100 份）→ 实际成交金额。

        必须使用【真实未复权价】（§16-§19）；价格缺失时返回 (None, None)，
        调用方必须显示『交易价缺失，无法换算份数』而不是用复权价硬算。
        """
        price = self.raw_prices.get(aid)
        if not price or price <= 0 or not amount or amount <= 0:
            return None, None
        lots = amount / (price * 100.0)
        n = int(round(lots))
        if n < 1:
            n = 1 if lots >= 0.5 else 0
        if n <= 0:
            return None, None
        return n * 100, round(n * 100 * price, 2)

    def min_capital_items(self, asset_targets: dict | None = None) -> list[dict]:
        """§21/§22：每只 ETF 100 份最小金额 → 最小合理组合规模。

        asset_targets: {etf_id: 目标权重(0~1)}（组合内各 ETF 的战略目标权重）。
        """
        out = []
        for aid, price in self.raw_prices.items():
            w = float((asset_targets or {}).get(aid) or 0.0)
            if w <= 0:
                continue
            min_amt = round(price * 100, 2)
            out.append({
                "id": aid, "name": self.names.get(aid, aid),
                "price": price, "min_amount": min_amt,
                "target_weight_pct": round(w * 100, 1),
                "min_total": round(min_amt / w, 0) if w > 0 else None,
                "fits_20k": min_amt / w <= 20000,
                "fits_50k": min_amt / w <= 50000,
                "fits_100k": min_amt / w <= 100000,
            })
        out.sort(key=lambda x: -(x["min_total"] or 0))
        return out

    def sibling(self, base_mv: dict) -> "PortfolioSimulator":
        """在"假设已执行若干批"的新持仓上重建模拟器（分批阶梯用）。"""
        kw = dict(self._ctor)
        kw["base_mv"] = dict(base_mv)
        return PortfolioSimulator(**kw)

    # ---------------------------------------------------------------- 逐日 OOS
    def daily_oos_predictions(self, horizons: Iterable[int] | None = None,
                              step: int = 1, progress=None) -> dict:
        """逐日滚动样本外预测（验收 §9/§10）。

        每个交易日 t 只用 ≤t 的数据选相似样本并给出【条件胜率预测】，
        未来 h 日真实结局发生后再记录对错——逐日推进，绝无未来函数。
        输出预测点数 / 独立信号数（间隔≥h，窗口不重叠）/ Brier / 5% 宽校准桶。
        """
        if not self._ready:
            return {"ok": False, "message": self.prepare_error or "引擎未就绪"}
        engine: SimilarStateEngine = self._engine
        nav = self._nav
        n = len(nav)
        hs = tuple(int(h) for h in (horizons or (20, 60, 120)))
        out: dict = {"ok": True, "horizons": {}}
        start_t = max(int(engine.min_train), 300)
        for h in hs:
            pts: list[dict] = []
            mults: list[float] = []
            for t in range(start_t, n - h, max(1, step)):
                try:
                    st = engine.stats_at(t, nav, h, 0.0)
                except Exception:  # noqa: BLE001
                    continue
                pred = st.get("win_rate_pct")
                if pred is None or not st.get("available"):
                    continue
                if not nav[t]:
                    continue
                realized = nav[t + h] / nav[t] - 1.0
                pts.append({"t": t, "pred": pred / 100.0,
                            "hit": 1.0 if realized > 0 else 0.0,
                            "ret": realized * 100.0})
                m = st.get("distance_mult")
                if m is not None:
                    mults.append(float(m))
                if progress and (t - start_t) % 200 == 0:
                    progress(f"  逐日OOS h={h}: {t - start_t}/{n - h - start_t}")
            # 独立信号：窗口不重叠（间隔 ≥ h）
            indep: list[dict] = []
            last_t = -10 ** 9
            for p in pts:
                if p["t"] - last_t >= h:
                    indep.append(p)
                    last_t = p["t"]

            def _cal(rows: list[dict], base_rate: float | None = None) -> dict:
                if not rows:
                    return {"points": 0, "brier": None, "naive_brier": None,
                            "brier_skill_score": None,
                            "calibration_verdict": "样本不足，无法评估校准",
                            "realized_win_rate_pct": None,
                            "avg_pred_pct": None, "buckets": []}
                brier = round(sum((r["pred"] - r["hit"]) ** 2 for r in rows)
                              / len(rows), 4)
                # ---- Naive Base-rate 基线（最终修正）：永远预测基础胜率的模型 ----
                # 校准好不好必须与它比（BSS > 0 才算优于基线），
                # 不再使用"Brier < 0.25 即可接受"的旧口径。
                naive_brier = None
                bss = None
                cal_verdict = "基础率基线不可用，无法判定校准"
                if base_rate is not None:
                    naive_brier = round(
                        sum((base_rate - r["hit"]) ** 2 for r in rows) / len(rows), 4)
                    if naive_brier and naive_brier > 0:
                        bss = round(1.0 - brier / naive_brier, 4)
                    if brier > naive_brier:
                        cal_verdict = ("模型校准未优于基础率基线"
                                       f"（Model Brier {brier} > Naive {naive_brier}）")
                    else:
                        cal_verdict = (f"优于基础率基线（Model Brier {brier} ≤ "
                                       f"Naive {naive_brier}，BSS {bss if bss is not None else '—'}）")
                edges = [(0.0, 0.50, "<50%"), (0.50, 0.55, "50-55%"),
                         (0.55, 0.60, "55-60%"), (0.60, 0.65, "60-65%"),
                         (0.65, 0.70, "65-70%"), (0.70, 0.75, "70-75%"),
                         (0.75, 0.80, "75-80%"), (0.80, 1.01, "≥80%")]
                buckets = []
                for lo, hi, label in edges:
                    ys = [r for r in rows if lo <= r["pred"] < hi]
                    if ys:
                        buckets.append({
                            "bucket": label, "samples": len(ys),
                            "avg_pred_pct": round(
                                sum(r["pred"] for r in ys) / len(ys) * 100, 1),
                            "realized_win_rate_pct": round(
                                sum(r["hit"] for r in ys) / len(ys) * 100, 1),
                        })
                wr = sum(r["hit"] for r in rows) / len(rows)
                ci_lo, ci_hi = wilson_ci(wr * 100.0, len(rows))
                return {
                    "points": len(rows),
                    "brier": brier,
                    "naive_brier": naive_brier,
                    "brier_skill_score": bss,
                    "calibration_verdict": cal_verdict,
                    "realized_win_rate_pct": round(wr * 100, 1),
                    "n_pos": sum(1 for r in rows if r["hit"] > 0),
                    "n_neg": sum(1 for r in rows if r["hit"] <= 0),
                    "ci95_low": round(ci_lo, 1),
                    "ci95_high": round(ci_hi, 1),
                    "avg_pred_pct": round(
                        sum(r["pred"] for r in rows) / len(rows) * 100, 1),
                    "buckets": buckets,
                    "avg_return_pct": round(
                        sum(r["ret"] for r in rows) / len(rows), 2),
                }

            # ---- 无条件基准（P0-3）：同一评估窗口内所有交易日、不看任何信号 ----
            bw = bt_total = 0
            for t in range(start_t, n - h):
                if nav[t] and nav[t + h]:
                    bt_total += 1
                    if nav[t + h] / nav[t] > 1.0:
                        bw += 1
            base_rate = (bw / bt_total) if bt_total else None

            res = _cal(indep, base_rate)   # 独立样本做校准（避免重叠窗口高估）
            res["points"] = len(pts)   # 预测点总数（逐日）
            res["independent"] = len(indep)
            res["all_points"] = _cal(pts, base_rate) if pts else None
            res["base_win_rate_pct"] = round(base_rate * 100, 1) if base_rate is not None else None
            res["base_days"] = bt_total
            if (res.get("realized_win_rate_pct") is not None
                    and res.get("base_win_rate_pct") is not None):
                res["uplift_vs_base_pp"] = round(
                    res["realized_win_rate_pct"] - res["base_win_rate_pct"], 1)
            res["distance_mult_avg"] = (
                round(sum(mults) / len(mults), 2) if mults else None)
            out["horizons"][h] = res
        return out

    def trend_only_oos(self, horizons: Iterable[int] | None = None,
                       step: int = 1) -> dict:
        """基准C：只使用趋势/位置特征的同类逐日 OOS 预测（与完整模型同框架）。"""
        if not self._ready:
            return {"ok": False, "message": self.prepare_error or "引擎未就绪"}
        feats = self._engine.f
        keep = [k for k in ("trend", "position", "ret20") if k in feats]
        if not keep:
            return {"ok": False, "message": "趋势特征不可用"}
        w = {k: 1.0 for k in keep}
        sub = SimilarStateEngine(feats, w, self.cfg)
        saved = (self._engine, self.feature_weights)
        try:
            self._engine = sub
            return self.daily_oos_predictions(horizons, step)
        finally:
            self._engine, self.feature_weights = saved

    def simulate_target(self) -> dict | None:
        """【最终战略目标组合】：完全按战略目标权重配置（现金=0）时的统计。

        ⚠️ 只用于"参照/展示目标长什么样"，**绝不允许**拿它替代【当前真实组合】
        来回答"现在该不该买"——那是第二阶段反复强调的错误。
        """
        if not self._target_mv or not self._ready:
            return None
        try:
            return self.evaluate(self._target_mv)
        except Exception:  # noqa: BLE001
            return None

    # ---------------------------------------------------------------- 准备
    def _prepare(self) -> None:
        """用【当前真实组合】建立基准面板 + 相似状态邻居集（只做一次）。"""
        try:
            etf_value = self.etf_value
            if etf_value <= 0:
                self.prepare_error = "持仓市值为 0，无法建立组合基准"
                return
            total = self.total_assets if self.total_assets > 0 else etf_value
            weights = {k: v / etf_value for k, v in self.base_mv.items() if v > 0}
            share = min(1.0, etf_value / total)
            panel = pf.Panel(self.datasets, weights, self.names)
            nav = nav_with_cash(panel, share)
            rets = scaled_rets(panel, share)
            feats = pf.build_portfolio_features(panel, self.asset_inds, {}, self.cfg,
                                                nav=nav, port_ret=rets)
            engine = SimilarStateEngine(feats, self.feature_weights, self.cfg)
            t = len(nav) - 1
            self._engine = engine
            self._panel = panel
            self._nav = nav
            self._share = share
            self._t = t
            self._dates = panel.dates
            self._asset_ids = list(panel.assets)
            self._asset_ret = {a: panel.ret[a] for a in panel.assets}
            self._analogs = {h: engine.neighbors_at(t, h) for h in self.horizons}
            self._c60 = self._avg_corr_at(panel, 60)
            self._ready = True
        except Exception as exc:  # noqa: BLE001
            self.prepare_error = f"{type(exc).__name__}: {exc}"

    @staticmethod
    def _avg_corr_at(panel: "pf.Panel", window: int) -> float | None:
        try:
            series = pf._rolling_avg_corr(panel, window)
            return series[-1] if series else None
        except Exception:  # noqa: BLE001
            return None

    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def analogs(self) -> dict:
        return {h: list(v) for h, v in self._analogs.items()}

    # ---------------------------------------------------------------- 基础
    @property
    def etf_value(self) -> float:
        return sum(self.base_mv.values())

    @property
    def cash(self) -> float:
        return max(0.0, self.total_assets - self.etf_value)

    def _key(self, mv: dict) -> tuple:
        return tuple(sorted((k, round(float(v), 4)) for k, v in mv.items() if v > 0))

    def classes_of_mv(self, mv: dict) -> dict:
        """按总资产口径给出 债券/股票/黄金/现金 四类权重（合计 1）。"""
        total = self.total_assets if self.total_assets > 0 else max(1e-9, sum(mv.values()))
        out = {"bond": 0.0, "stock": 0.0, "gold": 0.0, "cash": 0.0}
        for aid, v in mv.items():
            cls = self.class_of.get(aid, "other")
            if cls in out:
                out[cls] += float(v) / total
        out["cash"] = max(0.0, (total - sum(float(v) for v in mv.values())) / total)
        return out

    def class_gap(self, classes: dict) -> tuple[float, dict]:
        """与战略目标的偏离：返回 (平均绝对偏离 pp, 逐类偏离 pp)。"""
        dev: dict[str, float] = {}
        for cls, tgt in self.targets_with_cash.items():
            cur = classes.get(cls)
            if cur is None:
                continue
            dev[cls] = round((cur - tgt) * 100, 2)
        avg = sum(abs(v) for v in dev.values()) / len(dev) if dev else None
        return (avg if avg is not None else 0.0), dev

    # ---------------------------------------------------------------- 合成
    def _compose(self, mv: dict) -> tuple[list, float, float] | None:
        """按给定市值组合出【真实组合日收益序列】（现金 0 收益）。

        规则与 pf.Panel 完全一致：当日有数据的资产按其权重归一，
        缺数据的资产不参与（等价于该日按比例摊到可用资产上）。
        """
        etf_value = sum(float(v) for v in mv.values() if v > 0)
        if etf_value <= 0 or not self._asset_ids:
            return None
        total = self.total_assets if self.total_assets > 0 else etf_value
        weights = {k: float(v) / etf_value for k, v in mv.items() if v > 0}
        share = min(1.0, etf_value / total) if total > 0 else 1.0
        n = len(self._dates)
        out: list = [None] * n
        for i in range(n):
            tot = 0.0
            acc = 0.0
            for a in self._asset_ids:
                w = weights.get(a, 0.0)
                if w <= 0:
                    continue
                r = self._asset_ret[a][i]
                if r is None:
                    continue
                tot += w
                acc += w * r
            if tot > 0:
                out[i] = acc / tot * share
        return out, share, etf_value

    # ---------------------------------------------------------------- 评估
    def evaluate(self, mv: dict) -> dict | None:
        """给定"各 ETF 市值（元）"，评估整个组合的风险收益结构。"""
        key = self._key(mv)
        if key in self._cache:
            return self._cache[key]
        if not self._ready or self._engine is None:
            return None
        composed = self._compose(mv)
        if composed is None:
            return None
        rets, share, etf_value = composed
        nav = _nav_of(rets)

        try:
            hist = {h: self._engine.stats_from_neighbors(
                self._analogs.get(h, []), nav, h, self.cost, t=self._t)
                for h in self.horizons}
        except Exception:  # noqa: BLE001
            return None
        self.n_eval += 1

        pstats = {"dd_windows": {}, "annual_vol_pct": None}
        try:
            pstats = pf.portfolio_stats(self._panel, self.cfg, nav=nav, rets=rets)
        except Exception:  # noqa: BLE001
            pass

        classes = self.classes_of_mv(mv)
        gap_avg, gap_detail = self.class_gap(classes)
        weights_total = {aid: float(v) / (self.total_assets or 1.0)
                         for aid, v in mv.items() if v > 0}

        h60 = hist.get(60) or {}
        h20 = hist.get(20) or {}
        h30 = hist.get(30) or {}
        h90 = hist.get(90) or {}
        h120 = hist.get(120) or {}

        out = {
            "hist": hist,
            "rets": rets,
            "nav": nav,
            "etf_value": round(etf_value, 2),
            "cash_value": round(max(0.0, (self.total_assets or etf_value) - etf_value), 2),
            "share_etf": round(share, 4),
            "classes": {k: round(v, 4) for k, v in classes.items()},
            "weights_total": weights_total,
            "gap_avg_pp": _r(gap_avg, 2),
            "gap_detail_pp": gap_detail,
            "prob20": _r(h20.get("win_rate_pct"), 1),
            "prob30": _r(h30.get("win_rate_pct"), 1),
            "prob60": _r(h60.get("win_rate_pct"), 1),
            "prob90": _r(h90.get("win_rate_pct"), 1),
            "prob120": _r(h120.get("win_rate_pct"), 1),
            "prob30_oos": _r(h30.get("oos_win_rate_pct"), 1),
            "prob60_oos": _r(h60.get("oos_win_rate_pct"), 1),
            "prob90_oos": _r(h90.get("oos_win_rate_pct"), 1),
            # ---- 四概率体系（验收 §5/§48）----
            # A 基础胜率：不用任何择时信号，全历史候选池的无条件胜率
            "prob60_base": _r(h60.get("all_history_win_rate_pct"), 1),
            # D 相对基准提升 = C 样本外条件胜率 - A 基础胜率（样本外优先）
            "prob60_uplift": (
                round((_r(h60.get("oos_win_rate_pct"), 1)
                       or _r(h60.get("win_rate_pct"), 1))
                      - h60.get("all_history_win_rate_pct"), 1)
                if h60.get("all_history_win_rate_pct") is not None
                and (h60.get("oos_win_rate_pct") is not None
                     or h60.get("win_rate_pct") is not None) else None),
            "prob60_oos_samples": int(h60.get("oos_samples") or 0),
            "prob60_samples": int(h60.get("samples") or 0),
            "prob60_ci": [_r(h60.get("ci95_low"), 1), _r(h60.get("ci95_high"), 1)],
            "prob60_grade": h60.get("sample_grade"),
            "prob60_credibility": h60.get("credibility"),
            "exp20": _r(h20.get("expected_return_pct"), 2),
            "exp60": _r(h60.get("expected_return_pct"), 2),
            "exp120": _r(h120.get("expected_return_pct"), 2),
            "payoff60": _r(h60.get("payoff_ratio"), 2),
            "dd60": _r(h60.get("max_drawdown_pct"), 2),
            "max_dd_1y": _r(((pstats.get("dd_windows") or {}).get("1y") or {}).get("max_drawdown_pct")),
            "max_dd_2y": _r(((pstats.get("dd_windows") or {}).get("2y") or {}).get("max_drawdown_pct")),
            "cur_dd_1y": _r(((pstats.get("dd_windows") or {}).get("1y") or {}).get("current_drawdown_pct")),
            "vol_annual": pstats.get("annual_vol_pct"),
            "annual_return": pstats.get("annual_return_pct"),
            "sharpe": pstats.get("sharpe"),
            "sortino": pstats.get("sortino"),
            "diversification": self._diversification(mv),
            "quality": None,
            "quality_detail": None,
            "nav_bars": len(nav),
            "nav_last": round(nav[-1], 4),
        }
        self._cache[key] = out
        # Portfolio Quality 需要 hist / pstats / 分散度，单独算（便于缓存复用）
        try:
            from . import signals  # 延迟导入，避免模块级循环依赖
            quality = signals.portfolio_quality(
                pstats, {"diversification_score": out["diversification"]}, hist,
                classes, self.targets_class, self.cfg)
            out["quality"] = quality.get("score")
            out["quality_detail"] = quality
        except Exception:  # noqa: BLE001
            pass
        self._cache[key] = out
        return out

    def _diversification(self, mv: dict) -> float | None:
        """对权重敏感的分散度（0-100）。

        旧的分散度只由"平均两两相关性"决定，与仓位完全无关，
        因此对"把现金换进某只 ETF"这种操作毫无分辨力。
        这里加入有效资产数：仓位越集中，分散度越低。
        """
        if self._c60 is None:
            return None
        etf_value = sum(float(v) for v in mv.values() if v > 0)
        if etf_value <= 0:
            return None
        ws = [float(v) / etf_value for v in mv.values() if v > 0]
        n_eff = 1.0 / sum(w * w for w in ws)
        n_all = max(1, len(self._asset_ids))
        c = max(0.0, min(1.0, self._c60 / 100.0))
        return round(100.0 * (1.0 - c) * min(1.0, n_eff / n_all), 1)

    # ---------------------------------------------------------------- 边际
    def marginal_score(self, base: dict, after: dict) -> dict:
        """组合边际改善分（50 = 与不交易完全一样，>50 = 变好）。"""
        w = dict(DEFAULT_WEIGHTS)
        w.update({k: float(v) for k, v in
                  (self.cfg.get("simulation.marginal_weights", {}) or {}).items()})
        spans = dict(DEFAULT_SPANS)
        spans.update({k: float(v) for k, v in
                      (self.cfg.get("simulation.marginal_spans", {}) or {}).items()})

        def delta(k, nd=4):
            a, b = after.get(k), base.get(k)
            if a is None or b is None:
                return None
            return round(float(a) - float(b), nd)

        g = delta("gap_avg_pp", 4)
        d = {
            "prob60": delta("prob60", 2),
            "exp60": delta("exp60", 2),
            "dd1y": delta("max_dd_1y"),        # 回撤数值变大 = 改善
            "vol": delta("vol_annual"),
            "sharpe": delta("sharpe"),
            "div": delta("diversification"),
            "gap": (-g) if g is not None else None,   # 缺口缩小 = 改善
        }
        parts = {
            "prob60": _norm(d["prob60"], spans["prob60"]),
            "exp60": _norm(d["exp60"], spans["exp60"]),
            "dd1y": _norm(d["dd1y"], spans["dd1y"]),
            "vol": _norm(d["vol"], spans["vol"], invert=True),
            "sharpe": _norm(d["sharpe"], spans["sharpe"]),
            "div": _norm(d["div"], spans["div"]),
            "gap": _norm(d["gap"], spans["gap"]),
        }
        tot = sum(w.get(k, 0.0) for k, v in parts.items() if v is not None)
        score = (sum(w.get(k, 0.0) * v for k, v in parts.items() if v is not None) / tot
                 if tot else None)
        return {
            "score": _r(score, 1),
            "delta": d,
            "parts": {k: _r(v, 1) for k, v in parts.items()},
            "base": 50.0,
            "note": "50 = 与不交易完全一样；>50 表示整个组合的风险收益结构在改善。",
        }

    # ---------------------------------------------------------------- 网格
    def amount_grid(self, cash: float | None = None) -> list[float]:
        """金额梯度：固定档位 + 按可投资现金自动扩展。"""
        cash = self.cash if cash is None else max(0.0, float(cash))
        base = [float(x) for x in self.cfg.get(
            "simulation.amount_grid", [0, 500, 1000, 1500, 2000, 3000, 4000, 5000])]
        grid = [g for g in base if g >= 0]
        for frac in self.cfg.get("simulation.cash_fractions", [0.1, 0.25, 0.5, 0.75, 1.0]):
            if cash > 0:
                grid.append(round(cash * float(frac), 0))
        cap = float(self.cfg.get("simulation.max_amount_per_asset", 20000) or 20000)
        out = sorted({round(min(g, cash, cap), 0) for g in grid if 0 <= g <= max(cash, 0.0) + 1e-9})
        if not out or out[0] != 0.0:
            out.insert(0, 0.0)
        max_eval = int(self.cfg.get("simulation.max_grid_points", 9) or 9)
        if len(out) > max_eval and max_eval >= 2:
            step = (len(out) - 1) / (max_eval - 1)
            pick = {out[0], out[-1]}
            for i in range(1, max_eval - 1):
                pick.add(out[round(i * step)])
            out = sorted(pick)
        return out

    def max_addable(self, aid: str) -> float:
        """单只 ETF 还能加多少钱（受现金 / 单只 max_weight / 黄金 5% 硬上限约束）。"""
        cash = self.cash
        total = self.total_assets if self.total_assets > 0 else sum(self.base_mv.values())
        cur = float(self.base_mv.get(aid, 0.0))
        cls = self.class_of.get(aid)
        if cls == "gold":
            cap = float(self.cfg.get("portfolio.gold_max", 0.05))
            limit_cap = max(0.0, cap * total - cur)
        else:
            max_w = None
            for a in (getattr(self.cfg, "assets", None) or []):
                if str(a.get("id")) == str(aid):
                    max_w = a.get("max_weight")
                    break
            limit_cap = cash if max_w is None else max(0.0, float(max_w) * total - cur)
        return max(0.0, min(cash, limit_cap))

    def batch_cap(self, aid: str, gap_amount: float | None = None) -> dict:
        """单批买入的安全上限（防止一次把现金打光，与分批建仓纪律一致）。

        上限 = min( 可投资现金 × cash_pct, 目标缺口 × gap_pct, 单只可加上限 )
        """
        cash = self.cash
        pct_cash = float(self.cfg.get("simulation.first_batch_cap_pct_of_cash", 33) or 33) / 100.0
        pct_gap = float(self.cfg.get("simulation.first_batch_cap_pct_of_gap", 50) or 50) / 100.0
        hard = self.max_addable(aid)
        limits = {"可投资现金": cash * pct_cash, "单只权重上限": hard}
        if gap_amount is not None and gap_amount > 0:
            limits["目标仓位缺口"] = float(gap_amount) * pct_gap
        cap = min(limits.values()) if limits else hard
        return {
            "cap": max(0.0, min(cap, hard)),
            "limits": {k: round(v, 0) for k, v in limits.items()},
            "pct_of_cash": pct_cash * 100,
            "pct_of_gap": pct_gap * 100,
        }

    # ---------------------------------------------------------------- 单只最优
    def optimize_asset(self, aid: str, *, grid: list[float] | None = None,
                       baseline: dict | None = None,
                       gap_amount: float | None = None) -> dict:
        """对单只 ETF 做金额网格模拟，给出【边际组合贡献最大】的金额。"""
        baseline = baseline or self.evaluate(self.base_mv)
        if baseline is None:
            return {"available": False, "message": self.prepare_error or "组合基准无法计算",
                    "amount": 0.0}

        tol = float(self.cfg.get("simulation.effective_tolerance", 1.0) or 1.0)
        min_improve = float(self.cfg.get("simulation.min_improve", 1.0) or 1.0)
        top_n = int(self.cfg.get("simulation.report_top_amounts", 8) or 8)

        hard_cap = self.max_addable(aid)
        grid = grid if grid is not None else self.amount_grid()
        grid = sorted({0.0} | {round(min(g, hard_cap), 0) for g in grid if g <= hard_cap + 1e-9})

        sims: list[dict] = []
        for amt in grid:
            mv = dict(self.base_mv)
            mv[aid] = mv.get(aid, 0.0) + amt
            after = baseline if amt <= 0 else self.evaluate(mv)
            if after is None:
                continue
            sims.append(self._compare(aid, amt, baseline, after, mv))
        if not sims:
            return {"available": False, "message": "无法模拟该 ETF", "amount": 0.0}

        def eligible(s: dict) -> bool:
            return (float(s["marginal_score"] or 0.0) >= 50.0 + min_improve
                    and float(s["improve_quality"] or 0.0) >= 0.0
                    and float(s["amount"]) > 0)

        # 只在"能让整个组合变好"的方案中挑选（§50/§51）
        pool = [s for s in sims if eligible(s)]
        if pool:
            best_score = max(float(s["marginal_score"] or 0.0) for s in pool)
            near = [s for s in pool if float(s["marginal_score"] or 0.0) >= best_score - tol]
            # 同等边际效果下取【更省钱】的方案
            raw = min(near, key=lambda s: (float(s["amount"]), -float(s["marginal_score"] or 0.0)))
            # 批次安全上限：不一次把现金打光
            cap_info = self.batch_cap(aid, gap_amount)
            cap = float(cap_info["cap"])
            capped = float(raw["amount"]) > cap + 1e-9
            amount = cap if capped else float(raw["amount"])
            amount = max((float(s["amount"]) for s in sims if float(s["amount"]) <= amount + 1e-9),
                         default=0.0)
            pick = next((s for s in sims if abs(float(s["amount"]) - amount) < 1e-9), raw)
            is_wait = float(pick["amount"]) <= 0
            reason = (f"买入 {int(pick['amount']):,} 元后组合相似状态历史频率"
                      f"（研究参考）"
                      f"{pick['improve_prob60']:+.1f} 个百分点、期望收益 "
                      f"{pick['improve_exp60']:+.2f} 个百分点、1年最大回撤 "
                      f"{pick['improve_dd1y']:+.2f} 个百分点，组合质量 "
                      f"{pick['improve_quality']:+.1f} 分")
            if capped and not is_wait:
                reason += (f"；模型最优金额为 {int(raw['amount']):,} 元，"
                           f"但为遵守分批建仓纪律，第一批按安全上限 {int(amount):,} 元执行")
        else:
            zero = next((s for s in sims if float(s["amount"]) <= 0), sims[0])
            pick, raw = zero, zero
            is_wait = True
            if any(float(s["marginal_score"] or 0.0) > 50.0 for s in sims):
                reason = "有金额能改善风险收益，但组合质量仍会小幅下降 → 按纪律 WAIT"
            else:
                reason = ("不交易时整个组合的风险收益结构已经最好"
                          "（任何买入都不能带来足够改善）→ WAIT")
            cap_info = self.batch_cap(aid, gap_amount)
            capped = False

        return {
            "available": True,
            "id": aid,
            "name": self.names.get(aid, aid),
            "asset_class": self.class_of.get(aid),
            "amount": float(pick["amount"]),
            "optimal_amount": float(raw["amount"]),
            "capped": bool(capped),
            "batch_cap": cap_info,
            "hard_cap": round(hard_cap, 0),
            "baseline": baseline,
            "best": pick,
            "simulations": sims,
            "top_amounts": sorted(sims, key=lambda s: -float(s["marginal_score"] or 0.0))[:top_n],
            "reason": reason,
            "is_wait": is_wait,
        }

    def _compare(self, aid: str, amount: float, base: dict, after: dict,
                 mv: dict) -> dict:
        ms = self.marginal_score(base, after)
        sh, act = self.lots_for_amount(aid, float(amount))

        def d(k, nd=2):
            a, b = after.get(k), base.get(k)
            if a is None or b is None:
                return None
            return round(float(a) - float(b), nd)

        return {
            "id": aid,
            "name": self.names.get(aid, aid),
            "asset_class": self.class_of.get(aid),
            "amount": round(float(amount), 0),
            "raw_price": self.raw_prices.get(aid),
            "shares": sh, "actual_amount": act,
            "after_mv": round(mv.get(aid, 0.0), 2),
            "cash_after": after.get("cash_value"),
            "weight_after_pct": _r(after.get("weights_total", {}).get(aid, 0.0) * 100, 2),
            "class_after": after.get("classes"),
            "gap_after_pp": after.get("gap_avg_pp"),
            "marginal_score": ms["score"],
            "marginal_parts": ms["parts"],
            "marginal_delta": ms["delta"],
            "quality": after.get("quality"),
            "quality_before": base.get("quality"),
            "improve_quality": d("quality", 1),
            "prob20": after.get("prob20"),
            "prob60": after.get("prob60"),
            "prob60_oos": after.get("prob60_oos"),
            "prob120": after.get("prob120"),
            "improve_prob60": d("prob60", 1),
            "improve_prob120": d("prob120", 1),
            "exp60": after.get("exp60"),
            "improve_exp60": d("exp60"),
            "dd60": after.get("dd60"),
            "improve_dd60": d("dd60"),
            "max_dd_1y": after.get("max_dd_1y"),
            "improve_dd1y": d("max_dd_1y"),
            "vol_annual": after.get("vol_annual"),
            "improve_vol": d("vol_annual"),
            "sharpe": after.get("sharpe"),
            "improve_sharpe": d("sharpe"),
            "payoff60": after.get("payoff60"),
        }

    def plan_batches(self, aid: str, *, max_batches: int | None = None) -> dict:
        """分批建仓阶梯：**每一批都在"前一批已执行"的组合上重新计算最优金额**（§12/§13）。

        注意：第 2 批及以后本应在"条件满足时"才计算。这里给出的是
        【假设市场状态不变、前一批立即执行】下的前瞻估计，用于让用户知道
        "下一批大概多少钱、什么情况下会变成 0 元"，实际执行前会用当时的最新数据重算。
        """
        bc = max(1, int(max_batches or self.cfg.get("portfolio.batch_count", 4) or 4))
        batches: list[dict] = []
        mv = dict(self.base_mv)
        total_gap = self.target_gap(aid)
        invested = 0.0
        cur = self
        for i in range(1, bc + 1):
            if i > 1:
                try:
                    cur = self.sibling(mv)
                except Exception:  # noqa: BLE001
                    break
            gap_now = max(0.0, total_gap - invested)
            res = cur.optimize_asset(aid, gap_amount=gap_now)
            amt = float(res.get("amount") or 0.0)
            row = {
                "batch": i,
                "amount": amt,
                "pct": round(100.0 / bc, 1),
                "status": "可执行" if amt > 0 else "等待",
                "done": False,
                "marginal_score": (res.get("best") or {}).get("marginal_score"),
                "improve_prob60": (res.get("best") or {}).get("improve_prob60"),
                "improve_exp60": (res.get("best") or {}).get("improve_exp60"),
                "improve_quality": (res.get("best") or {}).get("improve_quality"),
                "capped": bool(res.get("capped")),
                "reason": res.get("reason"),
                "conditional": i > 1,
            }
            batches.append(row)
            if amt <= 0:
                for j in range(i + 1, bc + 1):
                    batches.append({
                        "batch": j, "amount": 0.0, "pct": round(100.0 / bc, 1),
                        "status": "等待", "done": False, "conditional": True,
                        "reason": "上一批已判定为等待，后续批次同样等待（需重新满足买入条件）",
                    })
                break
            invested += amt
            mv = dict(mv)
            mv[aid] = mv.get(aid, 0.0) + amt
        return {
            "asset_id": aid,
            "asset_name": self.names.get(aid, aid),
            "batches": batches,
            "invested_total": round(invested, 0),
            "gap_total": round(total_gap, 0),
            "remaining_gap": round(max(0.0, total_gap - invested), 0),
            "note": "每批金额都在上一批执行后的新组合上重新模拟得出，因此各批金额可以不相等；"
                    "市场条件恶化时后续批次会自动变成 0 元（WAIT）。",
        }

    def target_gap(self, aid: str) -> float:
        """相对【战略目标权重】的市值缺口（元）。"""
        tw = 0.0
        for a in (getattr(self.cfg, "assets", None) or []):
            if str(a.get("id")) == str(aid):
                tw = float(a.get("target_weight") or 0.0)
                break
        total = self.total_assets if self.total_assets > 0 else sum(self.base_mv.values())
        return max(0.0, tw * total - float(self.base_mv.get(aid, 0.0)))

    # ---------------------------------------------------------------- 全局最优
    def optimize_all(self, candidates: Iterable[str], *, top_n: int = 3) -> dict:
        """对所有候选 ETF 做完整金额网格模拟，挑出【最能改善整个组合】的那一笔。"""
        baseline = self.evaluate(self.base_mv)
        grid = self.amount_grid()
        per: dict[str, dict] = {}
        for aid in candidates:
            per[aid] = self.optimize_asset(aid, grid=grid, baseline=baseline,
                                           gap_amount=self.target_gap(aid))

        flat: list[dict] = []
        for aid, res in per.items():
            if res.get("available"):
                flat.extend(res.get("simulations", []))
        flat.sort(key=lambda s: (-float(s.get("marginal_score") or 0.0),
                                 float(s.get("amount") or 0.0)))
        # 全局最优只从"各只 ETF 的推荐方案"中选（已含批次安全上限与资格判定）
        recommended = [res["best"] for res in per.values()
                       if res.get("available") and not res.get("is_wait")
                       and float(res.get("amount") or 0) > 0]
        recommended.sort(key=lambda s: (-float(s.get("marginal_score") or 0.0),
                                        float(s.get("amount") or 0.0)))
        min_improve = float(self.cfg.get("simulation.min_improve", 1.0) or 1.0)
        eligible = [s for s in recommended
                    if float(s.get("marginal_score") or 0.0) >= 50.0 + min_improve
                    and float(s.get("improve_quality") or 0.0) >= 0.0]
        return {
            "available": bool(baseline),
            "baseline": baseline,
            "grid": grid,
            "per_asset": per,
            "best_trade": eligible[0] if eligible else None,
            "recommended": recommended,
            "best_available": flat[0] if flat else None,
            "eligible_count": len(eligible),
            "top_schemes": flat[:top_n],
            "n_eval": self.n_eval,
        }

    # ---------------------------------------------------------------- 再平衡
    def simulate_rebalance(self, gold_id: str, levels_pct: Sequence[float] = (10, 8, 5)
                           ) -> dict:
        """再平衡也必须模拟：黄金降到 X% 后组合结构与质量如何变化。

        不做"超配了就机械一次性卖到 5%"，而是给出【最合理的再平衡方案】。
        """
        baseline = self.evaluate(self.base_mv)
        if baseline is None:
            return {"available": False}
        gold_mv = float(self.base_mv.get(gold_id, 0.0))
        total = self.total_assets if self.total_assets > 0 else sum(self.base_mv.values())
        cur_pct = gold_mv / total * 100 if total else 0.0
        cap = float(self.cfg.get("portfolio.gold_max", 0.05)) * 100

        rows: list[dict] = []
        if gold_mv > 0:
            gold_price = self.raw_prices.get(gold_id)
            for lv in levels_pct:
                target_mv = total * float(lv) / 100.0
                if gold_mv <= target_mv + 1e-9:
                    continue
                sell = gold_mv - target_mv
                # §29：卖出份额必须 100 份整数倍——对理论卖出额取
                # floor/ceil 两个整手候选，分别真实模拟后择优。
                cands: list[tuple[int, float]] = []
                if gold_price and gold_price > 0:
                    raw_lots = sell / (gold_price * 100.0)
                    import math as _math
                    for n in {max(0, int(_math.floor(raw_lots))),
                              int(_math.ceil(raw_lots))}:
                        if n <= 0:
                            continue
                        cands.append((n * 100, round(n * 100 * gold_price, 2)))
                evaluated: list[dict] = []
                if cands:
                    for shares, actual in cands:
                        mv = dict(self.base_mv)
                        mv[gold_id] = max(0.0, gold_mv - actual)
                        after_c = self.evaluate(mv)
                        if after_c is None:
                            continue
                        evaluated.append({
                            "shares": shares, "actual": actual,
                            "after": after_c,
                            "realized_pct": (mv[gold_id] / total * 100
                                             if total else None),
                        })
                if evaluated:
                    # 优先组合质量更高；同质量选实际占比更接近目标的
                    evaluated.sort(key=lambda c: (
                        -(float(c["after"].get("quality") or 0)),
                        abs((c["realized_pct"] or 0) - float(lv))))
                    pick = evaluated[0]
                    after = pick["after"]
                    actual = pick["actual"]
                else:
                    mv = dict(self.base_mv)
                    mv[gold_id] = target_mv
                    after = self.evaluate(mv)
                    if after is None:
                        continue
                    actual = sell
                    pick = {"shares": None, "actual": actual,
                            "realized_pct": round(float(lv), 1)}
                ms = self.marginal_score(baseline, after)
                rows.append({
                    "target_pct": round(float(lv), 1),
                    "sell_amount": round(actual, 0),
                    "theoretical_amount": round(sell, 0),
                    "sell_shares": pick.get("shares"),
                    "sell_price": gold_price,
                    "realized_pct": pick.get("realized_pct"),
                    "cash_after": round(float(baseline["cash_value"]) + actual, 0),
                    "quality": after.get("quality"),
                    "improve_quality": _r((after.get("quality") or 0) - (baseline.get("quality") or 0), 1),
                    "marginal_score": ms["score"],
                    "prob60": after.get("prob60"),
                    "improve_prob60": _r((after.get("prob60") or 0) - (baseline.get("prob60") or 0), 1),
                    "exp60": after.get("exp60"),
                    "improve_exp60": _r((after.get("exp60") or 0) - (baseline.get("exp60") or 0), 2),
                    "max_dd_1y": after.get("max_dd_1y"),
                    "improve_dd1y": _r((after.get("max_dd_1y") or 0) - (baseline.get("max_dd_1y") or 0), 2),
                    "vol_annual": after.get("vol_annual"),
                    "sharpe": after.get("sharpe"),
                    "classes": after.get("classes"),
                })

        best = None
        if rows:
            best_q = max(float(r["quality"] or 0) for r in rows)
            near = [r for r in rows if float(r["quality"] or 0) >= best_q - 1.0]
            best = min(near, key=lambda r: float(r["sell_amount"]))
        return {
            "available": bool(rows),
            "gold_id": gold_id,
            "gold_name": self.names.get(gold_id, gold_id),
            "current_pct": _r(cur_pct, 2),
            "cap_pct": _r(cap, 2),
            "over_cap": bool(cur_pct > cap + 1e-6),
            "baseline": baseline,
            "options": rows,
            "best": best,
            "note": "再平衡按『组合质量是否改善』择优；不做机械清仓，允许保留在 5% 上限附近。",
        }

    # ---------------------------------------------------------------- 落盘行
    def simulation_rows(self, aid: str, res: dict) -> list[dict]:
        """把一次网格模拟展开成可落盘的行（需求第五十六条）。"""
        base = res.get("baseline") or {}
        out: list[dict] = []
        for s in res.get("simulations", []):
            classes = s.get("class_after") or {}
            out.append({
                "asset_id": aid,
                "asset_name": res.get("name"),
                "asset_class": res.get("asset_class"),
                "amount": s.get("amount"),
                "weight_after_pct": s.get("weight_after_pct"),
                "bond_pct": _r((classes.get("bond") or 0) * 100, 2),
                "stock_pct": _r((classes.get("stock") or 0) * 100, 2),
                "gold_pct": _r((classes.get("gold") or 0) * 100, 2),
                "cash_pct": _r((classes.get("cash") or 0) * 100, 2),
                "cash_after": s.get("cash_after"),
                "prob20": s.get("prob20"),
                "prob60": s.get("prob60"),
                "prob60_oos": s.get("prob60_oos"),
                "prob120": s.get("prob120"),
                "exp60": s.get("exp60"),
                "dd60": s.get("dd60"),
                "dd1y": s.get("max_dd_1y"),
                "vol_annual": s.get("vol_annual"),
                "sharpe": s.get("sharpe"),
                "quality": s.get("quality"),
                "quality_before": base.get("quality"),
                "improve_quality": s.get("improve_quality"),
                "marginal_score": s.get("marginal_score"),
                "improve_prob60": s.get("improve_prob60"),
                "improve_exp60": s.get("improve_exp60"),
                "improve_dd1y": s.get("improve_dd1y"),
                "is_best": bool(abs(float(s.get("amount") or 0) - float(res.get("amount") or 0)) < 1e-9),
            })
        return out


def risk_contribution_rows(panel: "pf.Panel", window: int = 120) -> list[dict]:
    """每个 ETF 对整体组合波动的贡献（需求第三十条）。"""
    rc = pf.risk_contribution(panel, window)
    return rc.get("rows", []) if rc.get("available") else []


__all__ = ["PortfolioSimulator", "nav_with_cash", "scaled_rets",
           "risk_contribution_rows", "CLASS_LABEL", "DEFAULT_WEIGHTS",
           "DEFAULT_SPANS"]
