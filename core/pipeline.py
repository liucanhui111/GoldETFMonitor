"""完整流水线：数据 → 指标 → 单ETF评分 → 组合统计 → 组合决策。

run_full() 是 UI、命令行、测试、回测共用的唯一入口，保证"看到的"和
"算出来的"是同一套东西。
"""
from __future__ import annotations

import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from data_sources import http_util
from data_sources.etf_source import ETFDataSource
from data_sources.http_util import STATUS
from data_sources.macro_source import MacroHub

from . import decision
from . import indicators, marginal, optimizer, scoring, signals, storage
from . import portfolio as pf
from .config import get_config
from .histstats import SimilarStateEngine, build_feature_series


def _asset_row(asset: dict) -> dict:
    return {
        "asset": asset, "raw": None, "ind": None, "hist": {}, "snapshot": None,
        "scores": None, "error": None, "weight_pct": None, "gap_pct": None,
        "value": None, "target_weight_pct": (asset.get("target_weight") or 0) * 100,
    }


def run_full(cfg=None, progress=None, use_cache_first: bool = False,
             with_macro: bool = True, save: bool = True) -> dict:
    cfg = cfg or get_config()
    t0 = time.time()
    warnings: list[str] = []
    errors: list[str] = []

    # 历史数据按组合账户分目录落盘（不同组合互不覆盖）
    try:
        storage.set_portfolio(cfg.portfolio_id)
    except Exception:  # noqa: BLE001
        pass

    # 主机熔断参数（避免不可达的数据源把首次加载拖成十几分钟）
    try:
        http_util.configure_breakers(cfg)
    except Exception:  # noqa: BLE001
        pass

    def step(msg: str) -> None:
        if progress:
            try:
                progress(msg)
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------
    # 1. 单 ETF：数据 → 指标 → 特征 → 历史相似状态统计
    # ------------------------------------------------------------------
    ds = ETFDataSource(cfg)
    cost = cfg.trade_cost()
    hist_weights = cfg.get("hist.feature_weights", {})
    rows: list[dict] = []
    assets = cfg.enabled_assets()

    # 1.0 并发预取行情：每只 ETF 一次请求，串行时冷启动要 30 秒以上，
    #     并发后按最慢的一只计时（指标/统计计算本身很快，不是瓶颈）。
    fetched: dict[str, tuple[dict | None, str]] = {}

    def _grab(asset: dict) -> tuple[str, dict | None, str]:
        try:
            return str(asset.get("id")), ds.fetch(asset, use_cache_first), ""
        except Exception as exc:  # noqa: BLE001
            return str(asset.get("id")), None, f"{type(exc).__name__}: {exc}"

    if assets:
        workers = max(1, min(int(cfg.get("app.fetch_workers", 3) or 3), len(assets)))
        step(f"并发获取 {len(assets)} 只 ETF 行情…（{workers} 路并行）")
        try:
            with ThreadPoolExecutor(max_workers=workers,
                                    thread_name_prefix="etf-fetch") as pool:
                for aid, raw, err in pool.map(_grab, assets):
                    fetched[aid] = (raw, err)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"并发预取异常，回退为逐个获取：{type(exc).__name__}: {exc}")
            fetched = {}

    for asset in assets:
        row = _asset_row(asset)
        name = asset.get("name") or asset.get("id")
        aid = str(asset.get("id"))
        if aid in fetched:
            raw, ferr = fetched[aid]
            if ferr:
                row["error"] = ferr
                errors.append(f"{name}：{ferr}")
        else:  # 并发预取不可用时的兜底：逐个获取
            step(f"获取 {name} 行情…")
            try:
                raw = ds.fetch(asset, use_cache_first)
            except Exception as exc:  # noqa: BLE001
                raw = None
                row["error"] = f"{type(exc).__name__}: {exc}"
                errors.append(f"{name}：{row['error']}\n{traceback.format_exc(limit=2)}")
        if not raw:
            row["error"] = row["error"] or "所有数据源均失败"
            warnings.append(f"{name} 数据源异常，显示『暂无数据』")
            rows.append(row)
            continue

        try:
            ind = indicators.compute_all(
                raw["dates"], raw["open"], raw["high"], raw["low"], raw["close"],
                cfg, asset.get("asset_class", "stock"),
            )
            step(f"计算 {name} 技术指标与历史统计…")
            feats = build_feature_series(ind, cfg)
            engine = SimilarStateEngine(feats, hist_weights, cfg)
            hist = engine.all_horizons(ind["n"] - 1, ind["close"], cost)
            row.update({"raw": raw, "ind": ind, "hist": hist})
            row["snapshot"] = signals.etf_snapshot(asset, ind, hist, cfg)
            # 价格来路（供界面标注「这个价是哪个源、是否本地缓存、缓存多久」）
            # —— 只做展示与透明度，不参与任何判断。
            try:
                row["snapshot"]["data_source"] = (raw or {}).get("source")
                row["snapshot"]["from_cache"] = bool((raw or {}).get("from_cache"))
                row["snapshot"]["cache_age_minutes"] = (raw or {}).get("cache_age_minutes")
            except Exception:  # noqa: BLE001
                pass
            row["features"] = feats
            row["engine"] = engine
        except Exception as exc:  # noqa: BLE001
            row["error"] = f"{type(exc).__name__}: {exc}"
            errors.append(f"{name} 指标计算失败：{row['error']}\n{traceback.format_exc(limit=3)}")
            warnings.append(f"{name} 指标计算失败")
        rows.append(row)

    ok_rows = [r for r in rows if r.get("snapshot")]
    if not ok_rows:
        return {
            "ok": False, "ts": datetime.now(), "rows": rows, "warnings": warnings,
            "errors": errors, "message": "全部 ETF 均无有效数据，无法生成组合判断",
            "data_status": [s.to_dict() for s in STATUS.all()],
            "elapsed_sec": round(time.time() - t0, 1),
        }

    # ------------------------------------------------------------------
    # 2. 当前仓位（含现金/待投资资金；按【总资产】口径，四类合计 100%）
    # ------------------------------------------------------------------
    cur = optimizer.current_weights(ok_rows, cfg)
    if cur["etf_value"] <= 0:
        # 未填写持仓 → 退化为配置目标权重，并在界面明确提示
        tw = {r["asset"]["id"]: float(r["asset"].get("target_weight") or 0.0) for r in ok_rows}
        tot = sum(tw.values()) or 1.0
        by_asset = {k: v / tot for k, v in tw.items()}
        by_class: dict[str, float] = {}
        for r in ok_rows:
            c = r["asset"].get("asset_class") or "other"
            by_class[c] = by_class.get(c, 0.0) + by_asset[r["asset"]["id"]]
        cur = {"total_assets": 0.0, "etf_value": 0.0, "cash_value": 0.0,
               "cash_pct": 0.0, "by_asset": by_asset, "by_asset_total": by_asset,
               "by_class": by_class, "source": "配置目标权重（持仓未填写）",
               "target_sum_pct": round(sum(tw.values()) * 100, 2),
               "portfolio_id": cfg.portfolio_id}
        warnings.append("未填写持仓数量/成本，当前仓位按配置目标权重展示（请到『设置』填写真实持仓）")
        weights_source = "配置目标权重（持仓未填写）"
    else:
        weights_source = cur["source"]
        if cur.get("cash_value") is not None and cur["cash_value"] < -1e-6:
            weights_source += "（总资产小于 ETF 市值，数据异常）"

    for r in ok_rows:
        aid = r["asset"]["id"]
        r["weight_pct"] = round(cur["by_asset_total"].get(aid, 0.0) * 100, 2)
        r["etf_weight_pct"] = round(cur["by_asset"].get(aid, 0.0) * 100, 2)
        r["value"] = r["snapshot"].get("market_value")
        tgt = (r["asset"].get("target_weight") or 0.0) * 100
        r["gap_pct"] = round(tgt - r["weight_pct"], 2)

    # ------------------------------------------------------------------
    # 3. 组合面板与组合统计（【真实组合】口径：ETF 真实权重 + 现金 0 收益）
    # ------------------------------------------------------------------
    step("构建组合面板…")
    datasets = {r["asset"]["id"]: {"dates": r["ind"]["dates"], "close": r["ind"]["close"]}
                for r in ok_rows}
    names = {r["asset"]["id"]: r["asset"].get("name") for r in ok_rows}
    asset_inds = {r["asset"]["id"]: r["ind"] for r in ok_rows}
    class_of = {r["asset"]["id"]: (r["asset"].get("asset_class") or "other")
                for r in ok_rows}
    base_mv = {r["asset"]["id"]: float(r["snapshot"].get("market_value") or 0.0)
               for r in ok_rows}
    panel = pf.Panel(datasets, cur["by_asset"], names)
    total_assets = float(cur.get("total_assets") or 0.0)
    share_base = min(1.0, cur["etf_value"] / total_assets) if total_assets > 0 else 1.0
    nav_base = marginal.nav_with_cash(panel, share_base)
    rets_base = marginal.scaled_rets(panel, share_base)

    step("计算组合最大回撤 / 波动率 / 相关性 / 风险贡献…")
    pstats = pf.portfolio_stats(panel, cfg, nav=nav_base, rets=rets_base)
    pstats["risk_series"] = pf.risk_score_series(panel, cfg)
    pstats["etf_only"] = pf.portfolio_stats(panel, cfg)      # ETF 满仓口径（对比用）
    pstats["cash_share"] = round(1.0 - share_base, 4)
    if pstats["span"].get("years") and pstats["span"]["years"] < 3:
        warnings.append(
            f"组合可回溯历史仅 {pstats['span']['years']} 年（受最晚上市 ETF 限制），"
            "长周期统计可信度下降")

    corr = pf.correlation_matrices(panel, cfg)
    rcontrib = pf.risk_contribution(panel, 120)

    asset_scores_raw: dict[str, dict] = {}
    step("构建组合级状态特征…")
    p_feats = pf.build_portfolio_features(panel, asset_inds, asset_scores_raw, cfg,
                                          nav=nav_base, port_ret=rets_base)
    p_weights = dict(cfg.get("hist.feature_weights", {}))
    p_weights.update({"corr60": 0.8, "navrel1y": 1.0})
    p_weights = {k: v for k, v in p_weights.items() if k in p_feats}

    targets_cfg = cfg.get("portfolio.target_class", {"bond": 0.50, "stock": 0.45,
                                                     "gold": 0.05})

    # ---- 真实未复权交易价（§16/§17：份数/金额换算专用） ----
    step("获取真实交易价（未复权）…")
    raw_prices: dict[str, float] = {}
    from .data_validation import fetch_raw_trade_price_cached
    for r in ok_rows:
        try:
            px = fetch_raw_trade_price_cached(r["asset"]["thscode"], cfg)
            if px and px > 0:
                raw_prices[r["asset"]["id"]] = float(px)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"{r['asset'].get('name')} 真实交易价获取失败：{exc}")
    if len(raw_prices) < len(ok_rows):
        missing_px = [r["asset"].get("name") for r in ok_rows
                      if r["asset"]["id"] not in raw_prices]
        warnings.append("以下 ETF 交易价缺失，份数换算不可用（不伪造）："
                        + "、".join(missing_px))

    # ---- 唯一真源：组合边际模拟引擎（真实组合，现金按 0 收益计入） ----
    step("统计组合历史盈利概率（真实组合口径，含待投资现金）…")
    # 【最终战略目标组合】各 ETF 目标市值（仅作参照，绝不当"当前组合"用）
    target_mv = {}
    for r in ok_rows:
        a = r["asset"]
        tw = float(a.get("target_weight") or 0.0)
        if tw > 0:
            target_mv[a["id"]] = tw * float(total_assets or 0.0)
    sim = marginal.PortfolioSimulator(
        datasets, names, asset_inds, class_of, cfg,
        base_mv=base_mv, total_assets=total_assets, cost=cost,
        targets_class=targets_cfg, feature_weights=hist_weights,
        horizons=tuple(cfg.get("hist.horizons", [5, 20, 60, 120]) or [5, 20, 60, 120]),
        target_mv=target_mv,
        raw_prices=raw_prices,
    )
    baseline = sim.evaluate(sim.base_mv)
    if baseline is not None:
        p_hist = baseline["hist"]
        p_hist_last = p_hist.get(60) or {}
        if baseline.get("diversification") is not None:
            corr["diversification_score"] = baseline["diversification"]
        weights_source += ("；组合概率/回撤按『真实组合』口径（"
                           f"ETF {baseline['share_etf'] * 100:.1f}% + 现金 "
                           f"{baseline['cash_value']:,.0f} 元按 0 收益计入）")
        if sim.prepare_error:
            warnings.append(f"组合边际引擎提示：{sim.prepare_error}")
    else:
        warnings.append("组合边际引擎未能建立基准，回退为 ETF 满仓口径统计（结果偏乐观）")
        p_engine = SimilarStateEngine(p_feats, p_weights, cfg)
        p_hist = p_engine.all_horizons(len(panel.dates) - 1, panel.nav, cost)
        p_hist_last = p_engine.stats_at(len(panel.dates) - 1, panel.nav, 60, cost)

    # ------------------------------------------------------------------
    # 3.5 唯一决策概率（最终修正）
    # 独立事件去重后的逐日滚动样本外 h=60 —— 全系统只认这一个决策概率，
    # 首页 / 边际模拟 / 交易前后对比 / 触发条件 / 买入等级判断全部引用它。
    # 重叠滚动 OOS 与相似状态频率只作研究参考，绝不参与决策。
    # 必须在这里（entry_gate 之前）算好，否则等级判断会退回旧口径。
    # ------------------------------------------------------------------
    daily_oos60: dict | None = None
    if sim is not None and getattr(sim, "ready", False):
        try:
            step("计算唯一决策概率（独立事件去重逐日样本外 h=60）…")
            _doos = sim.daily_oos_predictions((60,), step=1)
            daily_oos60 = (_doos.get("horizons") or {}).get(60) or None
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"唯一决策概率计算已跳过：{type(exc).__name__}: {exc}")
            daily_oos60 = None
    dec_prob = decision.resolve_decision_probability(p_hist, daily_oos60)
    if baseline is not None:
        # 让边际模拟/交易前后对比也能拿到同一个决策概率（同一 dict 对象）
        baseline["dec_prob"] = dec_prob
        # 基础率 / 相对基础率提升 统一改用决策口径，避免文档与运行口径分裂
        if dec_prob.get("base_rate") is not None:
            baseline["prob60_base"] = dec_prob.get("base_rate")
        if dec_prob.get("lift_pp") is not None:
            baseline["prob60_uplift"] = dec_prob.get("lift_pp")
        # 决策口径的独立事件样本外概率（供报告/自检引用）
        if dec_prob.get("prob_used") is not None:
            baseline["prob60_decision"] = dec_prob.get("prob_used")
            baseline["prob60_decision_source"] = dec_prob.get("source")

    # ------------------------------------------------------------------
    # 4. 单 ETF 评分
    # ------------------------------------------------------------------
    step("计算单 ETF 评分…")
    for r in ok_rows:
        snap = r["snapshot"]
        flags = signals.etf_flags(r["asset"], r["ind"], snap, cfg)
        sc = signals.etf_scores(r["asset"], r["ind"], snap, r["hist"], flags,
                                r["gap_pct"], cfg)
        r["scores"] = sc
        asset_scores_raw[r["asset"]["id"]] = sc

    # ------------------------------------------------------------------
    # 5. 组合层判断
    # ------------------------------------------------------------------
    stock_rows = [r for r in ok_rows if r["asset"].get("asset_class") == "stock"]
    stock_trend_avg = _avg([r["scores"]["trend"] for r in stock_rows])
    risk = signals.portfolio_risk_score(pstats, corr, p_hist, stock_trend_avg)
    quality = (baseline["quality_detail"] if baseline
               else signals.portfolio_quality(pstats, corr, p_hist, cur["by_class"],
                                              targets_cfg, cfg))
    entry = signals.portfolio_entry_signal(p_hist, risk, quality, ok_rows,
                                           cur["by_class"], targets_cfg, cfg)
    rebal = signals.rebalance_plan(ok_rows, cur["by_class"], targets_cfg, risk, cfg)

    # ---- 第二阶段：数据一致性 → 组合买入门槛（样本外优先）→ 加多少 ----
    step("检查持仓数据一致性…")
    consistency = decision.consistency_check(ok_rows, cur, cfg)
    if not consistency["ok"]:
        warnings.append("【数据一致性异常】" + "；".join(consistency["errors"]))
        warnings.append("已按规则禁止生成任何买入/卖出金额建议，请先修正好『设置』里的持仓与总资产。")

    gate = decision.entry_gate(p_hist, risk, ok_rows, consistency, cfg,
                              dec_prob=dec_prob)
    entry_score = decision.portfolio_entry_score(p_hist, corr, risk, ok_rows, cfg)

    # 黄金
    step("获取宏观与黄金专用指标…")
    macro: dict = {}
    if with_macro:
        try:
            macro = MacroHub(cfg).load(use_cache_first, progress=step)
            warnings.extend(macro.get("warnings", []))
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"宏观数据获取失败：{type(exc).__name__}: {exc}")
            macro = {"warnings": []}
    radar = (macro.get("gold_radar") or {})
    gold_row = next((r for r in ok_rows if r["asset"].get("asset_class") == "gold"), None)
    # 黄金宏观加权评分（8 指标，用户策略 2026-10 版）
    try:
        from .gold_macro import gold_macro_score as _gms
        gold_ind = (gold_row or {}).get("ind")
        macro["gold_macro_score"] = _gms(macro, gold_ind)
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"黄金宏观评分失败：{type(exc).__name__}: {exc}")
        macro["gold_macro_score"] = {}
    gold_ctl = signals.gold_position_control(cfg, cur["by_class"], gold_row, radar,
                                             macro_score=macro.get("gold_macro_score"))

    waiting = signals.waiting_capital(cur["by_class"], targets_cfg, entry)

    step("计算动态战术配置…")
    opts = optimizer.optimize(ok_rows, risk["state"], gate["grade"], cfg)

    # ---- 组合边际模拟：买什么 / 买多少 / 买完组合会怎样（§9·§10·§11·§36） ----
    step("模拟买入后【整个组合】会怎样（金额网格）…")
    marginal_res: dict = {}
    try:
        cands: list[str] = []
        seen: set[str] = set()
        for r in ok_rows:
            aid = r["asset"]["id"]
            if aid in seen or r["scores"]["flags"].get("blocked"):
                continue
            gap = sim.target_gap(aid)
            if gap <= 0:
                continue
            seen.add(aid)
            cands.append(aid)
        cands.sort(key=lambda a: -sim.target_gap(a))
        cands = cands[:int(cfg.get("simulation.max_candidates", 6) or 6)]
        if cands:
            marginal_res = sim.optimize_all(cands)
        else:
            marginal_res = {"available": bool(baseline), "baseline": baseline,
                            "grid": sim.amount_grid(), "per_asset": {},
                            "best_trade": None, "recommended": [], "top_schemes": []}
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"组合边际模拟已跳过：{type(exc).__name__}: {exc}")
        marginal_res = {"available": False, "baseline": baseline, "per_asset": {}}

    # ---- 再平衡也必须模拟（§42·§43）：黄金降到 10/8/5% 分别怎样 ----
    step("模拟再平衡方案（黄金回到战略上限）…")
    rebal_sim: dict = {}
    try:
        gold_id = next((r["asset"]["id"] for r in ok_rows
                        if r["asset"].get("asset_class") == "gold"), None)
        if gold_id and baseline is not None:
            rebal_sim = sim.simulate_rebalance(
                gold_id, cfg.get("simulation.rebalance_levels_pct", [10, 8, 5]))
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"再平衡模拟已跳过：{type(exc).__name__}: {exc}")
        rebal_sim = {"available": False}

    # ---- 【BUY PERMISSION GATE】读取策略验证得到的可信等级（缺失 → 研究级）----
    #   可信等级决定“买入权限”，不是买入等级：研究级不开放实际买入建议，
    #   组合模拟的理论最优金额只能作为“研究参考”。必须在生成 plan/three 之前
    #   确定，才能保证"今天是否投入新增资金"与最终状态不矛盾。
    trust: dict = {"trust_level": "研究级", "source": "default"}
    try:
        from . import strategy_validation as _sv   # 延迟导入，避免模块级循环依赖
        trust = _sv.load_trust_level(cfg)
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"可信等级读取失败（按研究级处理）：{type(exc).__name__}: {exc}")
    trust_level = trust.get("trust_level") or "研究级"
    perm = decision.buy_permission(cfg, trust_level)

    step("生成今日操作方案（买什么 / 买多少 / 下一笔何时买）…")
    plan = decision.allocation_plan(ok_rows, cur, opts, gate, consistency, risk, cfg,
                                    sim=sim, marginal=marginal_res, perm=perm)
    # ---- 分批建仓阶梯：每一批都在上一批执行后的新组合上重新计算（§12·§13） ----
    # 只有在"今天真的有一笔可执行"时才展开阶梯；否则（例如黄金超配需先再平衡、
    # 或边际模拟判定 0 元最优）阶梯统一回落为"等待"，避免与今日结论互相矛盾。
    ladder: dict = {}
    try:
        pick_id = None
        if plan.get("picks") and float(plan.get("total_first_batch_amount") or 0) > 0:
            pick_id = plan["picks"][0]["id"]
        if pick_id and consistency.get("ok"):
            ladder = sim.plan_batches(pick_id,
                                      max_batches=int(cfg.get("portfolio.batch_count", 4) or 4))
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"分批阶梯计算已跳过：{type(exc).__name__}: {exc}")
        ladder = {}

    batches = decision.batch_progress(gate, plan, cur, cfg, ladder=ladder)
    triggers = decision.next_buy_triggers(ok_rows, plan, gate, cur, targets_cfg,
                                          risk, consistency, cfg)
    three = decision.three_questions(gate, rebal, risk, consistency, plan, cur, cfg,
                                     perm=perm)
    today = decision.today_action(gate, three, risk, rebal, plan, consistency, cur, cfg,
                                  marginal=marginal_res, rebal_sim=rebal_sim,
                                  trust_level=trust_level)
    why_not = decision.why_not_buy(gate, plan, rebal, risk, cur, consistency, cfg,
                                   marginal=marginal_res)
    explain = decision.explainer(gate, three, plan, rebal, cur, risk, today["key"], cfg,
                                 marginal=marginal_res)
    comparison = decision.trade_comparison(marginal_res, plan, cfg)

    # 用第二阶段的等级/概率同步旧的入口，保证所有消费方口径一致
    _sync_entry(entry, gate, plan)

    final_op = signals.final_decision(entry, quality, risk, ok_rows, rebal,
                                      gold_ctl, waiting)

    # 【最终战略目标组合】参照（仅展示；绝不用于回答"现在该不该买"）
    try:
        target_sim = sim.simulate_target() if sim is not None else None
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"战略目标组合参照计算已跳过：{type(exc).__name__}: {exc}")
        target_sim = None

    # ------------------------------------------------------------------
    # 保守主概率（最终修正）：已在上方 3.5 节统一计算（dec_prob），
    # 这里不再重复计算，确保首页/决策/边际模拟口径完全一致。
    # ------------------------------------------------------------------
    pkg = decision.build_decision({
        "gate": gate, "entry_score": entry_score, "plan": plan, "batches": batches,
        "triggers": triggers, "three": three, "today": today, "why_not": why_not,
        "explain": explain, "consistency": consistency, "p_hist": p_hist,
        "marginal": marginal_res, "ladder": ladder, "comparison": comparison,
        "rebal_sim": rebal_sim, "target_sim": target_sim,
        # 【BUY PERMISSION GATE】可信等级 → 买入权限（研究级不开放实际买入）
        "trust_level": trust_level,
        # 唯一决策概率（独立事件去重样本外 h=60）：首页/决策主口径
        "daily_oos60": daily_oos60,
        "dec_prob": dec_prob,
        # 真实未复权交易价（份数换算）与最小资金适配（§16-§22）
        "raw_prices": raw_prices,
        "capital_fit": sim.min_capital_items(
            {r["asset"]["id"]: float(r["asset"].get("target_weight") or 0.0)
             for r in ok_rows}),
        # 首页(5 秒块)需要真实组合权重与战略目标来算"实际 vs 目标"、"黄金状态"
        "weights": cur, "target_class": targets_cfg,
        "gold_cap": float(cfg.get("portfolio.gold_max", 0.05) or 0.05) * 100.0,
    })

    snap_out = {
        "ok": True,
        "ts": datetime.now(),
        "elapsed_sec": round(time.time() - t0, 1),
        "cfg": cfg,
        "rows": rows,
        "assets": {r["asset"]["id"]: r for r in ok_rows},
        "portfolio": {
            "panel": panel,
            "stats": pstats,
            "correlation": corr,
            "risk_contribution": rcontrib,
            "hist": p_hist,
            "hist_60_detail": p_hist_last,
            "risk": risk,
            "quality": quality,
            "entry": entry,
            "rebalance": rebal,
            "gold_control": gold_ctl,
            "waiting": waiting,
            "weights": cur,
            "weights_source": weights_source,
            "target_class": targets_cfg,
            "target_class_with_cash": {**dict(targets_cfg), "cash": 0.0},
            "optimizer": opts,
            "decision": final_op,
            "features": p_feats,
            # ---- 第二阶段：普通用户看得懂的决策包 ----
            "plan": pkg,
            # ---- 第三阶段：组合边际模拟 / 分批阶梯 / 再平衡模拟 ----
            "simulator": sim,
            "baseline": baseline,
            "daily_oos60": daily_oos60,
            "dec_prob": dec_prob,
            "marginal": marginal_res,
            "rebal_sim": rebal_sim,
            "target_sim": target_sim,
            "portfolio_id": cfg.portfolio_id,
            "portfolio_name": cfg.portfolio_name,
            # ---- 【BUY PERMISSION GATE】可信等级 / 买入权限（最高优先级闸门）----
            "trust": trust,
            "trust_level": trust_level,
            "buy_permission": (pkg.get("buy_permission")
                               if isinstance(pkg, dict) else None),
        },
        "macro": macro,
        "warnings": warnings,
        "errors": errors,
        "data_status": [s.to_dict() for s in STATUS.all()],
    }

    # ---- 盘中参考报价（仅供参考显示，绝不参与任何判断）----
    # 判断一律沿用日线收盘价（snapshot.price），这里只是多给用户一个「此刻大概什么价」。
    # 短超时 + 不重试 + 失败置空，任何异常都不得影响主流程。
    snap_out["realtime"] = {}
    snap_out["realtime_error"] = ""
    if cfg.get("data_sources.realtime_quote", True):
        try:
            from data_sources.realtime_quote import fetch_quotes
            step("获取盘中参考报价…")
            snap_out["realtime"] = fetch_quotes([a.get("thscode") for a in assets])
        except Exception as exc:  # noqa: BLE001
            snap_out["realtime_error"] = f"{type(exc).__name__}: {exc}"

    # ---- 决策闸门链 ----
    # DATA → FRESHNESS → CONSISTENCY → TRUST → TRADE VALUE → RISK → POSITION LIMIT
    # 任一失败都只能 WAIT/HOLD/REBALANCE，绝不绕过（研究级禁止 BUY 的闸门保留）。
    #
    # 【单一事实源】新鲜度（FRESHNESS）只在这里算一次：
    #   ① 交给闸门链，决定能否 BUY；
    #   ② 存进 snap["freshness"]，供首屏「数据陈旧」横幅直接展示。
    # 这样「数据过期 → 禁止买入」的原因在第一屏就能看到，不再只藏在 85KB 旧
    # decision 路径的小字里（旧路径不感知新鲜度，此处以新闸门结果为准回流）。
    snap_out["freshness"] = None
    try:
        from .freshness_gate import check_freshness
        snap_out["freshness"] = check_freshness(cfg, snap_out)
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"新鲜度检查已跳过：{type(exc).__name__}: {exc}")
    try:
        from . import decision_engine as _de
        snap_out["decision_engine"] = _de.evaluate_decision(
            cfg, snap_out, trust_level=trust_level,
            freshness=snap_out.get("freshness"))
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"决策闸门链计算已跳过：{type(exc).__name__}: {exc}")
        snap_out["decision_engine"] = None

    # ---- 数据来源与有效性（PROVENANCE）----
    # 把"每个参数来自哪里 / 网址 / 是否正常 / 数据时间(到秒)"汇成一张表，
    # 供设置页直接展示；失败项如实标【错误】并给出原因。
    snap_out["provenance"] = None
    try:
        from .data_provenance import build_provenance
        snap_out["provenance"] = build_provenance(cfg, snap_out)
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"数据来源表生成已跳过：{type(exc).__name__}: {exc}")

    if save:
        step("保存历史数据…")
        try:
            save_history(snap_out)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"历史数据保存失败：{type(exc).__name__}: {exc}")

    return snap_out


