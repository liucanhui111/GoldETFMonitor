"""决策引擎（第二阶段）：把组合层判断翻译成普通用户 5 秒能看懂的操作结论。

本模块只回答"人话"问题，不引入新的技术指标：
  1. 今天到底怎么操作？（7 个最终状态之一）
  2. 是否投入新增资金 / 是否再平衡 / 是否降低超配资产？（三个【互相独立】的问题）
  3. 整个组合未来 60 日历史盈利概率多少？样本外多少？可信度如何？
  4. 现在买什么？买多少？第一笔多少钱？下一笔什么时候买？
  5. 为什么买 / 为什么不买 / 为什么等待？
  6. 黄金是不是超配？需不需要再平衡？超配多少金额？

硬规则（不可被"指标好看"绕过）：
  · 目标仓位 ≠ 必须买满；WAIT 是完全合法的结果，且允许"第一批 0 元"；
  · 黄金永远 ≤ 5%，超配时不接受任何加仓，只做再平衡；
  · 组合买入门槛优先用【样本外】60 日盈利概率；样本外不可用时降级并降低买入信心；
  · 持仓/现金/总资产/目标权重存在不一致时，禁止输出任何买入金额建议。
"""
from __future__ import annotations

from .scoring import clip

# --------------------------------------------------------------------------
# 7 个最终状态
#   emoji 只用于 GUI；text 为 GBK 安全文案，供命令行报告/控制台使用。
# --------------------------------------------------------------------------
ACTION_STATES: dict[str, dict] = {
    "GREEN_BUY":      {"text": "建议分批买入", "full": "🟢 建议分批买入", "color": "up"},
    "GREEN_PROBE":    {"text": "小仓试探",     "full": "🟢 小仓试探",     "color": "up"},
    "YELLOW_HOLD":    {"text": "持有观察",     "full": "🟡 持有观察",     "color": "warn"},
    "ORANGE_PAUSE":   {"text": "暂停加仓",     "full": "🟠 暂停加仓",     "color": "warn"},
    "RED_DEFEND":     {"text": "防守/不要买",  "full": "🔴 防守/不要买",  "color": "danger"},
    "BLUE_REBALANCE": {"text": "再平衡",       "full": "🔵 再平衡",       "color": "accent"},
    "WHITE_WAIT":     {"text": "WAIT 等待",    "full": "⚪ WAIT 等待",    "color": "muted"},
}

CLASS_LABEL = {"bond": "债券", "stock": "股票", "gold": "黄金", "cash": "现金/待投资"}
GRADE_ORDER = {"S": 5, "A": 4, "B": 3, "C": 2, "D": 1, "E": 0}


def _num(v, nd: int = 2):
    try:
        if v is None:
            return None
        return round(float(v), nd)
    except (TypeError, ValueError):
        return None


def _map(v, lo: float, hi: float):
    if v is None:
        return None
    if hi == lo:
        return 50.0
    return max(0.0, min(100.0, (float(v) - lo) / (hi - lo) * 100.0))


def _avg(xs):
    ys = [float(x) for x in xs if x is not None]
    return sum(ys) / len(ys) if ys else None


def state_of(key: str) -> dict:
    d = ACTION_STATES.get(key) or ACTION_STATES["WHITE_WAIT"]
    return {"key": key, "text": d["text"], "full": d["full"], "color": d["color"]}


# ==========================================================================
# 【最高优先级：BUY PERMISSION GATE】可信等级决定"买入权限"，不是买入等级
#   研究级的研究结论（模拟显示某笔买入可能改善组合）只能作“研究参考”，
#   不能转换成实际 BUY / SPLIT_BUY / TRIAL_BUY 状态。
#   只有可信等级达到预先定义的更高等级，才允许开放"小仓试探 / 建议分批买入"。
# ==========================================================================
TRUST_LEVEL_ORDER: dict[str, int] = {
    "研究级": 0, "观察级": 1, "辅助交易级": 2, "实盘辅助级": 3,
}
# 开放"实际买入建议"所需的最低可信等级（可在 config.trust.buy_permission_min_level 覆盖）。
DEFAULT_BUY_PERMISSION_MIN_LEVEL = "辅助交易级"
# 实际买入类动作（受权限闸门约束；其余均为观察/防守/再平衡/等待，永远允许）。
BUY_ACTIONS = ("GREEN_BUY", "GREEN_PROBE")          # 建议分批买入 / 小仓试探
BUY_ACTION_ALIASES = ("BUY", "SPLIT_BUY", "TRIAL_BUY")  # 对外等价命名（测试用）


def buy_permission(cfg, trust_level: str | None) -> dict:
    """最高优先级闸门：根据系统可信等级判定是否开放"实际买入建议"。

    · trust_level 缺失/未知 → 按最保守的【研究级】处理（不开放买入）；
    · 研究级 / 观察级 → 不开放；只有 ≥ 阈值（默认 辅助交易级）才开放；
    · 无论是否开放，PortfolioSimulator 的理论最优金额都保留为"研究参考"。
    """
    level = str(trust_level) if trust_level else "研究级"
    if level not in TRUST_LEVEL_ORDER:
        level = "研究级"
    min_level = DEFAULT_BUY_PERMISSION_MIN_LEVEL
    if cfg is not None:
        try:
            min_level = str(cfg.get("trust.buy_permission_min_level",
                                    DEFAULT_BUY_PERMISSION_MIN_LEVEL)
                            or DEFAULT_BUY_PERMISSION_MIN_LEVEL)
        except Exception:  # noqa: BLE001
            min_level = DEFAULT_BUY_PERMISSION_MIN_LEVEL
    if min_level not in TRUST_LEVEL_ORDER:
        min_level = DEFAULT_BUY_PERMISSION_MIN_LEVEL
    allowed = TRUST_LEVEL_ORDER[level] >= TRUST_LEVEL_ORDER[min_level]
    if allowed:
        reason = (f"可信等级 {level} ≥ 开放门槛 {min_level}：允许输出买入建议")
    else:
        reason = (f"可信等级 {level} < 开放门槛 {min_level}：不开放买入权限，"
                  "最终状态只能是 持有观察 / 暂停加仓 / 防守·不要买 / 再平衡 / WAIT")
    return {"allowed": allowed, "trust_level": level, "min_level": min_level,
            "reason": reason}


# ==========================================================================
# 一、数据一致性检查（需求 37 / 38）
# ==========================================================================
def consistency_check(rows: list[dict], cur: dict, cfg) -> dict:
    """每次刷新后自动检查：持仓数量 / 市值 / 总资产 / 现金 / 大类权重 / 权重合计。

    误差阈值默认 0.5%。发现不一致 → ok=False，上层必须禁止生成任何买入金额建议。
    """
    tol = float(cfg.get("decision.consistency_tolerance_pct", 0.5))
    checks: list[dict] = []
    errors: list[str] = []
    warns: list[str] = []

    def add(name: str, ok: bool, detail: str, *, fatal: bool = True):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            (errors if fatal else warns).append(detail)

    etf_value = float(cur.get("etf_value") or 0.0)
    total = float(cur.get("total_assets") or 0.0)
    cash = float(cur.get("cash_value") or 0.0)

    # 1) ETF 持仓数量与市值是否自洽
    mv_sum = 0.0
    bad_asset = []
    for r in rows:
        a = r["asset"]
        s = r.get("snapshot") or {}
        shares = float(a.get("shares") or 0.0)
        price = s.get("price")
        mv = s.get("market_value")
        if shares < 0:
            bad_asset.append(f"{a.get('name')} 份额为负")
        if price is None or mv is None:
            bad_asset.append(f"{a.get('name')} 缺价格/市值")
            continue
        mv_sum += float(mv)
    add("ETF 持仓数量与市值", not bad_asset,
        "；".join(bad_asset) if bad_asset else f"合计市值 {mv_sum:,.2f} 元，与逐只加总一致")

    # 2) 总资产 ≥ ETF 市值（否则现金为负）
    add("总资产 ≥ ETF 市值", total + 1e-6 >= etf_value,
        f"总资产 {total:,.2f} 元 / ETF 市值 {etf_value:,.2f} 元 / "
        f"现金 {cash:,.2f} 元",
        fatal=True)
    if cur.get("cash_value") is not None and cash < -1e-6:
        add("现金不为负", False,
            f"现金为负（{cash:,.2f} 元）：总资产小于 ETF 市值，请修正『设置』里的持仓或总资产")

    # 3) 大类权重合计 = 100%（±0.5pp）
    cls = cur.get("by_class") or {}
    wsum = sum(float(v) for v in cls.values()) * 100.0
    add("大类权重合计 = 100%", abs(wsum - 100.0) <= tol,
        f"债券/股票/黄金/现金 合计 {wsum:.2f}%")

    # 4) 目标权重合计 = 100%
    tsum = float(cur.get("target_sum_pct") or 0.0)
    if tsum > 0:
        add("目标权重合计 = 100%", abs(tsum - 100.0) <= tol,
            f"目标权重合计 {tsum:.2f}%", fatal=False)

    # 3b) 黄金权重是否远超战略上限（疑似份额口径错误，例如误用了历史 4500 份）
    gold_pct = float(cls.get("gold") or 0.0) * 100
    gold_cap = float(cfg.get("portfolio.gold_max", 0.05)) * 100
    if gold_pct > gold_cap + 15.0:
        add("黄金权重是否合理", False,
            f"黄金占总资产 {gold_pct:.1f}%，远超 {gold_cap:.0f}% 的战略上限，"
            "疑似黄金持仓份额误用了历史数据（例如 4500 份，正确应为 1900 份），"
            "请到『设置』核对持仓份额")

    # 5) 总资产是否已填写
    if total <= 0:
        add("总资产已填写", False,
            "未填写『总资产』：无法计算『现金/待投资资金』，建议到『设置』填写账户总资产",
            fatal=False)
    if cur.get("cash_pct") is not None and float(cur.get("cash_pct") or 0) > 90:
        add("现金比例合理", False,
            f"现金占总资产 {cur.get('cash_pct')}%，请确认总资产填写是否正确", fatal=False)

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warns,
        "checks": checks,
        "tolerance_pct": tol,
        "max_error_pct": round(abs(wsum - 100.0), 3),
    }


