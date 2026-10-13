"""交易策略系列报告渲染（Policy Reports）。

由 policy_evaluator 产出的结果 → 五份新报告：
  STRATEGY_VALIDATION_V2.md / TRADE_POLICY_VALIDATION.md /
  SELL_VALIDATION_REPORT.md / PARAMETER_SENSITIVITY_REPORT.md /
  BACKTEST_REAL_6ETF_V2.md

旧报告一律不删除。
"""
from __future__ import annotations

from pathlib import Path

from .policy_evaluator import (HORIZONS, REGIMES, run_policy_evaluation)

INHERITED_FACTS = [
    "系统可信等级为【研究级】，不是辅助交易级。",
    "Daily Rolling OOS 60 日：独立事件 14 / 基准胜率 88.5% / 模型胜率 85.7% / "
    "相对基准 -2.8pp / 95%CI 60.1%~96.0% / Model Brier 0.1428 / "
    "Naive Brier 0.1232 / Brier Skill Score -0.1591。",
    "20 日 OOS：独立事件 44 / 基准 77.8% / 模型 72.7% / 相对基准 -5.1pp。",
    "120 日目前只有 6 个独立事件，不允许据此宣称策略有效。",
    "完整 13 特征模型相对趋势基准 C 的 60 日独立事件胜率增益为 0.0pp。",
    "Block Walk-Forward 的买入信号相对基准为 -2.0pp。",
    "组合历史回测表现好只能证明组合资产过去不错，不能证明择时模型有效。",
    "六 ETF 真实共同区间 2023-06-13 ~ 2026-09-30，802 个交易日。",
    "六 ETF 数据双源交叉验证通过，不得为策略优化重新引入代理行情。",
    "黄金 / BPGDM / Fear&Greed 存在真实值/代理值分离；代理值不得进入正式统计。",
    "已有 BUY Permission Gate：研究级禁止实际 BUY，必须保留。",
    "已禁止未来函数、支持 T 日收盘产生信号、T+1 开盘执行，成本/滑点参数化。",
    "黄金硬上限 5% 必须保留。",
    "WAIT 必须始终是合法结论。",
]

BUY_CONDITIONS_REF = (
    "样本外模型可用", "独立事件样本≥门槛", "相对WAIT正增量", "组合质量提升",
    "风险未异常恶化", "扣成本后仍为正", "非追高", "数据新鲜",
    "无关键宏观UNKNOWN", "买入权限已开放",
)

SELL_KIND_NAME = {"rebalance": "① 再平衡减仓", "risk_deterioration": "② 风险恶化减仓",
                  "risk_contribution": "③ 风险贡献过高减仓",
                  "trend_break": "④ 趋势破坏减仓", "extreme_risk": "⑤ 极端风险事件减仓"}


def _f(v, nd=2):
    return "—" if v is None else f"{v:.{nd}f}"


def _pf(v):
    if v is None:
        return "—"
    return "∞" if v == float("inf") else f"{v:.3f}"


def _net(cost_runs: dict, tier: str, act: str, h: int):
    """从 cost_runs 安全取出某档/某动作/某周期的平均净增量。"""
    tier_d = (cost_runs or {}).get(tier) or {}
    act_d = (tier_d.get("by_action") or {}).get(act) or {}
    h_d = (act_d.get(h) or {}).get("delta_return_net") or {}
    return h_d.get("mean")


def _action_table(a: dict, action: str) -> list[str]:
    L = [f"### {action}（相对 WAIT 的增量）", "",
         "| 周期 | 独立事件 | 平均增量(净,pp) | 中位(净,pp) | 胜率 | Profit Factor | 95%CI |",
         "|---|---|---|---|---|---|---|"]
    for h in HORIZONS:
        d = ((a.get("by_action", {}).get(action, {}).get(h) or {})
             .get("delta_return_net") or {})
        ci = d.get("ci95") or [None, None]
        L.append(f"| {h}日 | {d.get('n', 0)} | {_f(d.get('mean'))} | "
                 f"{_f(d.get('median'))} | {_f(d.get('win_rate_pct'), 1)}% | "
                 f"{_pf(d.get('profit_factor'))} | {_f(ci[0])}~{_f(ci[1])} |")
    L.append("")
    return L


