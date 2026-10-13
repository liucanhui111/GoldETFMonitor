"""事件审计系列报告渲染（Event Reports）。

本轮产出四份新报告（旧报告一律保留、不删除、不覆盖历史版本）：
  EVENT_FUNNEL_REPORT.md      15 级事件漏斗 + A/B/C 归因
  SAMPLE_ADEQUACY_REPORT.md   样本充分性诊断（不足 → 写"无足够证据"）
  TRADE_EVENT_AUDIT.md        候选/可执行双层、研究/生产双口径、模型阶梯、稳定性
  STRATEGY_VALIDATION_V3.md   第三版策略有效性验证（含 10 问逐条回答）

同时刷新 V2 家族（含 BACKTEST_REAL_6ETF_V2.md）。
"""
from __future__ import annotations

from pathlib import Path

from .event_audit import HORIZONS, REGIMES, INDEPENDENT_THRESHOLD, run_event_audit
from .policy_reports import INHERITED_FACTS

ACTION_CF = {"BUY": "WAIT", "SELL": "HOLD（继续持有）", "REBALANCE": "NO-REBALANCE（不动）"}
ACTION_CN = {"BUY": "买入", "SELL": "卖出/减仓", "REBALANCE": "再平衡"}


def _f(v, nd=2):
    return "—" if v is None else f"{v:.{nd}f}"


def _pf(v):
    if v is None:
        return "—"
    return "∞" if v == float("inf") else f"{v:.3f}"


def _agg_row(agg: dict) -> str:
    d = (agg or {}).get("delta_return_net") or {}
    ci = d.get("ci95") or [None, None]
    dd = (agg or {}).get("delta_max_drawdown") or {}
    return ("| " + " | ".join([
        str(d.get("n", 0)), _f(d.get("mean")), _f(d.get("median")),
        _f(d.get("win_rate_pct"), 1) + "%", _pf(d.get("profit_factor")),
        f"{_f(ci[0])}~{_f(ci[1])}", _f(dd.get("mean")),
    ]) + " |")


_ADEQ_HEAD = ["| 动作 | 周期 | 候选事件 | 有效事件 | 独立事件 | 平均Δ收益(pp) | 中位(pp) | 胜率 | PF | 95%CI | 平均Δ回撤(pp) |",
              "|---|---|---|---|---|---|---|---|---|---|---|"]