def _avg(vals):
    xs = [float(v) for v in vals if v is not None]
    return sum(xs) / len(xs) if xs else None


_GRADE_ACTION = {"S": "分批买入", "A": "分批买入", "B": "小仓试探",
                 "C": "WAIT", "D": "WAIT", "E": "降低风险"}


def _sync_entry(entry: dict, gate: dict, plan: dict) -> None:
    """用第二阶段门槛（样本外优先）同步旧入口，避免两套口径互相矛盾。"""
    bc = int(plan.get("batch_count") or 4)
    grade = gate.get("grade") or "C"
    allowed = {"S": bc, "A": 3, "B": 2, "C": 0, "D": 0, "E": 0}.get(grade, 0)
    if not gate.get("prob_oos"):
        allowed = min(allowed, 2)
    entry.update({
        "grade": grade,
        "action": _GRADE_ACTION.get(grade, "WAIT"),
        "prob60": gate.get("prob_used"),
        "prob_oos": gate.get("prob_oos"),
        "prob_full": gate.get("prob_full"),
        "prob_source": gate.get("prob_source"),
        "credibility": gate.get("credibility"),
        "oos_samples": gate.get("oos_samples"),
        "payoff": gate.get("payoff"),
        "dd60": gate.get("dd60"),
        "samples": gate.get("samples"),
        "allowed_batches": allowed,
        "total_batches": bc,
        "batch_pct": round(100.0 / bc, 1) if bc else 25.0,
        "first_batch_amount": plan.get("total_first_batch_amount"),
        "reasons": list(gate.get("reasons") or []) + list(entry.get("reasons") or []),
    })