# ==========================================================================
# 二、组合买入评分（需求 9）
# ==========================================================================
def portfolio_entry_score(p_hist: dict, corr: dict, risk: dict,
                          ok_rows: list[dict], cfg) -> dict:
    """Portfolio Entry Score 0-100（辅助结论，**不是胜率**）。"""
    w = cfg.get("scores.entry_score", {
        "prob_60": 25, "exp_60": 20, "payoff": 15, "max_dd": 15,
        "market_state": 10, "trend": 5, "position": 5, "diversification": 5})
    h60 = p_hist.get(60) or {}
    prob = h60.get("oos_win_rate_pct")
    if prob is None:
        prob = h60.get("win_rate_pct")
    exp60 = h60.get("expected_return_pct")
    payoff = h60.get("payoff_ratio")
    dd60 = h60.get("max_drawdown_pct")

    stocks = [r for r in ok_rows if r["asset"].get("asset_class") == "stock"]
    trend = _avg([r["scores"].get("trend") for r in stocks])
    position = _avg([r["scores"].get("position") for r in stocks])

    factors = {
        "prob_60": _map(prob, 45, 78),
        "exp_60": _map(exp60, -3, 8),
        "payoff": _map(payoff, 0.5, 3.0),
        "max_dd": _map(dd60, -25, -3),
        "market_state": (100.0 - float(risk.get("score"))) if risk.get("score") is not None else None,
        "trend": trend,
        "position": (100.0 - position) if position is not None else None,
        "diversification": corr.get("diversification_score"),
    }
    tot = sum(float(w.get(k, 0)) for k, v in factors.items() if v is not None)
    score = (sum(float(w.get(k, 0)) * v for k, v in factors.items() if v is not None) / tot
             if tot else None)
    return {
        "score": _num(score, 1),
        "components": [{"name": k, "value": _num(v, 1), "weight": w.get(k)}
                       for k, v in factors.items()],
        "note": "Entry Score 只是辅助分（0-100），不是胜率；80 分不等于 80% 胜率。",
    }


# ==========================================================================
# 三·零、唯一决策概率（最终修正）：全系统只认一个概率口径
# ==========================================================================
# 决策主口径 = 【独立事件去重后的逐日滚动样本外 60 日概率】
# （逐日推进、60日窗口互不重叠、无未来函数）。
# 其余三个数字只作"研究参考"，绝不参与任何决策 / 金额 / 等级判断：
#   · 相似状态历史频率（kNN 邻居条件胜率）
#   · 重叠滚动OOS频率 （逐日预测点、60日窗口重叠）
#   · 无条件基础胜率   （同窗口内不看任何信号）
DECISION_SOURCE_LABELS = {
    "independent_event_oos": "独立事件去重样本外OOS",
    "overlap_rolling_oos": "重叠滚动OOS",
    "conditional_similar_state": "相似状态历史频率",
}
REF_LABEL_OVERLAP = "重叠滚动OOS频率"
REF_LABEL_CONDITIONAL = "相似状态历史频率"


def resolve_decision_probability(p_hist: dict | None,
                                 daily_oos60: dict | None) -> dict:
    """把"到底用哪个概率做决策"收敛成唯一口径（最终修正）。

    返回的 prob_used/source 是全系统唯一决策概率；其余字段仅用于对照展示。
    独立事件去重样本外不可用时，才按 重叠滚动OOS → 相似状态频率 依次降级，
    并把降级原因写进 notes，绝不用"看起来更高"的数字冒充决策概率。
    """
    h60 = (p_hist or {}).get(60) or {}
    do = daily_oos60 or {}
    indep = do.get("realized_win_rate_pct")          # 独立事件去重样本外（决策主口径）
    indep_n = int(do.get("independent") or 0)
    ap = do.get("all_points") or {}
    # 重叠滚动OOS频率（研究参考）：近 1.5 年相似样本的 OOS 频率；
    # 若缺失则退回逐日滚动全样本点频率（同样含重叠窗口）。
    overlap = h60.get("oos_win_rate_pct")
    if overlap is None:
        overlap = ap.get("realized_win_rate_pct")
    rolling_pts = ap.get("realized_win_rate_pct")     # 逐日滚动点频率（全样本含重叠）
    overlap_pts = int(do.get("points") or 0)
    conditional = h60.get("win_rate_pct")             # 相似状态历史频率（研究参考）
    conditional_n = int(h60.get("samples") or 0)
    base = do.get("base_win_rate_pct")
    if base is None:
        base = h60.get("all_history_win_rate_pct")
    lift = do.get("uplift_vs_base_pp")
    ci = [do.get("ci95_low"), do.get("ci95_high")]

    notes: list[str] = []
    if indep is not None:
        source, used = "independent_event_oos", indep
    elif overlap is not None:
        source, used = "overlap_rolling_oos", overlap
        notes.append("独立事件去重样本外不可用，已降级为重叠滚动OOS（可信度下降）")
    elif conditional is not None:
        source, used = "conditional_similar_state", conditional
        notes.append("样本外口径均不可用，已降级为相似状态历史频率（可信度最低）")
    else:
        source, used = None, None

    few = indep_n < 30
    no_edge = (lift is None) or (float(lift) <= 0)
    if few:
        notes.append("样本极少，仅供研究")
    if no_edge:
        notes.append("当前模型没有证明额外择时优势")

    return {
        "prob_used": _num(used, 1),
        "source": source,
        "source_label": DECISION_SOURCE_LABELS.get(source, "暂无数据"),
        "independent_events": indep_n,
        "oos_independent_prob": _num(indep, 1),
        "oos_overlap_prob": _num(overlap, 1),
        "rolling_points_prob": _num(rolling_pts, 1),
        "conditional_prob": _num(conditional, 1),
        "conditional_samples": conditional_n,
        "base_rate": _num(base, 1),
        "lift_pp": _num(lift, 1),
        "ci95": [_num(ci[0], 1), _num(ci[1], 1)],
        "overlap_points": overlap_pts,
        "enough_samples": (not few) if indep is not None else (conditional_n >= 30),
        "no_timing_edge": no_edge,
        "labels": {"overlap": REF_LABEL_OVERLAP, "conditional": REF_LABEL_CONDITIONAL},
        "notes": notes,
    }


# ==========================================================================
# 三、组合买入门槛（需求 8）：优先用样本外概率
# ==========================================================================
def entry_gate(p_hist: dict, risk: dict, ok_rows: list[dict], consistency: dict,
               cfg, dec_prob: dict | None = None) -> dict:
    gates = cfg.get("scores.entry_gate", {})
    h60 = p_hist.get(60) or {}
    prob_full = h60.get("win_rate_pct")
    prob_oos = h60.get("oos_win_rate_pct")
    oos_n = int(h60.get("oos_samples") or 0)
    samples = int(h60.get("samples") or 0)
    exp60 = h60.get("expected_return_pct")
    payoff = h60.get("payoff_ratio")
    dd60 = h60.get("max_drawdown_pct")
    credibility = h60.get("credibility") or h60.get("sample_grade") or "未知"

    # 【唯一决策概率】最终修正：只要可用，一律用独立事件去重样本外概率；
    # 重叠滚动 OOS 与相似状态频率只作研究参考，绝不参与决策/等级判断。
    if dec_prob and dec_prob.get("prob_used") is not None:
        prob_used = dec_prob.get("prob_used")
        prob_oos = dec_prob.get("oos_overlap_prob")      # 研究参考：重叠滚动OOS
        prob_full = dec_prob.get("conditional_prob")      # 研究参考：相似状态频率
        oos_n = int(dec_prob.get("independent_events") or 0)
        use_oos = True
        prob_source = dec_prob.get("source_label") or "独立事件去重样本外OOS"
    else:
        use_oos = (bool(cfg.get("decision.use_oos_first", True))
                   and prob_oos is not None and oos_n >= 10)
        prob_used = prob_oos if use_oos else prob_full
        prob_source = ("样本外" if use_oos
                       else ("全部历史（样本外尚不可用）"
                             if prob_full is not None else "暂无数据"))

    stock_rows = [r for r in ok_rows if r["asset"].get("asset_class") == "stock"]
    chase_all = bool(stock_rows) and all(r["scores"]["flags"].get("no_chase") for r in stock_rows)
    risk_score = risk.get("score") or 0.0

    reasons: list[str] = []
    unmet: list[str] = []

    if not consistency.get("ok"):
        return {
            "grade": "C", "prob_used": _num(prob_used, 1), "prob_oos": _num(prob_oos, 1),
            "prob_full": _num(prob_full, 1), "exp60": _num(exp60, 2), "payoff": _num(payoff, 2),
            "dd60": _num(dd60, 2), "samples": samples, "oos_samples": oos_n,
            "credibility": credibility, "use_oos": use_oos,
            "prob_source": prob_source, "dec_prob": dec_prob,
            "reasons": ["数据一致性异常：禁止生成买入建议（WAIT / 数据异常）"],
            "unmet": ["数据一致性检查未通过"], "chase_all": chase_all,
            "blocked": True,
        }

    if prob_used is None or samples < 30:
        grade = "C"
        reasons.append("组合历史相似样本不足，无法给出高等级买入信号")
    else:
        not_chasing = not (chase_all and (prob_used or 0) >= 58)
        risk_ok = risk_score < 65
        conds = {
            "样本外/等效60日概率≥65%": (prob_used or 0) >= 65,
            "60日期望收益>2%": (exp60 if exp60 is not None else -99) > 2,
            "盈亏比≥1.5": (payoff if payoff is not None else 0) >= 1.5,
            "有效样本≥50": samples >= 50,
            "组合风险没有明显恶化": risk_ok,
            "不是整体风险资产追高": not_chasing,
        }
        # 决策概率来自"独立事件去重样本外"时，其事件数必须同样达标；
        # 绝不用邻居样本数（可能上百）掩盖"独立事件仅十几个"的事实。
        if dec_prob is not None and dec_prob.get("source") == "independent_event_oos":
            conds["独立事件去重样本外事件≥30"] = \
                int(dec_prob.get("independent_events") or 0) >= 30
        if all(conds.values()):
            has_quality = any(
                r["scores"].get("grade") in ("A", "B") and not r["scores"]["flags"].get("blocked")
                for r in stock_rows)
            if (prob_used or 0) >= gates.get("S", {}).get("prob_60", 70) and \
                    (exp60 or -99) > gates.get("S", {}).get("exp_ret_60", 4) and has_quality:
                grade = "S"
            else:
                grade = "A"
            if not use_oos:
                grade = "B" if grade in ("S", "A") else grade
                reasons.append("样本外统计尚不可用，买入信心已下调（最多 B 级）")
                unmet.append("样本外60日概率（尚无数据）")
        elif (prob_used or 0) >= 58 and (exp60 or -99) > 0:
            grade = "B"
            for k, v in conds.items():
                if not v:
                    unmet.append(k)
        elif (prob_used or 0) >= 50 or abs(exp60 or 0) <= 0.5:
            grade = "C"
            for k, v in conds.items():
                if not v:
                    unmet.append(k)
        else:
            grade = "D"
            reasons.append("组合60日历史盈利概率低于50%且期望收益不为正")
            for k, v in conds.items():
                if not v:
                    unmet.append(k)

    if grade == "D" and (exp60 or 0) <= 0 and risk_score >= 60:
        grade = "E"
        reasons.append("组合期望收益为负且风险评分偏高")

    # ---- 否决项（顺序 = 优先级，符合"组合风险优先"） ----
    gold_cap = float(cfg.get("portfolio.gold_max", 0.05))
    gold_now = 0.0
    risk_now = 0.0
    cls = None
    veto = None
    if risk.get("state") == "Crisis":
        veto = "组合进入 Crisis 状态"
    elif risk_score >= 70:
        veto = "组合风险评分过高"
    if veto:
        if GRADE_ORDER.get(grade, 0) > 1:
            grade = "C"
        reasons.append(f"已被风控否决：{veto}")

    # ---- 决策概率本身的诚实提示（最终修正：口径统一后的必然结论） ----
    if dec_prob:
        if (dec_prob.get("source") == "independent_event_oos"
                and int(dec_prob.get("independent_events") or 0) < 30):
            reasons.append(
                f"独立事件去重样本外事件仅 {dec_prob.get('independent_events')} 个（<30）："
                "样本极少，仅供研究")
        if dec_prob.get("no_timing_edge"):
            reasons.append("当前模型没有证明额外择时优势"
                           "（独立事件样本外概率未高于同窗口基础胜率）")

    return {
        "grade": grade,
        "prob_used": _num(prob_used, 1),
        "prob_oos": _num(prob_oos, 1),
        "prob_full": _num(prob_full, 1),
        "exp60": _num(exp60, 2),
        "payoff": _num(payoff, 2),
        "dd60": _num(dd60, 2),
        "samples": samples,
        "oos_samples": oos_n,
        "credibility": credibility,
        "use_oos": use_oos,
        "prob_source": prob_source,
        "dec_prob": dec_prob,
        "reasons": reasons,
        "unmet": unmet,
        "chase_all": chase_all,
        "blocked": False,
        "veto": veto,
    }