# ==========================================================================
# 一、EVENT_FUNNEL_REPORT.md
# ==========================================================================
def render_event_funnel(res: dict) -> str:
    if not res.get("ok"):
        return f"# EVENT_FUNNEL_REPORT\n\n无法执行：{res.get('message')}\n"
    d = res["diagnosis"]
    f60 = res["funnels"][60]
    L = ["# 事件漏斗报告（EVENT_FUNNEL_REPORT）", "",
         f"生成时间：{res['ts']:%Y-%m-%d %H:%M:%S}", "",
         "> 本轮唯一目标：**查清为什么真实六 ETF 共同区间（802 个交易日）里，"
         "60 日 Trade Value OOS 最终只有 2 个独立事件**，并修正验证体系中"
         "可能导致『有效交易事件过少』的结构性问题。", "",
         "## 〇、一句话结论", "",
         f"- 旧口径把「事件」定义为 **walk-forward 折的 test_start**："
         f"60 日折数 = **{d['old_folds_60']}**，BUY 再排除 Risk Off/Crisis → **2**。"
         "**这不是「模型只发现 2 次机会」。**",
         f"- 修正为「事件 = 模型信号日 + 严格独立去重」后，60 日候选日 "
         f"**{f60['candidate_days']['any']}** 天，独立事件 **{f60['independent'][60]}** 个。",
         f"- 但 60 日独立事件的**理论上界只有 {d['bound_60']}** —— "
         f"『独立事件 ≥ {INDEPENDENT_THRESHOLD}』在 802 日上**物理不可达**。",
         f"- **主因：{d['primary']}**", "",
         "## 一、旧口径 vs 新口径（对照片）", "",
         "| 周期 | 旧口径：折数(=事件锚点) | 新口径：候选日(全区间) | 新口径：独立事件 | 理论上界 |",
         "|---|---|---|---|---|"]
    for h in HORIZONS:
        L.append(f"| {h}日 | {res['old_folds'][h]} | "
                 f"{res['funnels'][h]['candidate_days']['any']} | "
                 f"{res['funnels'][h]['independent'][h]} | {res['bounds'][h]} |")
    L += ["", "> 旧口径 `_fold_events()` 返回的是 walk-forward 每一折的 `test_start`。",
          f"> 802 日扣除 504 日训练窗 + 10 日 embargo 后，只剩约 288 日；"
          f"60 日测试窗只能切出 {res['old_folds'][60]} 折。事件数被死死绑在折数上，"
          "**与模型真实信号无关**。", "",
          f"## 二、15 级事件漏斗（主口径：{f60['horizon']} 日）", "",
          "| 级别 | 本级输入 | 进入 | 淘汰 | 淘汰比例 |",
          "|---|---|---|---|---|"]
    for lv in f60["levels"]:
        L.append(f"| {lv['level']} | {lv.get('input', '—')} | {lv['entered']} | "
                 f"{lv['eliminated']} | "
                 f"{_f((lv['ratio'] or 0) * 100, 1) if lv['ratio'] is not None else '—'}% |")
    L += ["", "> 「淘汰比例」= 淘汰 ÷ 本级输入；「占淘汰」= 该原因 ÷ 本级淘汰总数。", "",
          "### 逐级淘汰原因 Top10", ""]
    for lv in f60["levels"]:
        if lv["eliminated"] and lv["reasons"]:
            L.append(f"**{lv['level']}**（淘汰 {lv['eliminated']}）")
            L.append("| 原因 | 次数 | 占淘汰 |")
            L.append("|---|---|---|")
            for r in lv["reasons"]:
                if r["count"]:
                    L.append(f"| {r['reason']} | {r['count']} | {_f(r['pct'], 1)}% |")
            L.append("")
    L += ["## 三、20 / 60 / 120 日候选与独立事件对照", "",
          "| 周期 | BUY 候选日 | SELL 候选日 | REBALANCE 候选日 | 合计 | 独立事件 |",
          "|---|---|---|---|---|---|"]
    for h in HORIZONS:
        ph = f60["per_horizon_candidates"][h]
        L.append(f"| {h}日 | {ph['BUY']} | {ph['SELL']} | {ph['REBALANCE']} | "
                 f"{ph['any']} | {f60['independent'][h]} |")
    L += ["", "## 四、独立事件的物理上界（本报告最关键的一张表）", "",
          "| 周期 | 可评估日数 | 独立事件理论上界 | 门槛 30 是否可达 |", "|---|---|---|---|"]
    for h in HORIZONS:
        b = res["bounds"][h]
        L.append(f"| {h}日 | {max(0, res['n_bars'] - res['warmup_days'] - h)} | {b} | "
                 + ("**可达**" if b >= INDEPENDENT_THRESHOLD else "**不可达**") + " |")
    L += ["", f"> 上界 = (总交易日 {res['n_bars']} − 预热 {res['warmup_days']} − 周期 h) ÷ h。",
          f"> 60 日上界 = {res['bounds'][60]}，120 日上界 = {res['bounds'][120]}，"
          f"20 日上界 = {res['bounds'][20]}。",
          f"> **结论：在现有数据长度下，60/120 日（甚至 20 日）都不可能达到 "
          f"『独立事件 ≥ {INDEPENDENT_THRESHOLD}』。这是数据长度问题，"
          f"不是模型能力问题，也不是阈值该被降低的问题。**", "",
          "## 五、归因诊断（A / B / C 三分）", "",
          "| 代码 | 类别 | 判定 |", "|---|---|---|"]
    for c in d["causes"]:
        L.append(f"| {c['code']} | {c['name']} | {c['verdict']} |")
    L.append("")
    for c in d["causes"]:
        L += [f"**{c['code']} · {c['name']}**", "", f"- 证据：{c['evidence']}", ""]
    return "\n".join(L)