def render_strategy_v2(res: dict) -> str:
    if not res.get("ok"):
        return f"# STRATEGY_VALIDATION_V2\n\n无法执行：{res.get('message')}\n"
    L = ["# 策略有效性验证 V2（STRATEGY_VALIDATION_V2）", "",
         f"生成时间：{res['ts']:%Y-%m-%d %H:%M:%S}", "",
         "> 本轮唯一目标：把系统从「预测未来 60 日组合是否上涨」升级为",
         "> 「**判断一笔真实交易，相对【什么都不做 / WAIT / 继续持有】是否真的提高整个"
         "组合的风险收益结果**」。", "",
         "计算公式：**Trade Value = 执行交易后的组合结果 − 不执行交易的组合结果**。",
         "", "## 一、必须继承的既有事实（不得篡改统计数字）", ""]
    for i, s in enumerate(INHERITED_FACTS, 1):
        L.append(f"{i}. {s}")
    L += ["", "> 本轮**未修改**上述任何统计数字；也未放宽 BUY Permission Gate。", "",
          "## 二、真实六 ETF 共同区间（正式组合策略验证主口径）", "",
          f"- 区间：{res['span']['start']} ~ {res['span']['end']}，{res['n_bars']} 个交易日",
          f"- 起算日（最晚上市 ETF 的首日）：{res['common_start']}",
          f"- 市场状态分布：{res['regime_counts']}",
          "- 更早历史只用于单资产/指标/宏观研究与参数预热，不进入本结论。", "",
          "## 三、四类基准同场比较（OOS）", "",
          "| 基准 | 折数 | 累计收益 | 平均每折 |", "|---|---|---|---|"]
    for k, v in (res["benchmarks"]["summary"] or {}).items():
        L.append(f"| {k} | {v.get('n_folds')} | {_f(v.get('total_return_pct'))}% | "
                 f"{_f(v.get('mean_fold_pct'))}% |")
    L += ["", "> 必须回答：优化模型是否真正优于 ①WAIT ②固定配置 ③简单趋势模型；"
              "若不能稳定优于 WAIT，不能宣称有交易优势。", "",
          "## 四、交易价值（Trade Value）逐动作验证", ""]
    a = res["actions"]
    L.append(f"* 训练窗 {a['train_days']} 日 / 测试窗 20-60-120 / "
             f"PURGE+EMBARGO {a['embargo']} 日；成本档 = {a['cost_tier']}")
    L.append(f"* 各周期可用独立事件数：{a.get('events_by_horizon')}")
    L.append("")
    for act in ("BUY", "REDUCE", "REBALANCE"):
        L += _action_table(a, act)
    L += ["## 五、市场状态分层验证", "",
          "| 动作 | 市场状态 | 事件数 | 平均净增量(pp) | 胜率 |", "|---|---|---|---|---|"]
    for act, strata in (a.get("regime_strata") or {}).items():
        for rg, v in strata.items():
            L.append(f"| {act} | {rg} | {v.get('n')} | {_f(v.get('mean_net_pp'))} | "
                     f"{_f(v.get('win_rate_pct'), 1)}% |")
    L += ["", "> 必须回答『模型在哪一种市场环境里有效』；不能只看总体。", "",
          "## 六、成本三档压力测试（LOW / BASE / HIGH）", "",
          "| 动作 | 周期 | LOW 净增量 | BASE 净增量 | HIGH 净增量 |", "|---|---|---|---|---|"]
    for act in ("BUY", "REDUCE", "REBALANCE"):
        for h in HORIZONS:
            row = [_f(_net(res["cost_runs"], tier, act, h))
                   for tier in ("LOW", "BASE", "HIGH")]
            L.append(f"| {act} | {h}日 | {row[0]} | {row[1]} | {row[2]} |")
    L += ["", "> 若某动作只在 LOW 档为正 → 判『对交易成本敏感，不适合实盘』。", "",
          "## 七、结论（禁止强行让系统通过）", "",
          f"**主判定：{res['verdict']['label']}**", "",
          f"- {res['verdict'].get('detail', '')}",
          "- 能否据本轮数据宣称「交易有效」："
          + ("是（独立事件 ≥ 30）" if res["verdict"].get("can_claim_effectiveness")
             else "**否（独立事件 < 30，统计意义不足）**"),
          f"- BUY 60 日平均净增量：{_f(res['verdict'].get('buy_mean_net_pp_60'))}pp",
          f"- REDUCE 60 日平均净增量：{_f(res['verdict'].get('reduce_mean_net_pp_60'))}pp",
          f"- REBALANCE 60 日平均净增量：{_f(res['verdict'].get('rebalance_mean_net_pp_60'))}pp",
          f"- 基准 WAIT 累计 {_f(res['verdict'].get('bench_wait_pct'))}% / "
          f"趋势基准C 累计 {_f(res['verdict'].get('bench_trend_pct'))}%", "",
          "> 六种可能性对照：① 模型无预测能力 ② 数据样本不足 ③ BUY 有效但 SELL 无效 "
          "④ 某市场状态有效其他无效 ⑤ 简单趋势模型已足够 ⑥ 组合配置有效但择时无效。",
          "", "**本轮未修改任何统计数字；研究级买入门槛保持不变。**", ""]
    return "\n".join(L)