# ==========================================================================
# 四、加多少：逐 ETF 分配计划（需求 11 / 12 / 13 / 33 / 39）
# ==========================================================================
def allocation_plan(rows: list[dict], cur: dict, opts: dict, gate: dict,
                    consistency: dict, risk: dict, cfg, *,
                    sim=None, marginal: dict | None = None,
                    perm: dict | None = None) -> dict:
    """逐 ETF 的买入计划。

    第三阶段变化：第一批金额 **不再** 由"目标缺口 ÷ 批数 × 系数"机械得出，
    而是取【组合边际模拟】给出的最优金额（买多少让整个组合变得最好）。
    每只 ETF 的推荐金额都列出来（供对比），但今天真正执行的只有
    `picks` 里那一笔（组合综合质量最高的方案）。
    """
    total = float(cur.get("total_assets") or 0.0)
    cash = float(cur.get("cash_value") or 0.0)
    bc = max(1, int(cfg.get("portfolio.batch_count", 4)))
    ok = bool(consistency.get("ok"))
    gold_cap = float(cfg.get("portfolio.gold_max", 0.05))
    gold_now = float(cur.get("by_class", {}).get("gold") or 0.0)
    gold_over = gold_now > gold_cap + 1e-9
    gold_at = gold_now >= gold_cap - 1e-9
    grade_ok = gate.get("grade") in ("S", "A", "B")
    block_all_new = bool((not ok) or gold_over)
    marg = (marginal or {}).get("per_asset") or {}
    best_trade = (marginal or {}).get("best_trade")

    items: list[dict] = []
    for r in rows:
        a = r["asset"]
        aid = a.get("id")
        cls = a.get("asset_class") or "other"
        s = r["scores"]
        snap = r["snapshot"]
        target_w = float(a.get("target_weight") or 0.0)
        tactic_w = float((opts.get("weights") or {}).get(aid) or 0.0)
        cur_w = float(cur.get("by_asset_total", {}).get(aid) or 0.0)
        mv = float(snap.get("market_value") or 0.0)
        target_amt = target_w * total
        gap_amt = max(0.0, target_amt - mv)
        blocked = bool(s["flags"].get("blocked"))
        grade = s.get("grade")
        eq = s.get("entry_quality")
        rp1 = (snap.get("relpos") or {}).get("1y")

        m = marg.get(aid) or {}
        rec_amt = float(m.get("amount") or 0.0) if m.get("available") else 0.0

        reason = ""
        actionable = False
        if not ok:
            reason = "数据一致性异常：暂不给出金额建议"
        elif gold_over:
            reason = ("黄金已超过 5% 硬上限：今天先再平衡，不投入任何新增资金"
                      "（即使该标的机会很好）")
        elif cls == "gold" and gold_at:
            reason = "黄金已达 5% 硬上限：禁止增加，只能持有或再平衡"
        elif blocked:
            reason = s["flags"].get("block_reason") or "技术形态未确认"
        elif not grade_ok:
            reason = f"组合买入门槛为 {gate.get('grade')} 级，本次不投入新增资金"
        elif grade not in ("S", "A", "B"):
            reason = f"买入质量 {grade or '—'} 级，未达可买入标准"
        elif rec_amt <= 0:
            reason = (m.get("reason") or "组合边际模拟显示：买入不能让整个组合变好，"
                                         "因此不投入新增资金")
        else:
            actionable = True
        if block_all_new:
            actionable = False
            rec_amt = 0.0

        # 今天的动作（普通中文）
        if not ok:
            act = "数据异常 · WAIT"
        elif gold_over and cls == "gold":
            act = "再平衡（减仓）"
        elif gold_over:
            act = "WAIT（先再平衡）"
        elif cls == "gold" and gold_at:
            act = "持有（已达上限）"
        elif blocked and s["flags"].get("no_chase"):
            act = "WAIT（不追涨）"
        elif blocked and s["flags"].get("no_bottom_fishing"):
            act = "WAIT（不抄底）"
        elif actionable:
            act = "第一批" if grade in ("S", "A") else "小仓试探"
        elif grade == "C":
            act = "WAIT"
        else:
            act = "持有"

        pos_label = "高位" if (rp1 is not None and rp1 >= 65) else (
            "低位" if (rp1 is not None and rp1 <= 35) else "中位")

        items.append({
            "id": aid, "name": a.get("name"), "asset_class": cls,
            "class_label": CLASS_LABEL.get(cls, cls),
            "grade": grade, "buy_quality": grade,
            "entry_score": eq,
            "target_pct": _num(target_w * 100, 2),
            "tactic_pct": _num(tactic_w * 100, 2),
            "current_pct": _num(cur_w * 100, 2),
            "gap_pct": _num((target_w - cur_w) * 100, 2),
            "overweight_pp": _num(max(0.0, (cur_w - target_w) * 100), 2),
            "underweight_pp": _num(max(0.0, (target_w - cur_w) * 100), 2),
            "target_amount": _num(target_amt, 0),
            "current_amount": _num(mv, 0),
            "gap_amount": _num(gap_amt, 0),
            "first_batch_amount": _num(rec_amt, 0),
            "remaining_wait_amount": _num(max(0.0, gap_amt - rec_amt), 0),
            # ---- 组合边际贡献（第三阶段核心） ----
            "marginal_score": (m.get("best") or {}).get("marginal_score"),
            "optimal_amount": _num(m.get("optimal_amount"), 0),
            "marginal_capped": bool(m.get("capped")),
            "batch_cap": (m.get("batch_cap") or {}).get("cap"),
            "improve_prob60": (m.get("best") or {}).get("improve_prob60"),
            "improve_exp60": (m.get("best") or {}).get("improve_exp60"),
            "improve_dd1y": (m.get("best") or {}).get("improve_dd1y"),
            "improve_quality": (m.get("best") or {}).get("improve_quality"),
            "marginal_reason": m.get("reason"),
            "marginal_simulated": bool(m.get("available")),
            "prob60": s.get("prob60"), "exp60": s.get("exp60"), "payoff": s.get("payoff"),
            "position_label": pos_label,
            "action": act,
            "actionable": actionable,
            "blocked": blocked,
            "reason": reason,
            "scale_note": "",
            "in_gold_cap": cls != "gold",
        })

    items.sort(key=lambda x: (-float(x["first_batch_amount"] or 0),
                              -GRADE_ORDER.get(x["grade"], 0)))

    # ---- 今天真正执行的只有"组合综合质量最高"的那一笔（§36·§59） ----
    picks: list[dict] = []
    if ok and not block_all_new and best_trade:
        bid = best_trade.get("id")
        for it in items:
            if it["id"] == bid and it["actionable"]:
                it["first_batch_amount"] = _num(best_trade.get("amount"), 0)
                it["remaining_wait_amount"] = _num(
                    max(0.0, float(it["gap_amount"] or 0)
                        - float(it["first_batch_amount"] or 0)), 0)
                it["is_today_pick"] = True
                picks = [it]
                break

    total_first = sum(float(i["first_batch_amount"] or 0) for i in picks)
    if total_first > cash > 0:
        scale = cash / total_first
        for i in picks:
            i["first_batch_amount"] = _num(float(i["first_batch_amount"]) * scale, 0)
            i["scale_note"] = "受可投资现金限制，已按比例缩放"
        total_first = sum(float(i["first_batch_amount"] or 0) for i in picks)

    # ---- 【BUY PERMISSION GATE】买入权限关闭：金额只作研究参考，不作为可执行方案 ----
    #   模拟结果（哪只、多少钱最优）完整保留在 research_picks / research_reference，
    #   但 picks / total_first_batch_amount 归零 → 不会转成实际 BUY。
    permission_blocked = bool(perm) and not bool(perm.get("allowed"))
    research_picks = picks
    research_total = total_first
    if permission_blocked:
        for it in items:
            if it.get("action") in ("第一批", "小仓试探"):
                it["action"] = "研究参考（不开放买入权限）"
            it["research_only"] = True
        for i in research_picks:
            i["is_today_pick"] = False
            i["research_only"] = True
        picks = []
        total_first = 0

    return {
        "items": items,
        "total_first_batch_amount": _num(total_first, 0),
        "deployable_cash": _num(cash, 0),
        "picks": picks,
        "best_trade": best_trade,
        "batch_count": bc,
        "block_all_new": block_all_new,
        # ---- 【BUY PERMISSION GATE】买入权限（可信等级决定，不是买入等级）----
        "buy_permission": perm,
        "permission_blocked": permission_blocked,
        "research_only": permission_blocked,
        "research_picks": research_picks,
        "research_total_amount": _num(research_total, 0),
        "note": ("金额由【组合边际模拟】决定：先模拟 500/1000/2000/3000/5000… 元买入后"
                 "整个组合的质量，再取最优的那一笔；不会自动下单。"
                 + ("（注：当前买入权限关闭，以上金额仅作研究参考，今天不执行。）"
                    if permission_blocked else "")),
    }