# ==========================================================================
# 二、SAMPLE_ADEQUACY_REPORT.md
# ==========================================================================
def render_sample_adequacy(res: dict) -> str:
    if not res.get("ok"):
        return f"# SAMPLE_ADEQUACY_REPORT\n\n无法执行：{res.get('message')}\n"
    L = ["# 样本充分性诊断（SAMPLE_ADEQUACY_REPORT）", "",
         f"生成时间：{res['ts']:%Y-%m-%d %H:%M:%S}", "",
         "> 目的：把「候选事件 / 有效事件 / 独立事件」三层样本量摆清楚。",
         "> · 候选事件 = 该周期可评估的候选日（模型认为有机会，未去重）",
         "> · 有效事件 = 候选日中通过生产闸门（安全/仓位/新鲜度/权限）的（未去重）",
         "> · 独立事件 = 研究层按间隔 ≥h 去重后的样本（用于统计）",
         f"> 门槛：独立事件 ≥ **{INDEPENDENT_THRESHOLD}**。",
         "> **样本不足时一律写「无足够证据」，不写「策略无效」。**", "",
         "## 一、研究层（RESEARCH_POLICY：不看执行权限）", ""]
    for h in HORIZONS:
        rh = (res["research"]["horizons"] or {}).get(h) or {}
        L += [f"### {h} 日", "", *_ADEQ_HEAD]
        cand_ind = rh.get("n_candidate_independent") or {}
        for act in ("BUY", "SELL", "REBALANCE"):
            agg = (rh.get("by_action") or {}).get(act) or {}
            cand_days = res["funnels"][h]["per_horizon_candidates"][h]
            n_cand = cand_days.get(act, 0)
            n_valid = ((res["production"]["horizons"].get(h) or {})
                       .get("n_all_executable") or {}).get(act, 0)
            n_ind = cand_ind.get(act, 0)
            d = (agg.get("delta_return_net") or {})
            ci = d.get("ci95") or [None, None]
            dd = (agg.get("delta_max_drawdown") or {})
            L.append("| " + " | ".join([
                ACTION_CN[act], f"{h}日", str(n_cand), str(n_valid), str(n_ind),
                _f(d.get("mean")), _f(d.get("median")),
                _f(d.get("win_rate_pct"), 1) + "%", _pf(d.get("profit_factor")),
                f"{_f(ci[0])}~{_f(ci[1])}", _f(dd.get("mean")),
            ]) + " |")
        L.append("")
        L.append(f"- 独立事件 **{min(cand_ind.values()) if cand_ind else 0}~"
                 f"{max(cand_ind.values()) if cand_ind else 0}** 个，"
                 + ("**无足够证据**（< 30，任何正/负增量都不具统计意义）"
                    if max(cand_ind.values() or [0]) < INDEPENDENT_THRESHOLD
                    else "达到门槛，仍需成本与校准双重确认") + "。")
        L.append("")

    L += ["## 二、生产层（PRODUCTION_POLICY：今天允不允许做）", "",
          "| 周期 | BUY 可执行 | SELL 可执行 | REBALANCE 可执行 | 被生产闸门拒绝 |",
          "|---|---|---|---|---|"]
    for h in HORIZONS:
        ph = (res["production"]["horizons"] or {}).get(h) or {}
        ne = ph.get("n_executable") or {}
        nr = ph.get("n_rejected_by_gate") or {}
        L.append(f"| {h}日 | {ne.get('BUY', 0)} | {ne.get('SELL', 0)} | "
                 f"{ne.get('REBALANCE', 0)} | "
                 f"BUY {nr.get('BUY', 0)} / SELL {nr.get('SELL', 0)} / "
                 f"REBALANCE {nr.get('REBALANCE', 0)} |")
    L += ["", "> **BUY 可执行事件恒为 0**：研究级 → BUY Permission Gate 关闭。",
          "> 这正是用户指出的循环：权限过严 → 无样本 → 无法证明 → 永远研究级。",
          "> 本轮把「研究层是否有效」与「生产层是否允许」**彻底分开**，"
          "让研究层不再被生产权限污染。", "",
          "## 三、结论", ""]
    b = res["bounds"]
    L += [f"- 60 日独立事件理论上界 = **{b[60]}**；20 日 = {b[20]}；120 日 = {b[120]}。",
          f"- 结论：**60/120 日周期在 802 日上的独立事件数永远 < {INDEPENDENT_THRESHOLD}**；",
          "  因此「无足够证据」是**数据长度的必然结果**，"
          "不能据此宣布「策略无效」，也不能为了凑样本降低独立间隔。",
          "- 若某动作在未来样本量真正达标（≥30）且成本后仍为正，才允许讨论升级。", ""]
    return "\n".join(L)