# --------------------------------------------------------------------------
# 历史落盘
# --------------------------------------------------------------------------
def save_history(snap: dict) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    date = datetime.now().strftime("%Y-%m-%d")

    etf_rows, ind_rows, sig_rows, reb_rows = [], [], [], []
    for r in snap["rows"]:
        if not r.get("snapshot"):
            continue
        s = r["snapshot"]
        sc = r.get("scores") or {}
        aid = r["asset"]["id"]
        etf_rows.append({
            "date": date, "id": aid, "name": r["asset"].get("name"),
            "thscode": r["asset"].get("thscode"), "price": s.get("price"),
            "data_time": s.get("data_time"), "source": (r.get("raw") or {}).get("source"),
            "from_cache": (r.get("raw") or {}).get("from_cache"),
        })
        ind_rows.append({
            "date": date, "id": aid, "close": s.get("price"),
            "ma20": s["ma"].get("ma20"), "ma60": s["ma"].get("ma60"),
            "ma200": s["ma"].get("ma200"),
            "rsi14": s.get("rsi14"), "wr14": s.get("wr14"), "atr_pct": s.get("atr_pct"),
            "bias20": s.get("bias20"), "ret20": s.get("ret20"),
            "relpos1y": s["relpos"].get("1y"), "relpos2y": s["relpos"].get("2y"),
            "dd1y_max": (s["dd"].get("1y") or {}).get("max_drawdown_pct"),
            "dd2y_max": (s["dd"].get("2y") or {}).get("max_drawdown_pct"),
            "swing": s["swing"].get("state"),
            "trend_score": sc.get("trend"), "position_score": sc.get("position"),
            "shortterm_score": sc.get("shortterm"), "swing_score": sc.get("swing"),
            "buy_score": sc.get("buy_score"), "entry_quality": sc.get("entry_quality"),
            "grade": sc.get("grade"),
            "prob60": sc.get("prob60"), "exp60": sc.get("exp60"),
            "payoff60": sc.get("payoff"),
            "weight_pct": r.get("weight_pct"), "target_pct": r.get("target_weight_pct"),
        })

    p = snap["portfolio"]
    pk = p.get("plan") or {}
    w = p["weights"]
    portfolio_rows = [{
        "date": date, "ts": now,
        "portfolio_id": p.get("portfolio_id"),
        "total_assets": w.get("total_assets"),
        "etf_value": w.get("etf_value"),
        "cash_value": w.get("cash_value"),
        "weights_source": p["weights_source"],
        "bond_pct": round((w["by_class"].get("bond") or 0) * 100, 2),
        "stock_pct": round((w["by_class"].get("stock") or 0) * 100, 2),
        "gold_pct": round((w["by_class"].get("gold") or 0) * 100, 2),
        "cash_pct": round((w["by_class"].get("cash") or 0) * 100, 2),
        "risk_score": p["risk"]["score"], "market_state": p["risk"]["state"],
        "quality_score": p["quality"]["score"],
        "today_action": (pk.get("today") or {}).get("text"),
        "entry_score": (pk.get("entry_score") or {}).get("score"),
        "consistency_ok": (pk.get("consistency") or {}).get("ok"),
        "prob20": (p["hist"].get(20) or {}).get("win_rate_pct"),
        "prob60": (p["hist"].get(60) or {}).get("win_rate_pct"),
        "prob60_oos": (p["hist"].get(60) or {}).get("oos_win_rate_pct"),
        "prob120": (p["hist"].get(120) or {}).get("win_rate_pct"),
        "exp60": (p["hist"].get(60) or {}).get("expected_return_pct"),
        "dd1y": (p["stats"].get("dd_windows") or {}).get("1y", {}).get("current_drawdown_pct"),
        "dd1y_max": (p["stats"].get("dd_windows") or {}).get("1y", {}).get("max_drawdown_pct"),
        "dd2y_max": (p["stats"].get("dd_windows") or {}).get("2y", {}).get("max_drawdown_pct"),
        "vol_annual": p["stats"].get("annual_vol_pct"),
        "entry_grade": p["entry"]["grade"], "action": (pk.get("today") or {}).get("text")
        or p["decision"]["action"],
        "first_batch_amount": (pk.get("plan") or {}).get("total_first_batch_amount"),
    }]

    sig_rows.append({
        "date": date, "ts": now,
        "portfolio_id": p.get("portfolio_id"),
        "today_action": (pk.get("today") or {}).get("text"),
        "entry_grade": p["entry"]["grade"], "action": p["decision"]["action"],
        "new_money": (pk.get("three") or {}).get("new_money_text"),
        "rebalance": (pk.get("three") or {}).get("rebalance_text"),
        "reduce_overweight": (pk.get("three") or {}).get("reduce_text"),
        "allowed_batches": p["entry"]["allowed_batches"],
        "market_state": p["risk"]["state"], "risk_score": p["risk"]["score"],
        "quality_score": p["quality"]["score"],
        "prob60": (p["hist"].get(60) or {}).get("win_rate_pct"),
        "prob60_oos": (p["hist"].get(60) or {}).get("oos_win_rate_pct"),
        "exp60": (p["hist"].get(60) or {}).get("expected_return_pct"),
        "wait_ratio_pct": p["waiting"].get("wait_ratio_pct"),
        "cash_value": w.get("cash_value"),
        "cash_pct": round((w["by_class"].get("cash") or 0) * 100, 2),
        "gold_pct": round((w["by_class"].get("gold") or 0) * 100, 2),
        "first_batch_amount": (pk.get("plan") or {}).get("total_first_batch_amount"),
        "summary": " | ".join((pk.get("explain") or p["decision"]["lines"])[:3]),
    })
    for i in p["rebalance"]["items"]:
        reb_rows.append({"date": date, "id": i["id"], "name": i["name"],
                         "current_pct": i["current_pct"], "target_pct": i["target_pct"],
                         "deviation_pct": i["deviation_pct"], "level": i["level"],
                         "action": i["action"]})

    macro_rows = macro_history_rows(snap.get("macro") or {}, date)

    # ---- 组合边际模拟落盘（§55·§56）：候选 × 金额网格 + 再平衡各档 ----
    sim_rows: list[dict] = []
    rbsim_rows: list[dict] = []
    pid = p.get("portfolio_id")
    try:
        simulator = p.get("simulator")
        marg = p.get("marginal") or {}
        if simulator is not None and hasattr(simulator, "simulation_rows"):
            for aid, res in (marg.get("per_asset") or {}).items():
                if not (res or {}).get("available"):
                    continue
                for row in simulator.simulation_rows(aid, res):
                    sim_rows.append({"date": date, "ts": now, "portfolio_id": pid, **row})
    except Exception:  # noqa: BLE001
        sim_rows = []
    try:
        rb = p.get("rebal_sim") or {}
        best_t = (rb.get("best") or {}).get("target_pct")
        for o in rb.get("options") or []:
            rbsim_rows.append({
                "date": date, "ts": now, "portfolio_id": pid,
                "gold_id": rb.get("gold_id"), "gold_name": rb.get("gold_name"),
                "current_pct": rb.get("current_pct"), "cap_pct": rb.get("cap_pct"),
                "target_pct": o.get("target_pct"), "sell_amount": o.get("sell_amount"),
                "cash_after": o.get("cash_after"), "quality": o.get("quality"),
                "improve_quality": o.get("improve_quality"),
                "marginal_score": o.get("marginal_score"),
                "prob60": o.get("prob60"), "improve_prob60": o.get("improve_prob60"),
                "exp60": o.get("exp60"), "improve_exp60": o.get("improve_exp60"),
                "max_dd_1y": o.get("max_dd_1y"), "improve_dd1y": o.get("improve_dd1y"),
                "vol_annual": o.get("vol_annual"), "sharpe": o.get("sharpe"),
                "is_best": bool(best_t is not None and o.get("target_pct") == best_t),
            })
    except Exception:  # noqa: BLE001
        rbsim_rows = []

    storage.save_etf_history(etf_rows)
    storage.save_indicator_history(ind_rows)
    storage.save_portfolio_history(portfolio_rows)
    storage.save_signal_history(sig_rows)
    if reb_rows:
        storage.save_rebalance_history(reb_rows)
    if macro_rows:
        storage.save_macro_history(macro_rows)
    if sim_rows:
        storage.save_portfolio_simulation(sim_rows)
    if rbsim_rows:
        storage.save_rebalance_simulation(rbsim_rows)