# ==========================================================================
# 五、四批资金进度（需求 32）
# ==========================================================================
def batch_progress(gate: dict, plan: dict, cur: dict, cfg, *,
                   ladder: dict | None = None) -> list[dict]:
    """四批资金进度。

    优先使用【分批阶梯】（每批金额都在上一批执行后的新组合上重新模拟得出），
    这样各批金额可以不相等，市场恶化时后续批次会自动变成 0 元（WAIT）。
    """
    bc = max(1, int(cfg.get("portfolio.batch_count", 4)))
    if ladder and ladder.get("batches"):
        out = []
        for b in ladder["batches"][:bc]:
            out.append({
                "batch": b.get("batch"),
                "amount": b.get("amount") or 0.0,
                "pct": round(float(b.get("amount") or 0)
                             / max(1.0, float(ladder.get("gap_total") or 1)) * 100, 1),
                "status": b.get("status"),
                "done": bool(b.get("done")),
                "conditional": bool(b.get("conditional")),
                "marginal_score": b.get("marginal_score"),
                "improve_prob60": b.get("improve_prob60"),
                "improve_exp60": b.get("improve_exp60"),
                "improve_quality": b.get("improve_quality"),
                "reason": b.get("reason"),
                "condition": ("第一批：按组合边际模拟结果执行" if b.get("batch") == 1
                              else "前一批执行后重新模拟；条件不满足则为 0 元（WAIT）"),
            })
        return out

    grade = gate.get("grade")
    allowed = {"S": bc, "A": 3, "B": 2, "C": 0, "D": 0, "E": 0}.get(grade, 0)
    if not gate.get("prob_oos"):
        allowed = min(allowed, 2)
    if not (plan.get("picks") or []) or float(plan.get("total_first_batch_amount") or 0) <= 0:
        allowed = 0
    labels = {
        "S": ["组合概率/期望/盈亏比同时达标", "价格不追高", "趋势确认", "风险未恶化"],
        "A": ["组合概率/期望达标", "个股出现买点", "趋势确认", "风险未恶化"],
        "B": ["组合概率达标", "价格不追高", "趋势确认", "风险未恶化"],
    }.get(grade, ["组合概率达标", "期望收益转正", "盈亏比达标", "风险未恶化"])
    out = []
    for i in range(1, bc + 1):
        done = i <= allowed
        out.append({
            "batch": i,
            "amount": None,
            "pct": round(100.0 / bc, 1) if done else 0.0,
            "status": "已满足" if done else "等待",
            "done": done,
            "condition": labels[(i - 1) % len(labels)],
        })
    return out


# ==========================================================================
# 五之二、交易前后对比（需求 50 / 51）
# ==========================================================================
def trade_comparison(marginal: dict | None, plan: dict, cfg) -> dict:
    """把"不交易"和"执行建议"两个组合摆在一起对比。

    输出结构直接对应首页的【如果今天什么都不买】/【如果今天执行建议】两块。
    """
    marginal = marginal or {}
    base = marginal.get("baseline") or {}
    best = marginal.get("best_trade")
    picks = plan.get("picks") or []
    exec_id = picks[0].get("id") if picks else None
    executable = bool(best) and exec_id == best.get("id") and float(
        plan.get("total_first_batch_amount") or 0) > 0
    no_exec_reason = ""
    if best and not executable:
        for it in (plan.get("items") or []):
            if it.get("id") == best.get("id") and it.get("reason"):
                no_exec_reason = str(it.get("reason"))
                break
        if not no_exec_reason:
            no_exec_reason = "今天不执行（组合门槛或批次纪律未满足）"
    # ---- 唯一决策概率（最终修正）：交易前后对比的主口径 ----
    # 交易前后对比的"组合概率"必须以决策主口径（独立事件去重样本外）为准；
    # 相似状态历史频率只作研究参考，绝不再作为交易前后的主展示数字。
    _dp = base.get("dec_prob") or {}
    _dec_before = _dp.get("prob_used")
    if base:
        before = {
            "prob20": base.get("prob20"), "prob60": base.get("prob60"),
            "prob60_oos": base.get("prob60_oos"), "prob120": base.get("prob120"),
            "exp60": base.get("exp60"), "max_dd_1y": base.get("max_dd_1y"),
            "vol_annual": base.get("vol_annual"), "quality": base.get("quality"),
            "cash_value": base.get("cash_value"),
            "classes": base.get("classes"),
            "prob60_dec": _dec_before,
            "prob60_dec_source": _dp.get("source_label"),
            "prob60_dec_indep": _dp.get("independent_events"),
        }
    else:
        before = {}

    if not best:
        return {
            "available": bool(base),
            "trade": None,
            "before": before,
            "after": None,
            "improve": None,
            "executable": False,
            "no_exec_reason": "",
            "verdict": "今天不买",
            "verdict_detail": ("组合边际模拟显示：任何买入金额都不能让整个组合变得更好，"
                               "因此今天维持现状（WAIT）。"),
            "before_text": _compare_text(before),
        }

    after = {
        "prob20": best.get("prob20"), "prob60": best.get("prob60"),
        "prob60_oos": best.get("prob60_oos"), "prob120": best.get("prob120"),
        "exp60": best.get("exp60"), "max_dd_1y": best.get("max_dd_1y"),
        "vol_annual": best.get("vol_annual"), "quality": best.get("quality"),
        "cash_value": best.get("cash_after"),
        "classes": best.get("class_after"),
        # 决策口径交易后概率 = 决策基准（独立事件去重样本外）+ 模拟相对变化
        "prob60_dec": (round(float(_dec_before) + float(best.get("improve_prob60") or 0), 1)
                       if _dec_before is not None else None),
        "prob60_dec_estimated": True,
    }
    improve = {
        "prob60": best.get("improve_prob60"), "prob120": best.get("improve_prob120"),
        "prob60_dec": best.get("improve_prob60"),
        "exp60": best.get("improve_exp60"), "max_dd_1y": best.get("improve_dd1y"),
        "vol_annual": best.get("improve_vol"), "quality": best.get("improve_quality"),
        "marginal_score": best.get("marginal_score"),
    }
    worse = []
    if (best.get("improve_quality") or 0) < 0:
        worse.append("组合质量下降")
    if (best.get("improve_prob60") or 0) < -1.0:
        worse.append("60日盈利概率下降超过 1 个百分点")
    if (best.get("improve_dd1y") or 0) < -1.0:
        worse.append("1年最大回撤恶化超过 1 个百分点")
    verdict = "组合质量变差，禁止建议" if worse else "该交易改善整个组合"
    if worse:
        detail = ("模拟显示该笔交易会让组合变差（" + "、".join(worse)
                  + "），因此不建议执行。")
    else:
        bits = []
        if (best.get("improve_prob60") or 0) > 0.05:
            bits.append(f"相似状态历史频率 +{best['improve_prob60']:.1f} 个百分点（研究参考）")
        if (best.get("improve_exp60") or 0) > 0.005:
            bits.append(f"期望收益 +{best['improve_exp60']:.2f} 个百分点")
        if (best.get("improve_quality") or 0) > 0.05:
            bits.append(f"组合质量 +{best['improve_quality']:.1f} 分")
        if (best.get("improve_dd1y") or 0) > 0.005:
            bits.append(f"1年最大回撤收窄 {best['improve_dd1y']:.2f} 个百分点")
        detail = ("模拟显示：买入 " + str(best.get("name") or "")
                  + f" {float(best.get('amount') or 0):,.0f} 元后，"
                  + ("、".join(bits) if bits else "组合综合质量有所改善")
                  + "，因此这是一笔值得执行的交易。")
        if not executable:
            detail += f"　但今天不执行：{no_exec_reason}"
            verdict = "该交易能改善组合，但今天不执行"
    return {
        "available": True,
        "executable": bool(executable),
        "no_exec_reason": no_exec_reason,
        "trade": {
            "id": best.get("id"), "name": best.get("name"),
            "asset_class": best.get("asset_class"),
            "class_label": CLASS_LABEL.get(best.get("asset_class"), ""),
            "amount": best.get("amount"),
            "weight_after_pct": best.get("weight_after_pct"),
            "marginal_score": best.get("marginal_score"),
            "marginal_parts": best.get("marginal_parts"),
            "optimal_amount": ((marginal.get("per_asset") or {}).get(best.get("id")) or {}
                               ).get("optimal_amount"),
            "capped": bool(((marginal.get("per_asset") or {}).get(best.get("id")) or {}
                            ).get("capped")),
        },
        "before": before,
        "after": after,
        "improve": improve,
        "verdict": verdict,
        "verdict_detail": detail,
        "worse": worse,
        "before_text": _compare_text(before),
        "after_text": _compare_text(after),
        "note": ("对比基准是【同一批历史相似窗口】下的组合表现，"
                 "差异只来自组合构成变化，不含未来数据。"),
    }


def _compare_text(d: dict) -> dict:
    if not d:
        return {}
    return {
        "prob60": d.get("prob60"), "prob60_oos": d.get("prob60_oos"),
        "prob60_ci": d.get("prob60_ci"),
        "prob60_samples": d.get("prob60_samples"),
        "prob60_dec": d.get("prob60_dec"),
        "prob60_dec_source": d.get("prob60_dec_source"),
        "prob60_dec_estimated": d.get("prob60_dec_estimated"),
        "exp60": d.get("exp60"), "max_dd_1y": d.get("max_dd_1y"),
        "quality": d.get("quality"),
    }


# ==========================================================================
# 六、下一次买入触发条件（需求 10 / 14）
# ==========================================================================
def _etf_trigger(item: dict, snap: dict, cfg) -> str:
    cls = item["asset_class"]
    if cls == "gold":
        return ("黄金目标上限 5%；当前若超配，禁止增加（即使底部雷达很强），"
                "只能持有或再平衡")
    if item["blocked"]:
        fl = snap.get("flags") or {}
        if fl.get("no_chase"):
            return ("等待回调：1年相对价位回落到 <75% 或 BIAS20 < +4%；"
                    "且 WR 从超买区回落后重新转强，或价格重新站上 MA20")
        if fl.get("no_bottom_fishing"):
            return ("等待确认止跌：WR 超卖后出现反转确认 + 价格停止创新低 + 重新站上 MA20")
    if item["actionable"]:
        return "条件已满足，可执行第一批"
    if cls == "stock":
        return ("满足全部条件才买：组合独立事件去重样本外60日概率≥65% + 期望收益>2% + "
                "该 ETF 买入质量≥B 且不处于高位追涨；若属超跌，必须出现止跌确认")
    return ("满足组合买入门槛（S/A 级）后，按目标缺口分批补足；"
            "否则继续以债券/现金形式等待")