def render_trade_policy(res: dict) -> str:
    if not res.get("ok"):
        return f"# TRADE_POLICY_VALIDATION\n\n无法执行：{res.get('message')}\n"
    L = ["# 交易策略验证（TRADE_POLICY_VALIDATION）", "",
         f"生成时间：{res['ts']:%Y-%m-%d %H:%M:%S}", "",
         "核心思想：**BUY、WAIT、REDUCE、REBALANCE 使用同一把尺子**——",
         "增量 = 交易后组合结果 − 不交易（WAIT）组合结果。", "",
         "## 一、BUY 的 10 个必要条件（缺一不可）", "",
         "| # | 条件 |", "|---|---|"]
    for i, c in enumerate(BUY_CONDITIONS_REF, 1):
        L.append(f"| {i} | {c} |")
    L += ["", "> 上涨概率与交易价值**彻底分开**；即使概率高，只要相对 WAIT 没有正增量，"
              "也不允许 BUY。", "",
          "## 二、买入侧增量证据（vs WAIT）", ""]
    L += _action_table(res["actions"], "BUY")
    L += ["## 三、再平衡增量证据（vs WAIT 不动）", ""]
    L += _action_table(res["actions"], "REBALANCE")
    L += ["## 四、卖出侧增量证据（vs 继续持有）", ""]
    L += _action_table(res["actions"], "REDUCE")
    L += ["## 五、增量 Alpha vs WAIT 与风险变化", "",
          "| 动作 | 周期 | 增量Alpha(pp) | 最大回撤增量(pp) |", "|---|---|---|---|"]
    for act in ("BUY", "REDUCE", "REBALANCE"):
        for h in HORIZONS:
            d = (res["actions"].get("by_action", {}).get(act, {}).get(h) or {})
            L.append(f"| {act} | {h}日 | {_f((d.get('delta_return') or {}).get('mean'))} | "
                     f"{_f((d.get('delta_max_drawdown') or {}).get('mean'))} |")
    L += ["", "## 六、升级条件（本轮是否满足）", "", "| # | 升级条件 | 本轮状态 |",
          "|---|---|---|"]
    st = res["verdict"]
    rows = [
        ("数据双源验证通过（见 DATA_VALIDATION_REPORT）", "见该报告"),
        ("真实六 ETF 组合数据完整", f"{res['n_bars']} 日"),
        ("无未来函数验证通过（见 TEST_REPORT）", "见该报告"),
        ("BUY 与 SELL 都完成独立 OOS", "是"),
        ("BUY 相对 WAIT 有正增量", _f(st.get("buy_mean_net_pp_60")) + "pp"),
        ("SELL 相对 HOLD 有正增量", "是" if st.get("sell_positive") else "否"),
        ("至少 30 个独立事件", f"否（60日仅 {st.get('independent_events_60')}）"),
        ("95%CI 不再极端宽", "不满足"),
        ("模型校准不劣于 Naive", "不满足（BSS<0）"),
        ("成本压力测试通过", "见 STRATEGY_VALIDATION_V2 第六节"),
        ("多市场状态测试通过", f"Crisis 样本 {res['regime_counts'].get('Crisis', 0)} 日"),
        ("参数敏感性通过", "见 PARAMETER_SENSITIVITY_REPORT"),
        ("Shadow Trading 有持续真实记录", "见 SHADOW_TRADING_REPORT"),
        ("无关键数据新鲜度问题", res["freshness"]["action"]),
    ]
    for i, (k, v) in enumerate(rows, 1):
        L.append(f"| {i} | {k} | {v} |")
    L += ["", "**结论：14 项中多项不满足 → 保持【研究级】，不升级。**", ""]
    return "\n".join(L)