def macro_history_rows(macro: dict, date: str) -> list[dict]:
    out: list[dict] = []

    def add(item, value, extra=""):
        out.append({"date": date, "item": item, "value": value, "extra": extra})

    gm = macro.get("gold_metrics") or {}
    for k, label in (("last", "国际金价"), ("dev_ma200_pct", "金价偏离MA200%"),
                     ("ret20_pct", "金价20日%"), ("ret60_pct", "金价60日%")):
        if gm.get(k) is not None:
            add(label, gm[k])
    um = macro.get("usd_metrics") or {}
    if um.get("last") is not None:
        add("美元指数DXY", um["last"], um.get("trend", ""))
    rm = macro.get("real_rate_metrics") or {}
    if rm.get("last") is not None:
        add("美国10年期实际利率", rm["last"], rm.get("trend", ""))
    nm = macro.get("nominal_rate_metrics") or {}
    if nm.get("last") is not None:
        add("美国10年期国债收益率", nm["last"], nm.get("trend", ""))
    fg = macro.get("fear_greed") or {}
    if fg.get("score") is not None:
        add("官方Gold Fear&Greed", fg["score"], fg.get("zone", ""))
    else:
        add("官方Gold Fear&Greed", None, "暂无数据")
    proxy = macro.get("gold_sentiment_proxy") or {}
    if proxy.get("available"):
        add("黄金情绪代理（本地计算）", proxy.get("score"),
            "代理，非官方F&G")
    bm = macro.get("bpgdm_metrics") or {}
    # 真实 BPGDM 与代理值**字段分离**：真实值不可用时显示"暂无数据"，
    # 绝不把代理值冒充成真实 BPGDM。
    if bm.get("real_available"):
        add("真实BPGDM", bm.get("bpgdm_real"),
            f"来源 {bm.get('bpgdm_real_source') or ''}"
            + (f"；分位{bm.get('percentile_2y')}%" if bm.get("percentile_2y") is not None else ""))
    else:
        add("真实BPGDM", None, "暂无数据")
        if bm.get("available"):
            add("BPGDM代理（非真实BPGDM）", bm.get("bpgdm_proxy"),
                f"代理算法：{bm.get('bpgdm_proxy_algo') or ''}"
                + (f"；2年分位 {bm.get('percentile_2y')}%" if bm.get("percentile_2y") is not None else ""))
    gv = macro.get("gvz_metrics") or {}
    if gv.get("available"):
        add("黄金波动率指数GVZ", gv.get("gvz"),
            f"来源 {gv.get('gvz_source') or ''}；数据日 {gv.get('gvz_data_date') or ''}；"
            f"系统研究分档『{gv.get('band_name') or ''}』"
            + (f" 5日{gv.get('chg_5d'):+}" if gv.get("chg_5d") is not None else ""))
    else:
        add("黄金波动率指数GVZ", None, "暂无数据（UNKNOWN，不伪造）")
    pm = macro.get("policy_metrics") or {}
    if pm.get("available"):
        add("Fed利率预期(2Y-3M)", pm.get("last"), pm.get("direction", ""))
    ms = macro.get("gold_macro_score") or {}
    if ms.get("score") is not None:
        add("黄金宏观评分", ms.get("score"), ms.get("grade_label", ""))
    radar = macro.get("gold_radar") or {}
    if radar.get("total"):
        add("黄金底部雷达", f"{radar.get('hit')}/{radar.get('total')}", radar.get("status", ""))
    return out


def health_snapshot() -> dict:
    """数据源与缓存健康度（设置页用）。"""
    from data_sources.http_util import DATA_CACHE

    return {
        "sources": [s.to_dict() for s in STATUS.all()],
        "any_abnormal": STATUS.any_abnormal(),
        "cache": DATA_CACHE.list_items(),
    }