def next_buy_triggers(rows: list[dict], plan: dict, gate: dict, cur: dict,
                      targets: dict, risk: dict, consistency: dict, cfg) -> dict:
    total = float(cur.get("total_assets") or 0.0)
    stock_now = float(cur.get("by_class", {}).get("stock") or 0.0) * 100
    stock_tgt = float(targets.get("stock") or 0.0) * 100
    risk_score = risk.get("score")
    hi_pos = float(cfg.get("decision.probe_position_hi_pct", 80))

    stock_items = [i for i in plan["items"] if i["asset_class"] == "stock"]
    eq70 = any((i["entry_score"] or 0) >= 70 for i in stock_items)
    no_chase_any = any(i["blocked"] and "追涨" in (i["reason"] or "") for i in stock_items)

    conds = [
        {"name": "组合独立事件去重样本外60日概率 ≥65%（决策主口径）",
         "ok": (gate.get("prob_used") or 0) >= 65,
         "now": f"{gate.get('prob_used')}%（{gate.get('prob_source')}）"},
        {"name": "独立事件去重样本外事件 ≥30（概率可信度）",
         "ok": int(((gate.get("dec_prob") or {}).get("independent_events")) or 0) >= 30,
         "now": (f"{((gate.get('dec_prob') or {}).get('independent_events'))} 个"
                 + ("（<30，样本极少，仅供研究）"
                    if int(((gate.get("dec_prob") or {}).get("independent_events")) or 0) < 30
                    else ""))},
        {"name": "组合60日期望收益 >2%",
         "ok": (gate.get("exp60") if gate.get("exp60") is not None else -99) > 2,
         "now": f"{gate.get('exp60')}%"},
        {"name": "至少一只股票ETF 买入质量≥B 且 Entry Score ≥70",
         "ok": eq70,
         "now": "已满足" if eq70 else "暂无（最高 "
                 f"{max([(i['entry_score'] or 0) for i in stock_items] or [0]):.0f}）"},
        {"name": "该 ETF 不能处于高位追涨",
         "ok": not no_chase_any,
         "now": "存在高位标的" if no_chase_any else "无高位追涨标的"},
        {"name": "当前股票总仓位低于目标",
         "ok": stock_now < stock_tgt - 1e-9,
         "now": f"股票 {stock_now:.1f}% / 目标 {stock_tgt:.1f}%"},
        {"name": "组合风险评分没有明显恶化",
         "ok": (risk_score if risk_score is not None else 100) < 65,
         "now": f"风险评分 {risk_score}/100"},
        {"name": "若属超跌状态，必须出现止跌确认",
         "ok": not any(i["blocked"] and "抄底" in (i["reason"] or "") for i in stock_items),
         "now": "暂无未止跌的超跌标的"},
    ]

    focus = None
    cands = [i for i in plan["items"]
             if i["asset_class"] in ("stock", "bond") and float(i["gap_amount"] or 0) > 0]
    if cands:
        cands.sort(key=lambda i: (-(i["entry_score"] or 0),
                                  -float(i["gap_amount"] or 0)))
        focus = cands[0]
    focus_trigger = _etf_trigger(focus, {}, cfg) if focus else "—"

    return {
        "portfolio_conditions": conds,
        "all_met": all(c["ok"] for c in conds),
        "focus": {
            "id": focus["id"], "name": focus["name"],
            "trigger": focus_trigger,
            "next_batch_amount": focus["first_batch_amount"],
            "gap_amount": focus["gap_amount"],
        } if focus else None,
        "etf_triggers": [
            {"id": i["id"], "name": i["name"], "trigger": _etf_trigger(i, {}, cfg)}
            for i in plan["items"]
            if i["asset_class"] == "gold" or float(i["gap_amount"] or 0) > 0
        ][:6],
    }


# ==========================================================================
# 七、三个互相独立的问题（需求 4 / 35）
# ==========================================================================
def three_questions(gate: dict, rebal: dict, risk: dict, consistency: dict,
                    plan: dict, cur: dict, cfg, *, perm: dict | None = None) -> dict:
    ok = bool(consistency.get("ok"))
    gold_cap = float(cfg.get("portfolio.gold_max", 0.05))
    gold_now = float(cur.get("by_class", {}).get("gold") or 0.0)
    gold_over = gold_now > gold_cap + 1e-9

    new_money_would = bool(ok and not gold_over
                           and gate.get("grade") in ("S", "A", "B")
                           and plan.get("total_first_batch_amount", 0) > 0)
    # 【BUY PERMISSION GATE】买入权限关闭时，"是否投入新增资金"必须是 NO，
    # 否则会与"今天不执行实际买入"的最终状态自相矛盾。
    permission_blocked = bool(perm) and not bool(perm.get("allowed"))
    new_money = bool(new_money_would and not permission_blocked)
    permission_note = ""
    if permission_blocked and new_money_would:
        permission_note = ("买入权限关闭（可信等级未达开放门槛）：即使组合模拟显示"
                           "某笔交易可改善组合，今天也不投入新增资金，金额仅作研究参考。")
    reduce_over = bool(gold_over or any(
        (i.get("deviation_pct") or 0) >= float(cfg.get("portfolio.rebalance_priority_pct", 5.0))
        and "减仓" in (i.get("action") or "") for i in (rebal.get("items") or [])))
    need_rebal = bool(ok and (rebal.get("need") or reduce_over))

    return {
        "new_money": new_money,
        "new_money_text": "YES" if new_money else "NO",
        "new_money_note": permission_note,
        "rebalance": need_rebal,
        "rebalance_text": "YES" if need_rebal else "NO",
        "reduce_overweight": reduce_over,
        "reduce_text": "YES" if reduce_over else "NO",
        "reduce_targets": [i["name"] for i in (rebal.get("items") or [])
                           if i.get("deviation_pct", 0) > 0 and "减仓" in (i.get("action") or "")
                           ] or (["黄金ETF"] if gold_over else []),
        "buy_permission": perm,
        "permission_blocked": permission_blocked,
        "note": "三个问题是独立的：可以『不投入新资金 + 需要再平衡』，"
                "但不会出现『YES 买入 + 同时不投入资金』这种自相矛盾的状态。"
                + ("（注：买入权限关闭时，新增资金一律为 NO。）" if permission_blocked else ""),
    }


# ==========================================================================
# 八、今天到底怎么操作（需求 3 / 28 / 34）
# ==========================================================================
def today_action(gate: dict, three: dict, risk: dict, rebal: dict, plan: dict,
                 consistency: dict, cur: dict, cfg, *,
                 marginal: dict | None = None,
                 rebal_sim: dict | None = None,
                 trust_level: str | None = None) -> dict:
    gold_cap = float(cfg.get("portfolio.gold_max", 0.05))
    gold_now = float(cur.get("by_class", {}).get("gold") or 0.0)
    gold_over = gold_now > gold_cap + 1e-9
    grade = gate.get("grade")
    risk_score = risk.get("score") or 0.0
    best_trade = (marginal or {}).get("best_trade")
    has_trade = bool(best_trade and float(best_trade.get("amount") or 0) > 0)
    rb_best = (rebal_sim or {}).get("best") or {}

    if not consistency.get("ok"):
        key = "WHITE_WAIT"
        sub = "数据一致性异常：本次不给出任何金额建议，请先修正持仓/总资产"
    elif risk.get("state") == "Crisis" or risk_score >= 70:
        key = "RED_DEFEND"
        sub = "组合风险过高，优先防守：不新增风险资产"
    elif gold_over:
        key = "BLUE_REBALANCE"
        if rb_best.get("target_pct"):
            sub = (f"黄金当前 {float(cur.get('by_class', {}).get('gold') or 0) * 100:.1f}%，"
                   f"超过 5% 硬上限；模拟显示降到 {rb_best.get('target_pct')}% "
                   f"（卖出约 {int(rb_best.get('sell_amount') or 0):,} 元）后组合质量最好，"
                   "今天先再平衡，不投入新增资金")
        else:
            sub = "黄金超过 5% 硬上限，先再平衡（降回 5% 以内），不投入新增风险资金"
    elif grade in ("S", "A") and three["new_money"] and has_trade:
        key = "GREEN_BUY"
        sub = (f"组合满足 {grade} 级门槛，且模拟显示买入 {best_trade.get('name')} "
               f"{int(best_trade.get('amount') or 0):,} 元后整个组合质量改善 "
               f"{best_trade.get('improve_quality'):+.1f} 分，可按 {plan['batch_count']} 批分批买入")
    elif grade == "B" and three["new_money"] and has_trade:
        key = "GREEN_PROBE"
        sub = (f"组合仅达 B 级，建议小仓试探：{best_trade.get('name')} "
               f"{int(best_trade.get('amount') or 0):,} 元")
    elif grade in ("S", "A", "B") and not has_trade:
        key = "YELLOW_HOLD"
        sub = ("组合买入门槛已达标，但【组合边际模拟】显示今天任何买入都不能让整个组合"
               "变得更好 → 今天不投入新资金，继续持有观察")
    elif grade in ("D", "E"):
        key = "RED_DEFEND"
        sub = "组合历史盈利概率与期望收益不达标，不加仓、不抄底"
    elif three["rebalance"]:
        key = "BLUE_REBALANCE"
        sub = "暂不投入新资金，但存在需要再平衡的偏离"
    elif grade == "C":
        key = "YELLOW_HOLD" if not three["new_money"] and float(cur.get("etf_value") or 0) > 0 else "WHITE_WAIT"
        sub = "暂不投入新增风险资金，等待更好的股票 ETF 买入窗口"
    else:
        key = "WHITE_WAIT"
        sub = "暂不投入新增风险资金"

    # ---- 【BUY PERMISSION GATE】最高优先级：可信等级不足 → 不开放实际买入 ----
    #   注意：PortfolioSimulator 的理论最优金额仍保留为"研究参考"，
    #   但最终状态绝不能是 GREEN_BUY / GREEN_PROBE（BUY / SPLIT_BUY / TRIAL_BUY）。
    perm = buy_permission(cfg, trust_level)
    research_ref: dict | None = None
    permission_blocked = False
    if not perm["allowed"] and key in BUY_ACTIONS:
        permission_blocked = True
        if best_trade and float(best_trade.get("amount") or 0) > 0:
            research_ref = {
                "name": best_trade.get("name"),
                "amount": float(best_trade.get("amount") or 0),
                "improve_quality": best_trade.get("improve_quality"),
                "improve_prob60": best_trade.get("improve_prob60"),
            }
        # 降级到允许的最终状态（持有观察 / 暂停加仓 / 防守·不要买 / 再平衡 / WAIT）
        if grade in ("D", "E") or risk.get("state") == "Crisis" or risk_score >= 70:
            key = "RED_DEFEND"
        elif gold_over:
            key = "BLUE_REBALANCE"
        elif three.get("rebalance"):
            key = "BLUE_REBALANCE"
        elif float(cur.get("etf_value") or 0) > 0:
            key = "ORANGE_PAUSE"
        else:
            key = "WHITE_WAIT"
        _ref_txt = (f"研究参考：模型模拟买入 {research_ref['name']} "
                    f"{int(research_ref['amount']):,} 元后组合质量最高（仅供研究）；"
                    if research_ref else "")
        sub = (f"【买入权限关闭】系统可信等级={perm['trust_level']}，"
               f"未达开放门槛（{perm['min_level']}）。{_ref_txt}"
               f"{perm['trust_level']}：当前不开放买入权限，因此今天不执行。")

    st = state_of(key)
    st["sub"] = sub
    st["grade"] = grade
    st["has_marginal_trade"] = has_trade
    st["buy_permission"] = perm
    st["permission_blocked"] = permission_blocked
    st["research_reference"] = research_ref
    return st