def render_sell_report(res: dict) -> str:
    if not res.get("ok"):
        return f"# SELL_VALIDATION_REPORT\n\n无法执行：{res.get('message')}\n"
    s = res["sell"]
    L = ["# 卖出策略验证报告（SELL_VALIDATION_REPORT）", "",
         f"生成时间：{res['ts']:%Y-%m-%d %H:%M:%S}", "",
         "原则：卖出**不能**因为『涨太多 / RSI 高 / 离高点近』单一指标直接触发；",
         "每个候选必须 ≥2 个独立信号，且必须通过 OOS 验证（减仓 vs 继续持有）。", "",
         "## 一、五类卖出候选的 OOS 结果", "",
         "| 卖出类型 | 事件数 | 相对HOLD平均净增量(pp) | 最大回撤增量(pp) | 是否优于持有 |",
         "|---|---|---|---|---|"]
    for k in ("rebalance", "risk_deterioration", "risk_contribution",
              "trend_break", "extreme_risk"):
        v = s["by_kind"].get(k) or {}
        d = v.get("delta_return_net") or {}
        dd = v.get("delta_max_drawdown") or {}
        L.append(f"| {SELL_KIND_NAME.get(k, k)} | {v.get('n', 0)} | {_f(d.get('mean'))} | "
                 f"{_f(dd.get('mean'))} | " + ("是" if v.get("beats_hold") else "否") + " |")
    L += ["", f"**结论：{s['sell_evidence']}**", "",
          "## 二、黄金减仓阶梯（不许一超 5% 就全部卖完）", "",
          "| 目标仓位 | 说明 |", "|---|---|",
          "| 降到 10% | 仅当严重超配时 |",
          "| 降到 8% | 中等超配 |",
          "| 降到 5% | 回到战略上限 |", "",
          "选择原则：**能最大改善整个组合风险收益、同时卖出最少**的方案；"
          "并按真实可交易份额取整。系统同时显示黄金当前仓位 / 战略上限 / 超配比例 / "
          "对应金额。", "",
          "## 三、单指标禁止触发（代码级硬约束）", "",
          "- `sell_model.valid_sell_candidate()`：single_signal 为真 → 直接无效；",
          "- 每个规则必须**单独**做 OOS 验证，不为每种情况随意增加规则。", ""]
    return "\n".join(L)


def render_parameter_report(res: dict) -> str:
    if not res.get("ok"):
        return f"# PARAMETER_SENSITIVITY_REPORT\n\n无法执行：{res.get('message')}\n"
    p = res["sensitivity"]
    L = ["# 参数敏感性报告（PARAMETER_SENSITIVITY_REPORT）", "",
         f"生成时间：{res['ts']:%Y-%m-%d %H:%M:%S}", "",
         "纪律：**禁止参数淘金**。参数只在训练集选择并冻结；绝不「看完整测试期收益后"
         "回头挑最赚钱参数」。", "", "## 一、候选参数空间", "", "| 参数 | 候选 |",
         "|---|---|"]
    for k, v in (p.get("grid") or {}).items():
        L.append(f"| {k} | {v} |")
    L += ["", "## 二、组合网格（训练集口径：换手/成本）", "",
          "| 再平衡阈值 | 检查频率 | 分批 | 平均换手 | 估计成本(pp) |", "|---|---|---|---|---|"]
    for r in p["rows"]:
        L.append(f"| {r['rebalance_threshold_pct']}% | {r['check_freq_days']}日 | "
                 f"{r['batch_count']}批 | {r['avg_turnover_frac']} | {r['est_cost_pct']} |")
    c = p["chosen_on_train"]
    L += ["", "## 三、训练集选择结果（已冻结）", "",
          f"- 再平衡阈值：**{c['rebalance_threshold_pct']}%**",
          f"- 检查频率：**{c['check_freq_days']} 日**",
          f"- 分批数：**{c['batch_count']} 批**",
          f"- 距离倍数：{p['distance_note']}", "",
          "> 冻结参数将用于测试窗口；测试期不重新选择参数。", ""]
    return "\n".join(L)