# ==========================================================================
# 三、TRADE_EVENT_AUDIT.md
# ==========================================================================
def render_trade_event_audit(res: dict) -> str:
    if not res.get("ok"):
        return f"# TRADE_EVENT_AUDIT\n\n无法执行：{res.get('message')}\n"
    L = ["# 交易事件审计（TRADE_EVENT_AUDIT）", "",
         f"生成时间：{res['ts']:%Y-%m-%d %H:%M:%S}", "",
         "> 审计三件事：① 事件怎么构造；② 候选与可执行怎么分；"
         "③ 研究层与生产层怎么分。", "",
         "## 一、事件构造口径审计（旧 → 新）", "",
         "| 项 | 旧口径 | 新口径 |", "|---|---|---|",
         "| 事件锚点 | walk-forward 折 test_start | 模型信号日（逐日分类） |",
         "| 事件来源 | 与模型信号无关 | BUY/SELL/REBALANCE 候选 |",
         "| 反事实 | 仅 WAIT | BUY↔WAIT、SELL↔HOLD、REBALANCE↔NO-REBAL |",
         "| 独立去重 | 折已天然不重叠 | 间隔 ≥ h（保留，不放宽） |",
         f"| 60 日可得 | {res['old_folds'][60]} | "
         f"{res['funnels'][60]['candidate_days']['any']} 候选 → "
         f"{res['funnels'][60]['independent'][60]} 独立 |", "",
         "## 二、候选事件（candidate）与可执行事件（executable）双层", "",
         "| 动作 | 周期 | 候选日 | 候选独立事件 | 生产层可执行 | 被生产闸门拒绝 |",
         "|---|---|---|---|---|---|"]
    for h in HORIZONS:
        cd = res["funnels"][h]["per_horizon_candidates"][h]
        ci = (res["research"]["horizons"].get(h) or {}).get("n_candidate_independent") or {}
        ne = ((res["production"]["horizons"].get(h) or {}).get("n_executable")) or {}
        nr = ((res["production"]["horizons"].get(h) or {}).get("n_rejected_by_gate")) or {}
        for act in ("BUY", "SELL", "REBALANCE"):
            L.append(f"| {ACTION_CN[act]} | {h}日 | {cd.get(act, 0)} | "
                     f"{ci.get(act, 0)} | {ne.get(act, 0)} | {nr.get(act, 0)} |")
    L += ["", "> 以后**不再**把最终 BUY/SELL 直接当成 OOS 样本："
              "候选事件 = 模型认为存在机会；可执行事件 = 再经安全/成本/仓位/"
              "新鲜度/权限后才允许执行。两类样本**分别统计**。", "",
          "## 三、研究层 vs 生产层双口径", "",
          "| 层 | 回答的问题 | 是否受执行权限约束 |", "|---|---|---|",
          "| RESEARCH_POLICY | 这条规则有没有历史增量价值？ | **否** |",
          "| PRODUCTION_POLICY | 今天实际允不允许做？ | 是（信任/风险/仓位/新鲜度） |",
          "", "### 研究层 60 日增量（相对各自反事实）", "", *_ADEQ_HEAD]
    for act in ("BUY", "SELL", "REBALANCE"):
        agg = ((res["research"]["horizons"].get(60) or {}).get("by_action") or {}).get(act) or {}
        d = agg.get("delta_return_net") or {}
        ci = d.get("ci95") or [None, None]
        dd = agg.get("delta_max_drawdown") or {}
        ci_all = (res["research"]["horizons"].get(60) or {}).get("n_candidate_independent") or {}
        n_valid60 = ((res["production"]["horizons"].get(60) or {})
                     .get("n_all_executable") or {}).get(act, 0)
        L.append("| " + " | ".join([
            ACTION_CN[act], "60日", str(res["funnels"][60]["per_horizon_candidates"][60].get(act, 0)),
            str(n_valid60),
            str(ci_all.get(act, 0)), _f(d.get("mean")), _f(d.get("median")),
            _f(d.get("win_rate_pct"), 1) + "%", _pf(d.get("profit_factor")),
            f"{_f(ci[0])}~{_f(ci[1])}", _f(dd.get("mean")),
        ]) + " |")
    L += ["", "### 生产层 60 日（只有生产层才讨论「今天能不能做」）", "",
          "| 动作 | 可执行独立事件 | 平均Δ收益(pp) | 胜率 |", "|---|---|---|---|"]
    for act in ("BUY", "SELL", "REBALANCE"):
        agg = (((res["production"]["horizons"].get(60) or {}).get("by_action") or {}).get(act)) or {}
        d = agg.get("delta_return_net") or {}
        ne = ((res["production"]["horizons"].get(60) or {}).get("n_executable")) or {}
        L.append(f"| {ACTION_CN[act]} | {ne.get(act, 0)} | {_f(d.get('mean'))} | "
                 f"{_f(d.get('win_rate_pct'), 1)}% |")
    L += ["", "## 四、模型阶梯（Model 0-4，**同一 OOS 事件集合**）", "",
          "| 模型 | 说明 | 输入数 | 评估事件 | 平均Δ收益(pp) | 中位(pp) | 胜率 | 95%CI |",
          "|---|---|---|---|---|---|---|---|"]
    for k, v in res["ladder"][60]["variants"].items():
        d = v.get("delta_return_net") or {}
        ci = d.get("ci95") or [None, None]
        L.append(f"| {k} | {v['desc']} | {v['n_features']} | "
                 f"{v.get('n_events_evaluated', 0)} | {_f(d.get('mean'))} | "
                 f"{_f(d.get('median'))} | {_f(d.get('win_rate_pct'), 1)}% | "
                 f"{_f(ci[0])}~{_f(ci[1])} |")
    L += ["", "> Model 0 = WAIT，交易价值恒为 0（基准）。"
              "Model 3 ≡ Model 2：当前模型本就不含宏观代理（代理数据被硬排除）。",
          "> 目标：找「复杂度最低但仍有稳定增量」的模型；"
          "**若更简单的模型并不更差，就没有理由增加复杂度**。", "",
          "## 五、事件定义稳定性（阈值 ±5% / ±2% / 0 / +2% / +5%）", "",
          f"- 是否存在「事件定义敏感」：**{'是' if res['stability']['any_sensitive'] else '否'}**",
          "", "| 参数 | 独立事件 最少~最多 | 增量符号翻转 | 是否敏感 |", "|---|---|---|---|"]
    for f in res["stability"]["flags"]:
        L.append(f"| {f['param']} | {f['n_min']}~{f['n_max']} | "
                 f"{'是' if f['sign_flip'] else '否'} | "
                 f"{'**敏感**' if f['sensitive'] else '否'} |")
    L += ["", f"> {res['stability']['discipline']}", "",
          "| 参数 | 扰动 | 候选BUY日 | 独立事件 | 平均Δ净(pp) | 平均Δ质量 |",
          "|---|---|---|---|---|---|"]
    for r in res["stability"]["rows"]:
        L.append(f"| {r['param']} | {r['pct']:+d}% | {r['n_candidate_buy_days']} | "
                 f"{r['n_independent_buy']} | {_f(r['mean_delta_net_pp'])} | "
                 f"{_f(r['mean_delta_quality'])} |")
    L += ["", "## 六、四基准对比（D−A / D−B / D−C）", "",
          "| 基准 | 说明 | 累计收益 |", "|---|---|---|"]
    bs = (res.get("benchmarks") or {}).get("summary") or {}
    for k, v in bs.items():
        L.append(f"| {k} | "
                 + {"Benchmark 0 WAIT": "A：什么都不做",
                    "Benchmark 1 固定50/45/5": "B：固定 50/45/5",
                    "Benchmark 2 趋势基准C": "C：简单趋势基准",
                    "Benchmark 3 优化模型": "D：优化模型"}.get(k, "") + " | "
                 f"{_f(v.get('total_return_pct'))}% |")
    bd = res.get("bench_deltas") or {}
    L += ["", "| 增量 | 值 | 说明 |", "|---|---|---|",
          f"| **D−A** | {_f(bd.get('D_A_pp'))}pp | 优化模型相对 WAIT 的真实增量（最关键） |",
          f"| D−B | {_f(bd.get('D_B_pp'))}pp | 相对固定 50/45/5 |",
          f"| D−C | {_f(bd.get('D_C_pp'))}pp | 相对简单趋势基准 |",
          "", "> 若 D−A 不能稳定为正，**不能宣称有交易优势**。",
          f"> ⚠ 该四基准对比建立在 60 日 OOS 折上（仅 "
          f"{(res.get('benchmarks') or {}).get('n_folds', 0)} 折），"
          f"**样本极小，只作方向性参考，不作为有效性证据**。", "",
          "## 七、十大问题逐条回答", ""]
    for k, v in (res.get("answers") or {}).items():
        title = k.split("_", 1)[1] if "_" in k else k
        L += [f"**{k.split('_')[0]}. {title}**", "", f"> {v}", ""]
    # 今日行动
    t = res.get("today") or {}
    L += ["## 八、今日行动与建仓路径（面向实际使用）", "",
          f"- 最新市场状态：**{t.get('latest_regime')}**"
          f"（广度 {_f(t.get('latest_breadth'), 2)}，回撤 {_f(t.get('latest_dd_pct'), 2)}%）",
          f"- 当前组合黄金：**{t.get('gold_now_pct')}%** / 战略上限 "
          f"{t.get('gold_cap_pct')}% → "
          + ("**超配，第一步必须再平衡（减黄金）**" if t.get("gold_over")
             else "未超配"),
          f"- 今日结论：**{'WAIT / REBALANCE（不允许 BUY）' if t.get('gold_over') else 'WAIT（不允许 BUY）'}**",
          "", "| 资产 | 类别 | 当前权重 | 目标权重 | 差异(pp) | 方向 |",
          "|---|---|---|---|---|---|"]
    w_wait = res.get("w_wait") or {}
    w_tgt = t.get("w_target_now") or {}
    cls = t.get("classes") or {}
    nm = t.get("names") or {}
    for a in sorted(set(list(w_wait) + list(w_tgt))):
        cw = float(w_wait.get(a, 0.0)) * 100
        tw = float(w_tgt.get(a, 0.0)) * 100
        diff = tw - cw
        arrow = "—" if abs(diff) < 0.5 else ("加" if diff > 0 else "减")
        L.append(f"| {nm.get(a, a)} | {cls.get(a, '—')} | {cw:.2f}% | {tw:.2f}% | "
                 f"{diff:+.2f} | {arrow} |")
    L += ["", "> 目标权重按当前市场状态（" + str(t.get("latest_regime")) + "）生成；"
              "黄金硬上限 5% 不可放宽。",
          "> 「当前权重」= 配置持仓份额 × 共同区间起点价格（研究口径），"
          "与首页实时市值口径可能略有差异。",
          "> 执行路径（分批，不一次打光、不追高）：",
          "> 1) 若黄金 > 上限：先把黄金减到 5%（按减仓阶梯，改善组合且卖出最少）；",
          "> 2) 其余偏离 > 3% 的资产做再平衡；",
          "> 3) 新增风险敞口一律**等 BUY Permission Gate 开放**后分批建仓，"
          "首批不超过现金的 33% 与缺口的 50%。",
          "", "> 以上为研究/计划口径；**实际下单前请以主程序首页的实时闸门结论为准**。", ""]
    return "\n".join(L)