# ==========================================================================
# 九、为什么今天不能买（需求 13）
# ==========================================================================
def why_not_buy(gate: dict, plan: dict, rebal: dict, risk: dict, cur: dict,
                consistency: dict, cfg, marginal: dict | None = None) -> list[str]:
    reasons: list[str] = []
    total = float(cur.get("total_assets") or 0.0)
    for r in plan["items"]:
        if r["asset_class"] != "stock":
            continue
        snap_reason = r.get("reason") or ""
        if r["blocked"] and "追涨" in snap_reason:
            reasons.append(f"{r['name']}：1年相对价位 {r['position_label']}，"
                           "且 BIAS/WR 处于超买区，不适合追涨")
        elif r["blocked"] and "抄底" in snap_reason:
            reasons.append(f"{r['name']}：极度超跌但尚未止跌（仍创新低 / MA20 向下），"
                           "不宜抄底")
    gold_cap = float(cfg.get("portfolio.gold_max", 0.05))
    gold_now = float(cur.get("by_class", {}).get("gold") or 0.0) * 100
    if gold_now > gold_cap * 100 + 1e-6:
        reasons.append(f"黄金已经超过 5% 上限（当前 {gold_now:.1f}%），不能再加")
    cash_pct = float(cur.get("cash_pct") or 0.0)
    if cash_pct > 0:
        reasons.append(f"当前组合还有 {cash_pct:.1f}% 待投资资金，不必急于一次买入")
    # ---- 样本门禁：单只 ETF 样本不足 → 结构性不出买入信号（刻意保守，非故障）----
    _need = int(cfg.get("hist.min_samples", 50))
    _short = [str(r["name"])[:10] for r in plan["items"]
              if r["asset_class"] == "stock"
              and (r.get("buy_quality") or "") == "样本不足"]
    if _short:
        _names = "、".join(_short)
        reasons.append(
            f"单只 ETF 相似样本均低于 {_need} 个（{_names}）→ 买入质量只能判"
            "「样本不足」，按当前口径不会给出股票买入建议"
            "（刻意保守：不用远距离样本凑数，不是程序故障）")
    if (gate.get("prob_used") or 0) < 65:
        reasons.append(f"组合样本外60日历史盈利概率 {gate.get('prob_used')}%，"
                       "尚未达到 65% 的 A 级买入门槛")
    if (gate.get("exp60") if gate.get("exp60") is not None else -99) <= 2:
        reasons.append(f"组合60日期望收益 {gate.get('exp60')}%，未超过 2%")
    if (gate.get("payoff") or 0) < 1.5:
        reasons.append(f"盈亏比 {gate.get('payoff')}，小于 1.5")
    if risk.get("state") in ("Risk Off", "Crisis"):
        reasons.append(f"组合处于 {risk.get('state')} 状态，风险偏好下降")
    if not consistency.get("ok"):
        reasons.append("持仓/总资产数据不一致，必须修正后再生成建议")

    # ---- 组合边际模拟给出的"不买"理由（§34：高胜率但不值得买） ----
    marg = marginal or {}
    best = marg.get("best_trade")
    base = marg.get("baseline") or {}
    if not best and base:
        reasons.append("组合边际模拟：今天任何买入金额都不能让整个组合变得更好，"
                       "所以不投入新资金（WAIT）")
    elif best:
        # 组合 60 日胜率很高但期望收益改善很小时，明确提示"高胜率 ≠ 值得买"
        if (base.get("prob60") or 0) >= 80 and abs(float(best.get("improve_exp60") or 0)) < 0.5:
            reasons.append(f"相似状态历史频率虽高达 {base.get('prob60')}%（研究参考），"
                           f"但买入后期望收益仅 {best.get('improve_exp60'):+.2f} 个百分点，"
                           "改善有限，不宜大额买入")
        if (best.get("improve_dd1y") or 0) < -0.5:
            reasons.append(f"买入后1年最大回撤会恶化 "
                           f"{abs(best.get('improve_dd1y')):.2f} 个百分点，需控制金额")
    return reasons[:5]


def best_amount_text(marginal: dict | None) -> str:
    """把"今天买多少"翻译成一句话（首页用）。"""
    best = (marginal or {}).get("best_trade")
    if not best:
        return "今天不投入新资金（WAIT）"
    return (f"买入 {best.get('name')} {int(best.get('amount') or 0):,} 元"
            f"（组合质量 {best.get('improve_quality'):+.1f} 分，"
            f"相似状态历史频率 {best.get('improve_prob60'):+.1f} 个百分点）")


# ==========================================================================
# 十、原因解释器（需求 27 / 31）
# ==========================================================================
def explainer(gate: dict, three: dict, plan: dict, rebal: dict, cur: dict,
              risk: dict, decision_key: str, cfg, marginal: dict | None = None) -> list[str]:
    out: list[str] = []
    total = float(cur.get("total_assets") or 0.0)
    cash = float(cur.get("cash_value") or 0.0)
    prob = gate.get("prob_used")
    best = (marginal or {}).get("best_trade")
    if decision_key == "GREEN_BUY":
        out.append(f"组合{six_day(gate)}历史盈利概率 {prob}%，"
                   f"期望收益 {gate.get('exp60')}%，达到 {gate.get('grade')} 级买入门槛。")
        if best:
            out.append(f"把 {int(best.get('amount') or 0):,} 元从现金换入 {best.get('name')} 后，"
                       f"整个组合相似状态历史频率（研究参考）"
                       f"{best.get('improve_prob60'):+.1f} 个百分点、"
                       f"期望收益 {best.get('improve_exp60'):+.2f} 个百分点、"
                       f"组合质量 {best.get('improve_quality'):+.1f} 分 —— 这是模拟出来最优的一笔。")
        out.append(f"剩余待投资资金 {cash:,.0f} 元继续等待，不会一次买满。")
    elif decision_key == "GREEN_PROBE":
        out.append(f"组合概率仅达 B 级（{prob}%），因此只做小仓试探"
                   + (f"：{best.get('name')} {int(best.get('amount') or 0):,} 元。" if best else "。"))
    elif decision_key == "BLUE_REBALANCE":
        gold_now = float(cur.get("by_class", {}).get("gold") or 0.0) * 100
        out.append(f"黄金当前 {gold_now:.1f}%，已超过 5% 硬上限，因此今天优先再平衡，"
                   "不投入新增风险资金。")
        out.append("再平衡方案也经过模拟：按『组合质量是否改善』择优，不会仅仅因为涨多了就机械卖出。")
    elif decision_key == "RED_DEFEND":
        out.append(f"组合风险评分 {risk.get('score')}/100（{risk.get('state')}），"
                   "或历史盈利概率与期望收益不达标，因此以防守为主。")
    elif decision_key == "YELLOW_HOLD":
        if best:
            out.append("现有仓位继续持有，暂不投入新增风险资金。")
        else:
            out.append("组合买入门槛已达标，但组合边际模拟显示任何买入都不能让整个组合"
                       "变得更好（期望收益改善有限、回撤或波动会变差），因此今天 WAIT。")
            out.append("这正是『高胜率 ≠ 值得买』：60日胜率高只说明大概率不亏，"
                       "不代表现在加仓划算。")
    elif decision_key == "ORANGE_PAUSE":
        out.append("系统可信等级未达到【买入权限开放门槛】，因此今天暂停加仓。")
        out.append("组合边际模拟给出的理论最优金额只作研究参考，"
                   "不转换成实际买入建议（可信等级决定买入权限，不是买入等级）。")
    else:
        out.append(f"组合{six_day(gate)}历史盈利概率 {prob}%，尚未达到买入门槛，"
                   "因此今天 WAIT 等待。")
        out.append("WAIT 是完全合法的结论：目标仓位不等于必须买满。")
    if not gate.get("use_oos"):
        out.append("样本外统计尚不可用，买入信心已下调。")
    if total > 0 and cash > 0:
        out.append(f"当前总资产 {total:,.0f} 元，其中待投资资金 {cash:,.0f} 元"
                   f"（{cur.get('cash_pct')}%），暂不全部投入，等待下一个确认条件。")
    return out


def six_day(gate: dict) -> str:
    """决策概率的时间口径前缀（最终修正：全系统只认独立事件去重样本外）。"""
    src = str(gate.get("prob_source") or "")
    if "独立事件" in src:
        return "独立事件去重样本外60日"
    return "样本外60日" if gate.get("use_oos") else "60日"