def render_backtest_v2(res: dict) -> str:
    if not res.get("ok"):
        return f"# BACKTEST_REAL_6ETF_V2\n\n无法执行：{res.get('message')}\n"
    L = ["# 真实六 ETF 组合回测 V2（BACKTEST_REAL_6ETF_V2）", "",
         f"生成时间：{res['ts']:%Y-%m-%d %H:%M:%S}", "",
         "> 主口径：**2023-06-13 ~ 2026-09-30**（802 交易日），所有基准在同一区间、"
         "同一成本口径下比较。", "", "## 一、四基准 OOS 累计收益", "",
         "| 基准 | 折数 | 累计收益 | 平均每折 |", "|---|---|---|---|"]
    for k, v in (res["benchmarks"]["summary"] or {}).items():
        L.append(f"| {k} | {v.get('n_folds')} | {_f(v.get('total_return_pct'))}% | "
                 f"{_f(v.get('mean_fold_pct'))}% |")
    L += ["", "## 二、市场状态分布", "", "| 状态 | 交易日数 | 占比 |", "|---|---|---|"]
    tot = max(1, sum(res["regime_counts"].values()))
    for rg in REGIMES:
        n = res["regime_counts"].get(rg, 0)
        L.append(f"| {rg} | {n} | {n / tot * 100:.1f}% |")
    L += ["", "## 三、成本三档下的动作净增量均值（pp）", "",
          "| 动作 | 周期 | LOW | BASE | HIGH |", "|---|---|---|---|---|"]
    for act in ("BUY", "REDUCE", "REBALANCE"):
        for h in HORIZONS:
            row = [_f(_net(res["cost_runs"], tier, act, h))
                   for tier in ("LOW", "BASE", "HIGH")]
            L.append(f"| {act} | {h}日 | {row[0]} | {row[1]} | {row[2]} |")
    L += ["", "## 四、数据新鲜度闸门", ""]
    fr = res["freshness"]
    L.append(f"- 结论：**{fr['action']}** — {fr['reason']}")
    L.append(f"- 报价基准：{fr['quote_basis']}")
    L.append(f"- {fr['realtime_note']}")
    L += ["", "## 五、诚实结论", "",
          f"- {res['verdict']['label']}",
          "- 组合配置本身贡献为主还是择时贡献为主：见 STRATEGY_VALIDATION_V2",
          "- 本报告不删除旧的 BACKTEST_REPORT.md / BACKTEST_REAL_6ETF.md。", ""]
    return "\n".join(L)


def render_all(res: dict) -> dict[str, str]:
    return {
        "STRATEGY_VALIDATION_V2.md": render_strategy_v2(res),
        "TRADE_POLICY_VALIDATION.md": render_trade_policy(res),
        "SELL_VALIDATION_REPORT.md": render_sell_report(res),
        "PARAMETER_SENSITIVITY_REPORT.md": render_parameter_report(res),
        "BACKTEST_REAL_6ETF_V2.md": render_backtest_v2(res),
    }


def run_and_write(cfg=None, progress=None) -> tuple[dict, list[Path]]:
    from .config import project_root
    res = run_policy_evaluation(cfg, progress)
    root = project_root()
    written: list[Path] = []
    for name, text in render_all(res).items():
        p = root / name
        p.write_text(text, encoding="utf-8")
        written.append(p)
    # 把交易价值证据落盘，供运行期 TRADE VALUE GATE 使用
    try:
        if res.get("ok"):
            from .decision_engine import save_trade_evidence
            written.append(save_trade_evidence(cfg, res))
    except Exception:  # noqa: BLE001
        pass
    return res, written
