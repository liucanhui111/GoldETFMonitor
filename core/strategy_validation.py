"""策略有效性验证（验收 §49/§55/§58 + P0-3/P0-4/P0-10）。

回答唯一问题：**这个概率模型相对"什么都不看"的基准，到底有没有择时增益？**

产出 STRATEGY_VALIDATION_REPORT.md。核心纪律（P0-3/P0-4）：

**两套样本外验证体系严格分开，禁止混用：**

- 体系 A【Block Walk-Forward】：每 126 个交易日取一个不重叠测试块，
  块数少（约 11 个），只能看方向，不能证明高胜率；
- 体系 B【Daily Rolling OOS】：每个交易日滚动预测，预测点数百级，
  但 60 日窗口高度重叠——必须同时报告 预测点数 / 独立事件数（间隔≥h，
  窗口不重叠）/ 按独立事件去重后的胜率与 95%CI。

首页默认采用体系 B，并显示：预测点数、独立事件数、正收益数、负收益数、
基准胜率、模型胜率、提升 pp、95%CI。

诚实原则：
1. 不使用"提升≥2pp 即有效"这类未经验证的硬阈值——只报告方向、数量与 CI；
2. 92% 这类数字是【历史频率】，不等于模型有 92% 的预测能力；
3. 任何 P0 验证报告缺失时，可信等级最高只能判【研究/观察级】。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from . import backtest as bt
from . import pipeline


# --------------------------------------------------------------------------
# 主入口
# --------------------------------------------------------------------------
def run_strategy_validation(cfg=None, progress=None) -> dict:
    def step(msg):
        if progress:
            progress(msg)

    step("加载真实组合与历史数据…")
    snap = pipeline.run_full(cfg, progress=progress, use_cache_first=True,
                             with_macro=False, save=False)
    if not snap.get("ok", True):
        return {"ok": False, "message": snap.get("message") or "快照构建失败"}

    p = snap["portfolio"]
    sim = p.get("simulator")
    base = p.get("baseline") or {}
    gate = ((p.get("plan") or {}).get("gate")) or {}

    # ---- 1. 四概率（最终修正：决策主口径 = 独立事件去重样本外） ----
    h60 = base.get("hist", {}).get(60) or {}
    dec = base.get("dec_prob") or {}
    four = {
        "A_base": dec.get("base_rate") or h60.get("all_history_win_rate_pct"),
        "B_cond": dec.get("conditional_prob") or h60.get("win_rate_pct"),
        "C_overlap": dec.get("oos_overlap_prob") or h60.get("oos_win_rate_pct"),
        "D_decision": dec.get("prob_used"),           # 决策主口径（独立事件去重样本外）
        "independent_events": dec.get("independent_events"),
        "D_uplift": dec.get("lift_pp"),               # 相对基础率（决策口径 − A）
        "samples": h60.get("samples"),
        "oos_samples": h60.get("oos_samples"),
        "ci": dec.get("ci95") or [h60.get("ci95_low"), h60.get("ci95_high")],
        "credibility": h60.get("credibility"),
    }

    # ---- 2. 体系 B：逐日滚动 OOS（完整模型 + 趋势基准C） ----
    step("体系B：逐日滚动样本外预测（完整模型，h=20/60/120）…")
    full_oos = (sim.daily_oos_predictions((20, 60, 120), step=1, progress=progress)
                if sim else {"ok": False, "message": "模拟器未就绪"})
    step("体系B：逐日滚动样本外预测（趋势基准C，h=60）…")
    trend_oos = (sim.trend_only_oos((60,), step=1)
                 if sim else {"ok": False, "message": "模拟器未就绪"})

    # ---- 3. 体系 A：Block Walk-Forward ----
    step("体系A：Block Walk-Forward 信号验证…")
    bt_res = bt.run_backtest(cfg, progress)
    wf = bt_res.get("walk_forward") or {}

    # ---- 4. 敏感性检查（P0-10） ----
    step("敏感性检查（成本 / 相似距离 / 特征消融）…")
    full60 = (full_oos.get("horizons") or {}).get(60) or {}
    t60 = (trend_oos.get("horizons") or {}).get(60) or {}
    sens = _sensitivity(cfg, bt_res, full60, t60)

    # ---- 5. P0 报告齐备性（P0-1/P0-10） ----
    from .config import project_root
    root = project_root()
    report_status: dict[str, str] = {}
    for name in ("DATA_VALIDATION_REPORT.md", "STRATEGY_VALIDATION_REPORT.md",
                 "BACKTEST_REPORT.md", "BACKTEST_REAL_6ETF.md"):
        report_status[name] = "存在" if (root / name).exists() else "缺失"
    dv_path = root / "DATA_VALIDATION_REPORT.md"
    data_val_clean = None
    if dv_path.exists():
        try:
            txt = dv_path.read_text(encoding="utf-8")
            if "未验证（缺第二数据源）" in txt or "差异（" in txt:
                data_val_clean = False
            elif "总体：全部 ETF 关键指标双源一致" in txt:
                data_val_clean = True
            else:
                data_val_clean = None   # 内容无法确认 → 按"未验证"处理
        except OSError:
            data_val_clean = None

    # ---- 6. 可信等级（P0-10） ----
    level, level_reasons = _trust_level(snap, four, full60, t60, wf, sens,
                                        report_status, data_val_clean)

    out = {
        "ok": True,
        "ts": datetime.now(),
        "four": four,
        "full_oos": full_oos,
        "trend_oos": trend_oos,
        "walk_forward": wf,
        "sensitivity": sens,
        "report_status": report_status,
        "data_val_clean": data_val_clean,
        "trust_level": level,
        "trust_reasons": level_reasons,
        "warnings": snap.get("warnings") or [],
    }
    return out


# --------------------------------------------------------------------------
# 敏感性检查（解析法，不重跑昂贵回测；P0-10）
# --------------------------------------------------------------------------
def _sensitivity(cfg, bt_res: dict, full60: dict, trend60: dict) -> dict:
    out: dict = {"items": []}

    # 1) 成本敏感性：全期换手 × 成本翻倍 → 收益下降多少（解析，不改排名以外的假设）
    try:
        cost = float(cfg.trade_cost())
    except Exception:  # noqa: BLE001
        cost = 0.002
    years = None
    span = bt_res.get("span") or ""
    try:
        d1 = datetime.strptime(span.split("~")[0].strip()[:10], "%Y-%m-%d")
        d2 = datetime.strptime(span.split("~")[1].strip()[:10], "%Y-%m-%d")
        years = max((d2 - d1).days / 365.25, 0.5)
    except Exception:  # noqa: BLE001
        years = None
    for r in (bt_res.get("strategies") or []):
        drag = float(r.get("cost_drag_pct") or 0.0)
        ann = drag / years if years else None
        out["items"].append({
            "kind": "成本×2",
            "target": r.get("label"),
            "detail": (f"全期成本拖累 {drag:.2f}pp；若佣金+滑点翻倍，"
                       f"全期收益再降约 {drag:.2f}pp"
                       + (f"（年化约 {ann:.2f}pp）" if ann is not None else ""))
            ,
            "flip_risk": "低" if drag < 5 else "中" if drag < 15 else "高",
        })
    out["cost_years"] = round(years, 1) if years else None

    # 2) 相似距离参数敏感性：是否长期贴着硬上限跑
    mult = full60.get("distance_mult_avg")
    out["items"].append({
        "kind": "相似距离",
        "target": "Daily OOS h=60",
        "detail": (f"相似阈值平均使用 {mult}×（硬上限 3.0×，超上限已禁止）"
                   if mult is not None else "无距离数据"),
        "flip_risk": ("低" if mult is not None and mult <= 2.5
                      else "中" if mult is not None and mult <= 3.0 else "高"),
    })

    # 3) 特征消融：完整模型 vs 趋势基准C（同框架）
    if full60.get("realized_win_rate_pct") is not None \
            and trend60.get("realized_win_rate_pct") is not None:
        gain = (full60["realized_win_rate_pct"]
                - trend60["realized_win_rate_pct"])
        out["items"].append({
            "kind": "特征消融",
            "target": "完整13特征 vs 趋势3特征",
            "detail": f"独立事件胜率差 {gain:+.1f}pp（正=多出的特征有增量）",
            "flip_risk": "低" if gain > 0 else "中",
        })
    return out


# --------------------------------------------------------------------------
# 可信等级（P0-10：综合判定，不用单一 2pp 阈值）
# --------------------------------------------------------------------------
def _trust_level(snap: dict, four: dict, full60: dict, trend60: dict,
                 wf: dict, sens: dict, report_status: dict,
                 data_val_clean) -> tuple[str, list[str]]:
    """系统可信等级（不是买入等级）：研究级/观察级/辅助交易级/实盘辅助级。

    规则（P0-10）：
    · 任何 P0 验证报告缺失，或数据验证存在差异/未验证 → 最高【观察级】；
    · 择时优势不设"2pp 达标线"，只统计四项证据的方向一致性；
    · 实盘辅助级要求：全部基础门槛通过 + 证据方向全部为正 + 双源验证干净
      + 敏感性无高风险项。
    """
    reasons: list[str] = []

    # ---- 基础门槛（不涉及择时优势） ----
    gates: list[tuple[bool, str]] = []
    gates.append((not snap.get("errors"),
                  "数据完整（无加载错误）" if not snap.get("errors")
                  else "存在数据加载错误"))
    has_abnormal = bool((snap.get("data_status") and any(
        s.get("ok") is False for s in snap["data_status"] if isinstance(s, dict))))
    gates.append((not has_abnormal, "关键数据源正常" if not has_abnormal
                  else "存在数据源异常（降级或不可达）"))
    samples = four.get("samples") or 0
    gates.append((samples >= 50, f"相似样本充足（{samples} ≥ 50）"
                  if samples >= 50 else f"相似样本不足（{samples} < 50）"))

    indep = full60.get("independent") or 0
    gates.append((indep >= 30, f"独立事件数 {indep} ≥ 30（h=60 去重后）"
                  if indep >= 30 else f"独立事件数仅 {indep} < 30（h=60 去重后）"
                  "——重叠预测点不能当独立样本"))
    ci_lo, ci_hi = full60.get("ci95_low"), full60.get("ci95_high")
    ci_w = (ci_hi - ci_lo) if (ci_lo is not None and ci_hi is not None) else None
    if ci_w is None:
        gates.append((False, "独立事件 95%CI 不可用"))
    elif ci_w <= 30:
        gates.append((True, f"独立事件 95%CI 宽度 {ci_w:.1f}pp ≤ 30pp"))
    else:
        gates.append((False, f"独立事件 95%CI 宽度过大（{ci_w:.1f}pp > 30pp）"))
    brier = full60.get("brier")
    naive = full60.get("naive_brier")
    bss = full60.get("brier_skill_score")
    # 校准判定（最终修正）：必须与 Naive Base-rate 基线（永远预测基础胜率）比较，
    # BSS > 0 才算优于基线；不再使用"Brier < 0.25 即可接受"的旧口径。
    if brier is None or naive is None:
        gates.append((False, "概率校准不可用（Model Brier / Naive 基线缺失）"))
    elif bss is not None and bss > 0:
        gates.append((True, f"概率校准优于基础率基线"
                            f"（Model Brier {brier} < Naive {naive}，BSS {bss:+.4f}）"))
    else:
        gates.append((False, f"模型校准未优于基础率基线"
                             f"（Model Brier {brier} ≥ Naive {naive}，BSS "
                             f"{bss if bss is not None else '—'}）"
                             "——概率展示不可当作可靠预测"))

    # ---- P0 报告齐备性门禁 ----
    missing = [k for k, v in report_status.items() if v == "缺失"]
    reports_ok = not missing
    if missing:
        reasons.append(f"✗ P0 验证报告缺失：{', '.join(missing)}——可信等级封顶【观察级】")
    else:
        reasons.append("✓ 四份 P0 验证报告齐备")
    if data_val_clean is False:
        reasons.append("✗ 数据双源验证存在差异/未验证项——可信等级封顶【观察级】")
    elif data_val_clean is True:
        reasons.append("✓ 数据双源验证全部一致")
    else:
        reasons.append("✗ 数据双源验证结果无法确认——可信等级封顶【观察级】")

    # ---- 择时优势证据（只看方向与数量，不设 pp 达标线） ----
    evid: list[tuple[str, str, bool]] = []   # (名称, 描述, 方向为正)
    up = four.get("D_uplift")
    evid.append(("决策口径 vs 基础率",
                 f"{up}pp" if up is not None else "暂无数据",
                 up is not None and up > 0))
    ob = full60.get("uplift_vs_base_pp")
    evid.append(("逐日OOS vs 同窗基准",
                 f"{ob}pp" if ob is not None else "暂无数据",
                 ob is not None and ob > 0))
    if full60.get("realized_win_rate_pct") is not None \
            and trend60.get("realized_win_rate_pct") is not None:
        g = full60["realized_win_rate_pct"] - trend60["realized_win_rate_pct"]
        evid.append(("完整模型 vs 趋势基准C",
                     f"{g:+.1f}pp", g > 0))
    b60 = (wf.get("buy_signal") or {}).get("n60") or {}
    a60 = (wf.get("all_blocks") or {}).get("n60") or {}
    if b60.get("win_rate_pct") is not None and a60.get("win_rate_pct") is not None:
        d = b60["win_rate_pct"] - a60["win_rate_pct"]
        evid.append(("Walk-Forward 信号 vs 基准",
                     f"{d:+.1f}pp（仅 {a60.get('samples')} 个不重叠块，"
                     "只能看方向）", d > 0))

    gates_ok = all(c for c, _ in gates)
    n_pos = sum(1 for _, _, ok in evid if ok)
    sens_risk = [i for i in sens.get("items", []) if i.get("flip_risk") == "高"]

    for ok, msg in gates:
        reasons.append(("✓ " if ok else "✗ ") + msg)
    for name, val, ok in evid:
        reasons.append(f"{'✓' if ok else '✗'} 证据[{name}]：{val}"
                       + ("（方向为正）" if ok else "（方向为负/缺失）"))

    # ---- 判定 ----
    if not gates_ok:
        return "研究级", reasons
    if not reports_ok or data_val_clean is not True:
        return "观察级", reasons
    if n_pos == len(evid) and len(evid) >= 4 and not sens_risk:
        return "辅助交易级", reasons + \
            ["注：四项证据方向一致为正且敏感性无高风险项；仍建议先小规模跟踪验证。"]
    if n_pos == len(evid) and len(evid) >= 4:
        return "观察级", reasons + \
            [f"注：敏感性存在高风险项（{', '.join(i['kind'] for i in sens_risk)}），封顶观察级。"]
    if n_pos >= 2:
        return "观察级", reasons + \
            ["注：择时优势证据方向不一致，目前没有充分证据证明模型具有稳定择时优势。"]
    return "研究级", reasons + \
        ["注：择时优势证据多数为负——高概率主要来自资产长期上涨特征。"]


# --------------------------------------------------------------------------
# 报告渲染
# --------------------------------------------------------------------------
def render_strategy_md(res: dict) -> str:
    if not res.get("ok"):
        return f"# 策略有效性验证报告\n\n无法执行：{res.get('message')}\n"
    L: list[str] = []
    f4 = res["four"]
    L.append("# 策略有效性验证报告（STRATEGY_VALIDATION_REPORT）")
    L.append("")
    L.append(f"生成时间：{res['ts']:%Y-%m-%d %H:%M:%S}")
    L.append("")
    L.append("> 回答唯一问题：**模型相对「什么都不看」的基准，有没有真正的择时增益？**")
    L.append("")
    L.append("**两套样本外验证体系严格分开（P0-3）：**")
    L.append("")
    L.append("- **体系 A【Block Walk-Forward】**：约 11 个不重叠测试块，"
             "块数少，只能看信号方向，不能证明高胜率；")
    L.append("- **体系 B【Daily Rolling OOS】**：逐日滚动预测数百点，"
             "但 60 日窗口高度重叠——独立事件数（去重后）才是有效样本量。")
    L.append("")
    L.append("首页默认采用**体系 B**。两者口径不同，数字不可互换、不可混用。")
    L.append("")

    # ---- 一、四概率（最终修正：仅一个决策主口径，其余为研究参考） ----
    L.append("## 一、概率口径（当前真实组合 · 未来 60 日）")
    L.append("")
    L.append("系统只用**一个**决策概率（最后一行）；其余仅作研究参考，不参与任何买卖判断。")
    L.append("")
    L.append("| 口径 | 含义 | 数值 | 用途 |")
    L.append("|---|---|---|---|")
    L.append(f"| 基础历史概率 | 不用任何择时信号，同窗口无条件胜率 | {_p(f4.get('A_base'))} | 研究参考（基准） |")
    L.append(f"| 相似状态历史频率 | 当前状态相似样本（kNN）胜率 | {_p(f4.get('B_cond'))} | 研究参考（不参与决策） |")
    L.append(f"| 重叠滚动OOS频率 | 近 1.5 年相似样本的样本外频率（重叠窗口） | {_p(f4.get('C_overlap'))} | 研究参考（不参与决策） |")
    L.append(f"| **保守独立事件OOS概率** | **独立事件去重后的逐日样本外** | "
             f"**{_p(f4.get('D_decision'))}** | **唯一决策主口径** |")
    L.append(f"| 相对基础率 | 决策口径 − 基础历史概率 | {f4.get('D_uplift')}pp | 择时优势证据 |")
    L.append("")
    L.append(f"决策口径独立事件 {f4.get('independent_events')} 个，"
             f"95%CI {_p(f4['ci'][0])}~{_p(f4['ci'][1])}；"
             f"相似样本 {f4.get('samples')}（研究参考）。")
    L.append("")
    L.append("> ⚠️ 以上均为【历史频率】统计，不等于模型的预测能力。"
             "92.0%（重叠滚动OOS）与 92.8%（相似状态频率）都只是研究参考，"
             "**绝不是**模型择时增益——决策只看独立事件去重样本外概率。")
    L.append("")

    # ---- 二、体系 B：Daily OOS（首页优先口径） ----
    L.append("## 二、体系 B：Daily Rolling OOS（首页默认口径，h=60）")
    L.append("")
    full60 = (res["full_oos"].get("horizons") or {}).get(60) or {}
    if full60:
        pts = full60.get("points") or 0
        indep = full60.get("independent") or 0
        L.append("| 指标 | 数值 | 说明 |")
        L.append("|---|---|---|")
        L.append(f"| 预测点数 | {pts} | 逐日滚动，窗口高度重叠 |")
        L.append(f"| 独立事件数 | {indep} | 间隔≥60 交易日、窗口不重叠（有效样本量）|")
        L.append(f"| 独立事件正收益数 | {full60.get('n_pos', '—')} | 实际上涨 |")
        L.append(f"| 独立事件负收益数 | {full60.get('n_neg', '—')} | 实际下跌 |")
        L.append(f"| 基准胜率（同窗口不看信号） | {_p(full60.get('base_win_rate_pct'))} | "
                 f"共 {full60.get('base_days', '—')} 个交易日 |")
        L.append(f"| 模型胜率（独立事件去重） | {_p(full60.get('realized_win_rate_pct'))} | "
                 "不是 92% 那类重叠点频率 |")
        L.append(f"| 提升 | {full60.get('uplift_vs_base_pp', '—')}pp | 模型 − 基准 |")
        L.append(f"| 95%CI（独立事件） | {_p(full60.get('ci95_low'))} ~ "
                 f"{_p(full60.get('ci95_high'))} | 二项置信区间 |")
        L.append(f"| Model Brier | {full60.get('brier')} | 模型逐点预测的 Brier |")
        L.append(f"| Naive Brier（基础率基线） | {full60.get('naive_brier')} | "
                 "永远预测基础胜率的模型——校准必须与它比较，不是与 0.25 比较 |")
        L.append(f"| Brier Skill Score | {full60.get('brier_skill_score')} | "
                 + ("正值 = 优于基础率基线" if (full60.get("brier_skill_score") or 0) > 0
                    else "**负值 = 模型校准未优于基础率基线**（概率展示不可当可靠预测）")
                 + " |")
        L.append(f"| 平均预测 | {_p(full60.get('avg_pred_pct'))} | 与实际胜率对比看偏差 |")
        L.append("")
        if indep and pts:
            L.append(f"> 重叠警示：{pts} 个逐日预测点中，60 日窗口互相重叠，"
                     f"**不能当成 {pts} 个独立样本**；独立事件仅 {indep} 个"
                     f"（约 {pts / max(indep, 1):.0f}:1 重叠）。"
                     "上面的胜率与 CI 全部按独立事件计算。")
        ob = full60.get("uplift_vs_base_pp")
        if ob is None or ob <= 0:
            L.append("")
            L.append("**⚠️ 逐日样本外模型相对基准没有正提升：目前没有证据证明"
                     "模型具有额外择时优势。**高概率主要来自组合本身长期上涨特征。")
        L.append("")
        if full60.get("distance_mult_avg") is not None:
            L.append(f"相似距离阈值平均使用 {full60.get('distance_mult_avg')}×"
                     "（**硬上限 3.0×**：超过 3× 已禁止自动放宽；"
                     "3× 以内样本不足时按『样本不足/UNKNOWN』处理，"
                     "不会为生成概率强行使用更远的历史）。")
            L.append("")
        L.append("### 概率校准（独立事件，与 Naive 基线比较）")
        L.append("")
        _cal_v = full60.get("calibration_verdict")
        if _cal_v:
            L.append(f"**校准结论：{_cal_v}**")
            if full60.get("brier_skill_score") is not None \
                    and full60["brier_skill_score"] <= 0:
                L.append("")
                L.append("**⚠️ 模型校准未优于基础率基线**：Naive Base-rate 模型"
                         "（永远预测基础胜率，什么都不算）比这套相似状态模型的 Brier "
                         "还低。展示的概率不能当作可靠预测，也不能宣称『校准可接受』"
                         "（旧的 Brier<0.25 口径已废弃）。")
            L.append("")
        L.append("| 预测概率区间 | 独立样本 | 平均预测 | 实际上涨频率 |")
        L.append("|---|---|---|---|")
        for b in full60.get("buckets", []):
            L.append(f"| {b['bucket']} | {b['samples']} | {b['avg_pred_pct']}% | "
                     f"{b['realized_win_rate_pct']}% |")
        L.append("")
        L.append("读法：预测 70% 的那批日子，实际是否约 70% 上涨。"
                 "系统性偏差大（>10pp）时该桶概率不可靠。")
        _gap = ((full60.get("avg_pred_pct") or 0)
                - (full60.get("realized_win_rate_pct") or 0))
        if full60.get("avg_pred_pct") is not None and _gap > 5:
            L.append("")
            L.append(f"**⚠️ 概率明显偏高：平均预测 {full60.get('avg_pred_pct')}%，"
                     f"实际只上涨 {full60.get('realized_win_rate_pct')}%"
                     f"（偏高 {_gap:.1f}pp）。展示的概率应整体打折理解，"
                     "不能把 90%+ 当成可靠买入概率。**")
    else:
        L.append("样本不足，无法生成逐日 OOS 校准。")
    L.append("")

    # ---- 三、体系 B 其他周期 ----
    L.append("## 三、体系 B 其他周期（Daily OOS）")
    L.append("")
    L.append("| 周期 | 预测点 | 独立事件 | 基准胜率 | 模型胜率 | 提升 | 95%CI |")
    L.append("|---|---|---|---|---|---|---|")
    for h, d in (res["full_oos"].get("horizons") or {}).items():
        if d:
            L.append(f"| {h}日 | {d.get('points')} | {d.get('independent')} | "
                     f"{_p(d.get('base_win_rate_pct'))} | "
                     f"{_p(d.get('realized_win_rate_pct'))} | "
                     f"{d.get('uplift_vs_base_pp', '—')}pp | "
                     f"{_p(d.get('ci95_low'))}~{_p(d.get('ci95_high'))} |")
    L.append("")

    # ---- 四、体系 B：趋势基准对比 ----
    t60 = (res["trend_oos"].get("horizons") or {}).get(60) or {}
    if t60 and full60:
        L.append("## 四、体系 B：完整模型 vs 趋势基准C（同为逐日 OOS，h=60）")
        L.append("")
        L.append("| 口径 | 独立事件 | 实际胜率 | 平均预测 | Brier |")
        L.append("|---|---|---|---|---|")
        L.append(f"| 完整模型（13 特征） | {full60.get('independent')} | "
                 f"{full60.get('realized_win_rate_pct')}% | {full60.get('avg_pred_pct')}% | "
                 f"{full60.get('brier')} |")
        L.append(f"| 趋势基准C（3 特征） | {t60.get('independent')} | "
                 f"{t60.get('realized_win_rate_pct')}% | {t60.get('avg_pred_pct')}% | "
                 f"{t60.get('brier')} |")
        L.append("")
        gain = (full60.get("realized_win_rate_pct") or 0) \
            - (t60.get("realized_win_rate_pct") or 0)
        L.append(f"完整模型 − 趋势基准：**{gain:+.1f}pp**。"
                 + ("多出的宏观/摆动特征有正增量。" if gain > 0 else
                    "**完整模型相对纯趋势没有优势**——多出的特征没有换来区分度。"))
        L.append("")

    # ---- 五、体系 A：Block Walk-Forward ----
    wf = res.get("walk_forward") or {}
    b60 = (wf.get("buy_signal") or {}).get("n60") or {}
    a60 = (wf.get("all_blocks") or {}).get("n60") or {}
    L.append("## 五、体系 A：Block Walk-Forward（与体系 B 分开，不得混用）")
    L.append("")
    if b60:
        L.append(f"- 不重叠测试块共 {a60.get('samples') or '—'} 个"
                 "（块数少，只能看方向，不能证明高胜率）：")
        L.append(f"- 基准（不看信号）60 日胜率：{_p(a60.get('win_rate_pct'))}"
                 f"（样本 {a60.get('samples')}）")
        L.append(f"- 买入信号（S/A/B）60 日胜率：**{_p(b60.get('win_rate_pct'))}**"
                 f"（样本 {b60.get('samples')}）")
        if a60.get("win_rate_pct") is not None and b60.get("win_rate_pct") is not None:
            diff = b60["win_rate_pct"] - a60["win_rate_pct"]
            L.append(f"- **信号相对基准提升：{diff:+.1f}pp**"
                     + ("（无区分度）" if diff <= 0 else "（方向为正，"
                        f"但仅 {a60['samples']} 块，统计意义有限）"))
    else:
        L.append("Walk-Forward 结果不可用。")
    L.append("")

    # ---- 六、敏感性 ----
    L.append("## 六、敏感性检查（P0-10）")
    L.append("")
    L.append("| 类型 | 对象 | 结论 | 翻转风险 |")
    L.append("|---|---|---|---|")
    for it in (res.get("sensitivity", {}).get("items") or []):
        L.append(f"| {it.get('kind')} | {it.get('target')} | "
                 f"{it.get('detail')} | {it.get('flip_risk')} |")
    L.append("")
    L.append("> 注：成本敏感性为解析估算（不重跑回测）；"
             "未做全参数网格搜索，这是当前版本的已知局限。")
    L.append("")

    # ---- 七、P0 报告齐备性 ----
    L.append("## 七、P0 验证报告齐备性")
    L.append("")
    for k, v in (res.get("report_status") or {}).items():
        L.append(f"- {k}：{v}")
    L.append(f"- 数据双源验证："
             + ("全部一致" if res.get("data_val_clean") is True else
                "存在差异/未验证项" if res.get("data_val_clean") is False
                else "无法确认"))
    L.append("")

    # ---- 八、可信等级 ----
    L.append("## 八、系统可信等级（不是买入建议等级）")
    L.append("")
    L.append(f"**{res['trust_level']}**")
    L.append("")
    for r in res.get("trust_reasons", []):
        L.append(f"- {r}")
    L.append("")

    # ---- 九、诚实结论 ----
    L.append("## 九、诚实结论")
    L.append("")
    ob = full60.get("uplift_vs_base_pp") if full60 else None
    if ob is None or ob <= 0:
        L.append("1. 当前版本**只能作为研究和辅助工具**：逐日样本外模型相对基准"
                 "没有正提升，不能把显示的高概率当作择时能力。")
    else:
        L.append("1. 逐日样本外相对基准有正提升，但独立事件有限，"
                 "只能作为辅助交易参考，继续积累样本。")
    L.append("2. 92.0%（重叠滚动OOS频率）与 92.8%（相似状态历史频率）都只是"
             "**历史条件频率**，只作研究参考、不参与任何决策，不代表未来，"
             "也不代表预测能力；唯一决策口径是独立事件去重样本外概率，"
             "其独立事件数与 95%CI 才是判断可信度的依据。")
    L.append("3. 所有判断以【当前真实组合】口径为准，最终战略组合只作参照；"
             "WAIT 是正常且经常正确的答案。")
    L.append("")
    return "\n".join(L)


def _p(v, nd: int = 1) -> str:
    return "暂无数据" if v is None else f"{v:.{nd}f}%"


def run_and_write(cfg=None, progress=None) -> tuple[dict, Path]:
    from .config import project_root
    res = run_strategy_validation(cfg, progress)
    out = project_root() / "STRATEGY_VALIDATION_REPORT.md"
    out.write_text(render_strategy_md(res), encoding="utf-8")
    # 【BUY PERMISSION GATE】把本次可信等级持久化，供运行期决策读取。
    try:
        if res.get("ok"):
            save_trust_level(cfg, res)
    except Exception:  # noqa: BLE001
        pass
    return res, out


# --------------------------------------------------------------------------
# 可信等级持久化（运行期 BUY PERMISSION GATE 的唯一权威来源）
#   可信等级决定“买入权限”，不是买入等级：研究级不开放实际买入建议。
# --------------------------------------------------------------------------
TRUST_CACHE_NAME = "strategy_trust.json"
TRUST_LEVELS = ("研究级", "观察级", "辅助交易级", "实盘辅助级")


def trust_cache_path(cfg=None) -> Path:
    """可信等级缓存路径（与 config/data 同级，随交付包一起移动）。"""
    if cfg is not None:
        try:
            return Path(cfg.path).resolve().parent.parent / "data" / TRUST_CACHE_NAME
        except Exception:  # noqa: BLE001
            pass
    from .config import project_root
    return project_root() / "data" / TRUST_CACHE_NAME


def save_trust_level(cfg, res: dict) -> Path:
    """持久化本次 --validate-strategy 的可信等级与关键证据。"""
    p = trust_cache_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    four = res.get("four") or {}
    full = res.get("full_oos") or {}
    payload = {
        "trust_level": res.get("trust_level") or "研究级",
        "computed_at": (res.get("ts") or datetime.now()).strftime("%Y-%m-%d %H:%M:%S"),
        "independent_events": full.get("independent"),
        "lift_pp": four.get("D_uplift"),
        "brier_skill_score": full.get("brier_skill_score"),
        "note": ("由 --validate-strategy 生成；运行期据此判定买入权限"
                 "（可信等级决定买入权限，不是买入等级）"),
    }
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def load_trust_level(cfg=None) -> dict:
    """读取最近一次策略验证的可信等级；缺失/损坏 → 按最保守的【研究级】处理。"""
    p = trust_cache_path(cfg)
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(d, dict):
            raise ValueError("bad payload")
        lvl = str(d.get("trust_level") or "研究级")
        if lvl not in TRUST_LEVELS:
            lvl = "研究级"
        d["trust_level"] = lvl
        d.setdefault("source", f"persisted:{p.name}")
        return d
    except Exception:  # noqa: BLE001
        return {
            "trust_level": "研究级",
            "computed_at": None,
            "source": "default:未运行 --validate-strategy（按最保守处理）",
            "note": "缺失策略验证结果 → 默认研究级，不开放买入权限",
        }