# ==========================================================================
# 十·五、第一屏结论（headline）：5 秒只看这一块
# ==========================================================================
def headline(today: dict, plan: dict, triggers: dict, ladder: dict,
             marginal: dict, comparison: dict, rebal_sim: dict,
             cur: dict, targets: dict, gold_cap_pct: float = 5.0,
             consistency: dict | None = None) -> dict:
    """把整份决策压成【今天到底干什么】一句话 + 三个关键数字。

    设计原则：参数再多，第一眼只能看到一个答案。
      answer  : 3-6 字极简答案（买 / 不买 / 再平衡 / 先修数据）
      big     : 一句话大字结论（普通中文，无术语）
      amount  : 今天要投入的金额（不买 = 0）
      name    : 今天要买的 ETF（不买 = None）
      prob60  : 当前【真实组合】未来 60 日历史盈利概率
      why     : ≤3 条一句话理由
      next    : 下一次买入 / 再平衡的条件
    """
    key = (today or {}).get("key") or "WHITE_WAIT"
    base = (marginal or {}).get("baseline") or {}
    trade = (marginal or {}).get("best_trade")
    rb = (rebal_sim or {}).get("best") or {}
    cls = {k: float(v) * 100 for k, v in (cur.get("by_class") or {}).items()}
    gold_cap = float(gold_cap_pct or 5.0)
    gold_now = cls.get("gold") or 0.0
    gold_over = gold_now > gold_cap + 1e-9
    prob60 = base.get("prob60")
    prob60_oos = base.get("prob60_oos")
    prob30 = base.get("prob30")
    prob90 = base.get("prob90")
    prob30_oos = base.get("prob30_oos")
    prob90_oos = base.get("prob90_oos")
    amount = float((trade or {}).get("amount") or 0.0)
    name = (trade or {}).get("name")
    grade = (today or {}).get("grade")
    consistency_bad = not (consistency or {}).get("ok", True)

    # ---- 一句话大字结论 ----
    def _amt_txt(a: float, tr: dict | None) -> str:
        """金额表述：有真实价时显示『X 份（约 Y 元）』，否则只显示金额。"""
        sh = (tr or {}).get("shares")
        act = (tr or {}).get("actual_amount")
        if sh and act:
            return f"{int(sh):,} 份（约 {act:,.0f} 元）"
        return f"{a:,.0f} 元"

    if key == "GREEN_BUY" and name and amount > 0:
        answer = "分批买入"
        big = f"今天买：{name} {_amt_txt(amount, trade)}"
        color = "up"
    elif key == "GREEN_PROBE" and name and amount > 0:
        answer = "小仓买入"
        big = f"今天小仓试探：{name} {_amt_txt(amount, trade)}"
        color = "up"
    elif key == "BLUE_REBALANCE":
        answer = "再平衡"
        if rb.get("target_pct"):
            sh = rb.get("sell_shares")
            amt_txt = (f"{int(sh):,} 份（约 {float(rb.get('sell_amount') or 0):,.0f} 元）"
                       if sh else
                       f"约 {float(rb.get('sell_amount') or 0):,.0f} 元"
                       "（交易价缺失，无法换算份数）")
            big = (f"今天只做一件事：卖出黄金 {amt_txt}"
                   f"（降到 {rb.get('target_pct')}%）")
        else:
            big = "今天只做一件事：再平衡，不加风险资产"
        color = "accent"
    elif key == "RED_DEFEND":
        answer = "不买"
        big = "今天不买，以防守为主"
        color = "danger"
    elif key == "YELLOW_HOLD":
        answer = "不买"
        big = "今天不买，继续持有观察"
        color = "warn"
    elif key == "ORANGE_PAUSE":
        answer = "暂停加仓"
        big = "今天不买（暂停加仓）：当前可信等级不开放买入权限"
        color = "warn"
    elif consistency_bad:
        answer = "先修数据"
        big = "持仓数据对不上，今天先不给建议"
        color = "danger"
    else:
        answer = "不买"
        big = "今天不买，继续等"
        color = "muted"

    # ---- ≤3 条一句话理由（按重要性排序，优先给"结论本身"的依据）----
    prob60_base = base.get("prob60_base")
    prob60_uplift = base.get("prob60_uplift")
    dp = (base.get("dec_prob") or {})           # 唯一决策概率（最终修正）
    why: list[str] = []
    if dp.get("prob_used") is not None:
        why.append(
            f"决策主口径【独立事件去重样本外60日】{float(dp['prob_used']):.1f}%"
            f"（独立事件 {dp.get('independent_events')} 个，95%CI "
            f"{_num((dp.get('ci95') or [None])[0], 1)}%~{_num((dp.get('ci95') or [None, None])[1], 1)}%）；"
            f"同窗口基础胜率 {_num(dp.get('base_rate'), 1)}%，"
            f"相对基础率 {float(dp.get('lift_pp') or 0):+.1f}pp")
        if prob60 is not None:
            why.append(f"研究参考：相似状态历史频率 {float(prob60):.1f}%、"
                       f"重叠滚动OOS频率 {_num(prob60_oos, 1)}%（均不参与决策）")
    elif prob60 is not None:
        if prob60_oos is not None and prob60_base is not None \
                and prob60_uplift is not None:
            # 口径纪律：重叠窗口频率不叫"样本外"；主口径是独立事件去重样本外
            why.append(f"研究参考：相似状态历史频率 {float(prob60):.1f}%、"
                       f"重叠滚动OOS频率 {float(prob60_oos):.1f}%；"
                       f"不用任何信号的基础胜率 {float(prob60_base):.1f}%，"
                       f"条件相对基准 {float(prob60_uplift):+.1f}pp")
        else:
            extra = f"，重叠滚动OOS频率 {float(prob60_oos):.1f}%" \
                if prob60_oos is not None else ""
            why.append(f"相似状态历史频率 {float(prob60):.1f}%{extra}")
    if gold_over:
        why.append(f"黄金已占 {gold_now:.1f}%，超过 5% 硬上限，必须先降下来")
    if key in ("GREEN_BUY", "GREEN_PROBE") and trade:
        iq = trade.get("improve_quality")
        ip = trade.get("improve_prob60")
        bits = []
        if iq is not None:
            bits.append(f"组合质量 {float(iq):+.1f} 分")
        if ip is not None:
            bits.append(f"相似状态历史频率（研究参考）{float(ip):+.1f} 个百分点")
        why.append("买入后 " + "、".join(bits) if bits else "买入后整个组合变好")
    elif key in ("YELLOW_HOLD", "WHITE_WAIT"):
        why.append("边际模拟显示：今天任何买入金额都不能让整个组合变得更好")
    if grade:
        why.append(f"组合买入门槛评级 {grade} 级（S 最好、E 最差）")
    why = [w for w in why if w][:3]

    # ---- 下一次条件 ----
    nxt = ""
    for b in (ladder or {}).get("batches") or []:
        if not b.get("done") and b.get("condition"):
            nxt = str(b["condition"])
            break
    if not nxt:
        nxt = ((triggers or {}).get("focus") or {}).get("trigger") or ""
    if not nxt:
        ok_conds = [c.get("name") for c in (triggers or {}).get("portfolio_conditions") or []
                    if not c.get("ok")]
        nxt = ("还需要满足：" + "、".join(ok_conds)) if ok_conds else "组合条件已满足，按分批纪律执行"

    # ---- 今天到底掏不掏钱：只有 GREEN_* 才是真买 ----
    trade_today = key in ("GREEN_BUY", "GREEN_PROBE") and bool(name) and amount > 0
    _perm = (today or {}).get("buy_permission") or {}
    _ref = (today or {}).get("research_reference")
    _blocked = bool((today or {}).get("permission_blocked"))
    note = ""
    if not trade_today and _blocked:
        # 最高优先级：可信等级不足 → 只能给"研究参考"，最终不执行
        if _ref:
            note = (f"研究参考：模型模拟买入 {_ref.get('name')} "
                    f"{int(_ref.get('amount') or 0):,} 元后组合质量最高；"
                    f"但{_perm.get('trust_level') or '研究级'}："
                    "当前不开放买入权限，因此今天不执行")
        else:
            note = (f"{_perm.get('trust_level') or '研究级'}："
                    "当前不开放买入权限，因此今天不执行")
    elif not trade_today and name and amount > 0:
        if key == "BLUE_REBALANCE":
            note = (f"参考：模型算出的最优是 {name} {amount:,.0f} 元，"
                    "但今天先做再平衡，不投入新增资金")
        elif key == "RED_DEFEND":
            note = f"参考：模型算出的最优是 {name} {amount:,.0f} 元，但当前风险偏高，今天不执行"
        else:
            note = (f"参考：模型算出的最优是 {name} {amount:,.0f} 元，"
                    "但改善幅度不足以抵消风险，今天不执行")
    elif not trade_today and gold_over:
        note = "黄金已超上限，今天只做再平衡，不投入任何新增资金"

    return {
        "key": key,
        "answer": answer,
        "big": big,
        "color": color,
        # 【今天实际要不要掏钱】——只有 GREEN_BUY / GREEN_PROBE 才是真买
        "amount": amount if trade_today else 0.0,
        "name": name if trade_today else None,
        "class_label": (trade or {}).get("class_label") if trade_today else None,
        "trade_today": trade_today,
        # 今天不买时，给出"理论最优"作参考（避免用户以为漏了机会）
        "ref_amount": (None if trade_today else amount) or None,
        "ref_name": (None if trade_today else name),
        "shares": (trade or {}).get("shares") if trade_today else None,
        "actual_amount": (trade or {}).get("actual_amount") if trade_today else None,
        "note": note,
        # ---- 【BUY PERMISSION GATE】可信等级决定买入权限（最终修正）----
        "buy_permission": _perm or None,
        "permission_blocked": _blocked,
        "research_reference": _ref,
        # ---- 唯一决策概率（最终修正）：首页/决策/金额/等级共用这一个口径 ----
        "dec_prob": dp or None,
        "prob_used": dp.get("prob_used"),
        "prob_source": dp.get("source"),
        "prob_source_label": dp.get("source_label"),
        "prob60": prob60,
        "prob60_oos": prob60_oos,
        "prob30": prob30,
        "prob30_oos": prob30_oos,
        "prob90": prob90,
        "prob90_oos": prob90_oos,
        "prob60_base": base.get("prob60_base"),
        "prob60_uplift": base.get("prob60_uplift"),
        "prob60_samples": base.get("prob60_samples"),
        "prob60_oos_samples": base.get("prob60_oos_samples"),
        "prob60_credibility": base.get("prob60_credibility"),
        "prob60_ci": base.get("prob60_ci"),
        "samples": base.get("prob60_samples"),
        "exp60": base.get("exp60"),
        "max_dd_1y": base.get("max_dd_1y"),
        "quality": base.get("quality"),
        "grade": grade,
        "why": why,
        "next": nxt,
        "gold_now": round(gold_now, 2),
        "gold_cap": round(gold_cap, 2),
        "gold_over": gold_over,
        "counterfactual": {
            "if_no_trade_prob60": (comparison or {}).get("before_text", {}).get("prob60"),
            "if_trade_prob60": (comparison or {}).get("after_text", {}).get("prob60"),
            "verdict": (comparison or {}).get("verdict"),
            "executable": bool((comparison or {}).get("executable")),
        },
    }