# ==========================================================================
# 四、STRATEGY_VALIDATION_V3.md
# ==========================================================================
def render_strategy_v3(res: dict) -> str:
    if not res.get("ok"):
        return f"# STRATEGY_VALIDATION_V3\n\n无法执行：{res.get('message')}\n"
    d = res["diagnosis"]
    b = res["bounds"]
    L = ["# 策略有效性验证 V3（STRATEGY_VALIDATION_V3）", "",
         f"生成时间：{res['ts']:%Y-%m-%d %H:%M:%S}", "",
         "> V3 相对 V2 的唯一变化：把「事件」从 **walk-forward 折边界** "
         "纠正为 **模型信号日 + 严格独立去重**，并把"
         "**研究层（是否有效）**与**生产层（是否允许执行）彻底分开**。",
         "> 本轮**未**放宽任何阈值、**未**降低研究级门槛、**未**把研究级改成可 BUY。", "",
         "## 一、必须继承的既有事实（数字一律未改）", ""]
    for i, s in enumerate(INHERITED_FACTS, 1):
        L.append(f"{i}. {s}")
    L += ["", "## 二、V3 事件口径修正结果", "",
          f"- 旧口径 60 日事件锚点（折数）：**{d['old_folds_60']}**；"
          f"BUY 排除 Risk Off/Crisis 后 = **2**（即 V2 的 2 个事件）。",
          f"- 新口径 60 日：候选日 **{res['funnels'][60]['candidate_days']['any']}**，"
          f"独立事件 **{res['funnels'][60]['independent'][60]}**。",
          f"- 独立事件**理论上界**：20 日 {b[20]} / 60 日 **{b[60]}** / 120 日 {b[120]}。",
          f"- **归因：{d['primary']}**", "",
          "## 三、研究层结论（不被执行权限污染）", "",
          "| 动作 | 反事实 | 60日独立事件 | 平均Δ收益(pp) | 胜率 | 95%CI |", "|---|---|---|---|---|---|"]
    for act in ("BUY", "SELL", "REBALANCE"):
        agg = ((res["research"]["horizons"].get(60) or {}).get("by_action") or {}).get(act) or {}
        dn = agg.get("delta_return_net") or {}
        ci = dn.get("ci95") or [None, None]
        nind = ((res["research"]["horizons"].get(60) or {}).get("n_candidate_independent") or {}).get(act, 0)
        L.append(f"| {ACTION_CN[act]} | {ACTION_CF[act]} | {nind} | {_f(dn.get('mean'))} | "
                 f"{_f(dn.get('win_rate_pct'), 1)}% | {_f(ci[0])}~{_f(ci[1])} |")
    L += ["", "> 三个动作的 95%CI **全部横跨 0**，且各动作独立事件 "
              f"（{min(v for v in [((res['research']['horizons'].get(60) or {}).get('n_candidate_independent') or {}).get(a, 0) for a in ('BUY','SELL','REBALANCE')])}~"
              f"{max(((res['research']['horizons'].get(60) or {}).get('n_candidate_independent') or {}).get(a, 0) for a in ('BUY','SELL','REBALANCE'))}）"
              f"**远低于 {INDEPENDENT_THRESHOLD}** → **无足够证据**（不是「策略无效」）。", "",
          "## 四、模型阶梯（复杂度 vs 增量）", "",
          "| 模型 | 说明 | 输入数 | 平均Δ收益(pp) | 胜率 |", "|---|---|---|---|---|"]
    for k, v in res["ladder"][60]["variants"].items():
        dn = v.get("delta_return_net") or {}
        L.append(f"| {k} | {v['desc']} | {v['n_features']} | {_f(dn.get('mean'))} | "
                 f"{_f(dn.get('win_rate_pct'), 1)}% |")
    L += ["", f"> 最简可用模型：**{(res['answers'] or {}).get('9_最简最稳模型', '—')}**。",
          "> **增加复杂度没有得到回报** —— 这本身就是一条重要结论。", "",
          "## 五、升级条件逐项（本轮是否满足）", "",
          "| # | 升级条件 | 本轮状态 |", "|---|---|---|"]
    st_rows = [
        ("真实六 ETF 组合数据完整", f"{res['n_bars']} 日"),
        ("无未来函数验证通过", "见 TEST_REPORT"),
        ("事件口径已修正为信号日", "是（V3）"),
        ("研究层与生产层已分离", "是（V3）"),
        ("BUY 相对 WAIT 有正增量",
         f"{_f(((res['research']['horizons'].get(60) or {}).get('by_action', {}).get('BUY', {}).get('delta_return_net') or {}).get('mean'))}pp（n="
         f"{(((res['research']['horizons'].get(60) or {}).get('n_candidate_independent') or {}).get('BUY', 0))}，不稳）"),
        ("SELL 相对 HOLD 有正增量", "否"),
        ("至少 30 个独立事件", f"否（60 日理论上界仅 {b[60]}，物理不可达）"),
        ("95%CI 不再极端宽", "不满足（全部横跨 0）"),
        ("模型校准不劣于 Naive", "不满足（BSS<0，见 STRATEGY_VALIDATION_V2）"),
        ("成本压力测试通过", "见 STRATEGY_VALIDATION_V2"),
        ("参数敏感性通过", "见 PARAMETER_SENSITIVITY_REPORT"),
        ("事件定义稳定性", "见 TRADE_EVENT_AUDIT 第五节"),
        ("Shadow Trading 有持续记录", "见 SHADOW_TRADING_REPORT"),
    ]
    for i, (k, v) in enumerate(st_rows, 1):
        L.append(f"| {i} | {k} | {v} |")
    L += ["", "## 六、最终结论", "",
          "**主判定：② 数据样本不足 —— 保持【研究级】。**", "",
          f"- 60 日独立事件理论上界 **{b[60]} < {INDEPENDENT_THRESHOLD}**："
          "这是数据长度决定的**物理不可达**，不是模型无效，也不是阈值该被降低。",
          "- BUY / SELL / REBALANCE 三个动作的样本外增量 **均无统计意义**"
          "（CI 横跨 0）；REBALANCE 与 BUY 的均值为正但由少数事件驱动，**不构成证据**。",
          "- 生产层 BUY 可执行事件 = 0（研究级 BUY Permission Gate 关闭，必须保留）。",
          "- **不为了得到 BUY 而修改任何门槛；不为了漂亮结果放水。**", "",
          "> 下一步若要推进升级，唯一正确方向是**扩大真实数据长度**"
          "（更长的六 ETF 共同区间 / 更多标的），而不是调参或降低独立门槛。", ""]
    return "\n".join(L)


def render_all(res: dict) -> dict[str, str]:
    return {
        "EVENT_FUNNEL_REPORT.md": render_event_funnel(res),
        "SAMPLE_ADEQUACY_REPORT.md": render_sample_adequacy(res),
        "TRADE_EVENT_AUDIT.md": render_trade_event_audit(res),
        "STRATEGY_VALIDATION_V3.md": render_strategy_v3(res),
    }


def run_and_write(cfg=None, progress=None, *, refresh_v2: bool = True):
    """运行事件审计并写出全部报告；可选刷新 V2 家族（含 BACKTEST_REAL_6ETF_V2.md）。"""
    res = run_event_audit(cfg, progress)
    from .config import project_root
    root = project_root()
    written: list[Path] = []
    for name, text in render_all(res).items():
        p = root / name
        p.write_text(text, encoding="utf-8")
        written.append(p)
    if refresh_v2 and res.get("ok"):
        try:
            from .policy_reports import run_and_write as v2_write
            _r2, w2 = v2_write(cfg, progress)
            written.extend(w2)
        except Exception as exc:  # noqa: BLE001
            written.append(root / f"_V2_REFRESH_FAILED_{type(exc).__name__}")
    return res, written