# ==========================================================================
# 十一、汇总：一份"用户可读"的决策包
# ==========================================================================
def build_decision(snap_like: dict) -> dict:
    """把所有子结论汇总成一份"用户可读"的决策包（含首页 5 秒块）。"""
    gate = snap_like["gate"]
    marginal = snap_like.get("marginal") or {}
    comparison = snap_like.get("comparison") or {}
    rebal_sim = snap_like.get("rebal_sim") or {}
    target_sim = snap_like.get("target_sim")
    cur = snap_like.get("weights") or {}
    targets = snap_like.get("target_class") or {}

    classes_now = {k: round(float(v) * 100, 2)
                   for k, v in (cur.get("by_class") or {}).items()}
    classes_tgt = {k: round(float(v) * 100, 2) for k, v in (targets or {}).items()}
    classes_tgt.setdefault("cash", 0.0)
    classes_dev = {}
    for k, t in classes_tgt.items():
        dev = round(classes_now.get(k, 0.0) - float(t), 2)
        if abs(dev) < 1e-9:
            dev = 0.0
        classes_dev[k] = dev
    over = {k: v for k, v in classes_dev.items() if v > 0}
    under = {k: v for k, v in classes_dev.items() if v < 0}

    base = marginal.get("baseline") or {}
    trade = comparison.get("trade")

    hl = headline(snap_like["today"], snap_like["plan"], snap_like.get("triggers") or {},
                  snap_like.get("ladder") or {}, marginal, comparison, rebal_sim,
                  cur, targets,
                  float(snap_like.get("gold_cap", 5.0) or 5.0),
                  snap_like.get("consistency"))

    # ---- 唯一决策概率（最终修正）：独立事件去重后的逐日样本外（h=60） ----
    # 首页/交易前后/边际模拟/触发条件全部引用它；重叠滚动 OOS 只作辅助统计。
    d60 = snap_like.get("daily_oos60") or {}
    dec_prob = snap_like.get("dec_prob")
    if dec_prob is None:
        dec_prob = resolve_decision_probability(snap_like.get("p_hist") or {}, d60)
    cons: dict | None = None
    if dec_prob.get("oos_independent_prob") is not None:
        cons = {
            "prob": dec_prob.get("prob_used"),
            "indep": dec_prob.get("independent_events"),
            "points": dec_prob.get("overlap_points"),
            "overlap_freq": dec_prob.get("oos_overlap_prob"),
            "ci": dec_prob.get("ci95"),
            "base": dec_prob.get("base_rate"),
            "uplift": dec_prob.get("lift_pp"),
            "brier": d60.get("brier"),
            "naive_brier": d60.get("naive_brier"),
            "bss": d60.get("brier_skill_score"),
            "calibration": d60.get("calibration_verdict"),
            "few_samples": not dec_prob.get("enough_samples"),
            "no_edge": dec_prob.get("no_timing_edge"),
        }
    hl["conservative"] = cons
    hl["dec_prob"] = dec_prob

    home = {
        "total_assets": cur.get("total_assets"),
        "etf_value": cur.get("etf_value"),
        "cash_value": cur.get("cash_value"),
        "action": snap_like["today"],
        "capital_fit": snap_like.get("capital_fit") or [],
        "raw_prices": snap_like.get("raw_prices") or {},
        "dec_prob": dec_prob,
        "portfolio_prob_used": dec_prob.get("prob_used"),
        "portfolio_prob_source": dec_prob.get("source_label"),
        "portfolio_prob60": base.get("prob60"),
        "portfolio_prob60_oos": base.get("prob60_oos"),
        "portfolio_prob60_ci": base.get("prob60_ci"),
        "portfolio_prob60_samples": base.get("prob60_samples"),
        "portfolio_exp60": base.get("exp60"),
        "portfolio_max_dd_1y": base.get("max_dd_1y"),
        "portfolio_vol": base.get("vol_annual"),
        "portfolio_quality": base.get("quality"),
        "portfolio_prob20": base.get("prob20"),
        "portfolio_prob120": base.get("prob120"),
        "if_no_trade_prob60": comparison.get("before_text", {}).get("prob60"),
        "if_no_trade_quality": comparison.get("before_text", {}).get("quality"),
        "trade": trade,
        "after_text": comparison.get("after_text"),
        "improve": comparison.get("improve"),
        "verdict": comparison.get("verdict"),
        "why": (snap_like.get("explain") or [])[:5],
        "why_not": (snap_like.get("why_not") or [])[:5],
        "next_batch": (snap_like.get("ladder") or {}).get("batches") or [],
        "ladder": snap_like.get("ladder") or {},
        "classes_now": classes_now,
        "classes_target": classes_tgt,
        "classes_dev": classes_dev,
        "overweight": over,
        "underweight": under,
        "gold": {
            "now_pct": classes_now.get("gold"),
            "cap_pct": float(snap_like.get("gold_cap", 5.0) or 5.0),
            "state": ("超配" if (classes_now.get("gold") or 0) > float(
                snap_like.get("gold_cap", 5.0) or 5.0) + 1e-6
                else "正常"),
            "rebalance": rebal_sim.get("best"),
        },
        "rebalance_sim": rebal_sim,
        # 【最终战略目标组合】参照（仅供展示；绝不是"当前组合"）
        "strategic_target": ({
            "prob60": target_sim.get("prob60"),
            "prob60_oos": target_sim.get("prob60_oos"),
            "exp60": target_sim.get("exp60"),
            "max_dd_1y": target_sim.get("max_dd_1y"),
            "vol_annual": target_sim.get("vol_annual"),
            "quality": target_sim.get("quality"),
            "cash_value": target_sim.get("cash_value"),
            "classes": target_sim.get("classes"),
            "note": ("这是『完全按战略目标（债券50%/股票45%/黄金5%/现金0）配置』时的"
                     "参照结果，不是当前组合，也不用于回答今天买不买；"
                     "当前概率一律以【真实持仓 + 现金】口径为准。"),
        } if target_sim else None),
        "best_amount_text": best_amount_text(marginal),
        "one_line": _one_line(snap_like, marginal, comparison, rebal_sim, classes_now),
        "headline": hl,
        "conservative": cons,
    }

    return {
        "today": snap_like["today"],
        "three": snap_like["three"],
        "gate": gate,
        "entry_score": snap_like["entry_score"],
        "plan": snap_like["plan"],
        "batches": snap_like["batches"],
        "triggers": snap_like["triggers"],
        "why_not": snap_like["why_not"],
        "explain": snap_like["explain"],
        "consistency": snap_like["consistency"],
        "comparison": comparison,
        "ladder": snap_like.get("ladder") or {},
        "rebal_sim": rebal_sim,
        "target_sim": target_sim,
        "headline": hl,
        "home": home,
        "prob": {
            # ---- 唯一决策概率（最终修正）：决策口径 = 独立事件去重样本外 ----
            "prob_used": dec_prob.get("prob_used"),
            "source": dec_prob.get("source"),
            "source_label": dec_prob.get("source_label"),
            "independent_events": dec_prob.get("independent_events"),
            "oos_independent": dec_prob.get("oos_independent_prob"),
            "oos_overlap": dec_prob.get("oos_overlap_prob"),
            "conditional": dec_prob.get("conditional_prob"),
            "base_rate": dec_prob.get("base_rate"),
            "lift_pp": dec_prob.get("lift_pp"),
            "dec_ci95": dec_prob.get("ci95"),
            "p20": _num((snap_like["p_hist"].get(20) or {}).get("win_rate_pct"), 1),
            "p30": _num((snap_like["p_hist"].get(30) or {}).get("win_rate_pct"), 1),
            "p60": _num((snap_like["p_hist"].get(60) or {}).get("win_rate_pct"), 1),
            "p90": _num((snap_like["p_hist"].get(90) or {}).get("win_rate_pct"), 1),
            "p120": _num((snap_like["p_hist"].get(120) or {}).get("win_rate_pct"), 1),
            "oos30": (snap_like["p_hist"].get(30) or {}).get("oos_win_rate_pct"),
            "oos60": dec_prob.get("oos_overlap_prob"),
            "oos90": (snap_like["p_hist"].get(90) or {}).get("oos_win_rate_pct"),
            "base60": dec_prob.get("base_rate"),
            "uplift60": dec_prob.get("lift_pp"),
            "samples60": gate.get("samples"),
            "oos_samples60": gate.get("oos_samples"),
            "ci95_low": (dec_prob.get("ci95") or [None, None])[0],
            "ci95_high": (dec_prob.get("ci95") or [None, None])[1],
            "grade": gate.get("credibility"),
            "note": ("这是历史相似市场状态下的统计概率，不代表未来实际收益；"
                     "决策主口径为独立事件去重样本外概率。"),
        },
        "dec_prob": dec_prob,
        "marginal": marginal,
        # ---- 【BUY PERMISSION GATE】可信等级决定买入权限（不是买入等级）----
        "trust_level": snap_like.get("trust_level")
        or (snap_like["today"] or {}).get("buy_permission", {}).get("trust_level"),
        "buy_permission": (snap_like["today"] or {}).get("buy_permission"),
        "research_reference": (snap_like["today"] or {}).get("research_reference"),
    }


def _one_line(snap_like: dict, marginal: dict, comparison: dict,
              rebal_sim: dict, classes_now: dict) -> str:
    """【今日最优操作】一句话（首页最后一行）。"""
    st = snap_like["today"]
    key = st.get("key")
    best = marginal.get("best_trade")
    rb = (rebal_sim or {}).get("best") or {}
    gold = classes_now.get("gold") or 0.0
    no_chase = [i["name"] for i in (snap_like["plan"].get("items") or [])
                if i.get("blocked") and "追涨" in (i.get("reason") or "")]
    parts: list[str] = []
    if key == "BLUE_REBALANCE":
        if rb.get("target_pct"):
            parts.append(f"黄金已超过 5%（当前 {gold:.1f}%），今天先把它降低到 "
                         f"{rb['target_pct']}%（卖出约 {int(rb.get('sell_amount') or 0):,} 元）")
        else:
            parts.append(f"黄金已超过 5%（当前 {gold:.1f}%），今天先再平衡，不加风险资产")
        if best:
            parts.append(f"再平衡后若条件仍满足，可考虑 {best.get('name')} "
                         f"{int(best.get('amount') or 0):,} 元")
    elif key in ("GREEN_BUY", "GREEN_PROBE") and best:
        parts.append(f"今天可以买 {best.get('name')} {int(best.get('amount') or 0):,} 元"
                     f"（组合质量 {best.get('improve_quality'):+.1f} 分）")
        parts.append("剩下资金等下一批条件")
    elif key == "YELLOW_HOLD":
        parts.append("今天不投入新资金，继续持有观察")
    elif key == "RED_DEFEND":
        parts.append("今天不加仓、不抄底，以防守为主")
    else:
        parts.append("今天 WAIT：不投入新增资金")
    if no_chase:
        parts.append("不追" + "、".join(no_chase))
    return "；".join(parts) + "。"
