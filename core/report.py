"""把快照渲染成"今日怎么操作"的中文文本报告（命令行 / 日志导出共用）。

文案规则：
    · 全部使用 GBK 安全字符（√ × ※ 等），避免中文 Windows 控制台
      cp936 编码报错；emoji 只出现在 GUI。
    · 先回答"要不要买 / 买什么 / 买多少 / 下一笔什么时候买"，再给指标细节。
"""
from __future__ import annotations


def _pct(v, nd: int = 2, sign: bool = True) -> str:
    if v is None:
        return "暂无数据"
    return f"{v:+.{nd}f}%" if sign else f"{v:.{nd}f}%"


def _n(v, nd: int = 2, suffix: str = "") -> str:
    return "暂无数据" if v is None else f"{v:.{nd}f}{suffix}"


def _num(v, nd: int = 1) -> str:
    return "暂无数据" if v is None else f"{v:.{nd}f}"


def _money(v) -> str:
    return "暂无数据" if v is None else f"{v:,.0f} 元"


def _sample_gate_note(snap: dict) -> list[str]:
    """说明「单只 ETF 相似样本门禁」的真实状态：刻意保守，不是故障。

    背景：为使概率诚实，系统同时启用「相似距离硬上限 3× 不自动放宽」与
    「相似样本 ≥min_samples」。两者叠加后，单只 ETF 常拿不到足够样本 →
    买入质量一律判「样本不足」→ 系统在当前口径下通常不会给出股票买入建议。
    这段文字的目的就是让读者知道这是设计而非 bug。
    """
    try:
        from .config import get_config
        cfg = get_config()
        need = int(cfg.get("hist.min_samples", 50))
        cap = float(cfg.get("hist.distance_cap_mult", 3.0))
    except Exception:  # noqa: BLE001
        need, cap = 50, 3.0
    pairs: list[tuple[str, int]] = []
    for r in (snap.get("rows") or []):
        h60 = ((r.get("hist") or {}).get(60) or {})
        n = h60.get("samples")
        if h60 and n is not None:
            pairs.append((str((r.get("asset") or {}).get("name") or "?")[:10], int(n)))
    if not pairs:
        return []
    short = [(nm, n) for nm, n in pairs if n < need]
    if not short:
        return []
    detail = "、".join(f"{nm} {n} 个" for nm, n in pairs)
    return [
        "【样本门禁说明（刻意保守，不是故障）】",
        f"  单只 ETF 买入评级前提：相似样本 ≥{need} 个，且相似距离 ≤{cap:.1f}×"
        "（超出不再自动放宽，宁缺毋滥）。",
        f"  当前各 ETF 可用样本：{detail}。",
        f"  其中 {len(short)}/{len(pairs)} 只低于 {need} 个 → 买入质量只能判"
        "「样本不足」→ 本系统在当前口径下通常不会产生股票 ETF 的买入建议。",
        "  这是刻意设定：宁可今天不出买入信号，也不用更远距离的历史样本凑数。",
    ]


def render_daily(snap: dict) -> str:
    if not snap.get("ok"):
        return "【数据不足】\n" + "\n".join(snap.get("warnings", []))

    p = snap["portfolio"]
    w = p["weights"]
    cls = w["by_class"]
    tgt = p.get("target_class_with_cash") or {**p["target_class"], "cash": 0.0}
    risk, q, entry, dec = p["risk"], p["quality"], p["entry"], p["decision"]
    gold, reb, hist, opts = p["gold_control"], p["rebalance"], p["hist"], p["optimizer"]
    pk = p.get("plan") or {}
    gate = pk.get("gate") or {}
    plan = pk.get("plan") or {}
    three = pk.get("three") or {}
    today = pk.get("today") or {}
    triggers = pk.get("triggers") or {}
    batches = pk.get("batches") or []
    prob = pk.get("prob") or {}

    L: list[str] = []
    hl = (pk.get("home") or {}).get("headline") or {}

    # ================= 第一屏：只看这一块 =================
    L.append("=" * 62)
    L.append(f"我的全天候 ETF 组合   [{p.get('portfolio_name') or '当前组合'} / "
             f"{p.get('portfolio_id') or '-'}]")
    L.append(f"数据时间：{snap['ts']:%Y-%m-%d %H:%M:%S}")
    L.append("")
    if hl:
        L.append("*" * 62)
        L.append(f"  今天怎么做 ：{hl.get('answer') or '等待'}")
        L.append(f"  {hl.get('big') or ''}")
        L.append("-" * 62)
        L.append(f"  今天投入    ：{_money(hl.get('amount')) if (hl.get('amount') or 0) > 0 else '0 元（今天不投入）'}")
        L.append(f"  买哪只      ：{hl.get('name') or '不买'}"
                 + (f"（{hl.get('class_label')}）" if hl.get("class_label") else ""))
        # ---- 执行台账：本程序没有券商接口，只有你点界面按钮/改持仓后才会知道已执行 ----
        try:
            from . import execution_ledger as _led
            _st = _led.status_text(hl, pk.get("rebal_sim") or {})
            L.append(f"  执行台账    ：{_st.get('label')}")
        except Exception:  # noqa: BLE001
            pass
        # ---- 价格口径：判断用日线收盘价；盘中参考价只作显示、不参与判断 ----
        try:
            from . import live_holdings as _lh
            L.append(f"  价格口径    ：{_lh.price_caption(snap)}")
        except Exception:  # noqa: BLE001
            pass
        # ---- 【BUY PERMISSION GATE】可信等级决定买入权限（最高优先级，常显）----
        _bp = hl.get("buy_permission") or {}
        if _bp:
            _lvl = _bp.get("trust_level") or "研究级"
            if _bp.get("allowed"):
                L.append(f"  买入权限    ：已开放（可信等级 {_lvl} ≥ 门槛 "
                         f"{_bp.get('min_level')}）")
            else:
                L.append(f"  买入权限    ：关闭（可信等级 {_lvl} < 门槛 "
                         f"{_bp.get('min_level')}）—— 今天不执行实际买入")
        if hl.get("permission_blocked"):
            _rr = hl.get("research_reference")
            L.append("-" * 62)
            if _rr:
                L.append(f"    ※ 研究参考：模型模拟买入 {_rr.get('name')} "
                         f"{int(_rr.get('amount') or 0):,} 元后组合质量最高"
                         " —— 仅供研究，不转换成实际买入。")
            L.append(f"    ※ {_bp.get('trust_level') or '研究级'}："
                     "当前不开放买入权限，因此今天不执行。")
        cons = hl.get("conservative") or {}
        _ci = (cons.get("ci") or hl.get("prob60_ci") or [])
        _ci_s = (f"，95%CI {_num(_ci[0], 1)}%~{_num(_ci[1], 1)}%"
                 if len(_ci) == 2 and _ci[0] is not None else "")
        if cons.get("prob") is not None:
            # ---- 唯一决策概率（最终修正）：独立事件去重后的逐日样本外 ----
            L.append(f"  组合未来60日历史概率：{_num(cons.get('prob'), 1)}%"
                     "（独立事件去重样本外 ← 决策主口径）")
            L.append(f"    独立样本外事件：{cons.get('indep')}"
                     + (f"（逐日预测点 {cons.get('points')} 个，"
                        "60日窗口重叠、不可当独立样本）" if cons.get("points") else ""))
            if len(_ci) == 2 and _ci[0] is not None:
                L.append(f"    95%CI：{_num(_ci[0], 1)}% ~ {_num(_ci[1], 1)}%")
            if cons.get("base") is not None:
                L.append(f"    无条件基础胜率：{_num(cons.get('base'), 1)}%"
                         "（不用任何信号的同窗口基准）")
            if cons.get("uplift") is not None:
                L.append(f"    模型相对基准：{float(cons['uplift']):+.1f}pp"
                         "（模型 − 基准）")
            if cons.get("overlap_freq") is not None:
                L.append(f"    重叠滚动OOS频率：{_num(cons.get('overlap_freq'), 1)}%"
                         "（研究参考，不参与决策）")
            if cons.get("few_samples"):
                L.append("    ※ 独立样本外事件不足 30 个：样本极少，仅供研究。")
            if cons.get("no_edge"):
                L.append("    ※ 当前模型没有证明额外择时优势"
                         "（模型胜率未高于同窗口无条件基准）。")
            if cons.get("calibration"):
                L.append(f"    概率校准：{cons.get('calibration')}")
            L.append(f"  相似状态历史频率（研究参考，不参与决策）："
                     f"30日 {_num(hl.get('prob30'), 1)}%"
                     f" · 60日 {_num(hl.get('prob60'), 1)}%"
                     f" · 90日 {_num(hl.get('prob90'), 1)}%")
        else:
            _base = hl.get("prob60_base")
            _up = hl.get("prob60_uplift")
            L.append(f"  相似状态历史频率（研究参考）：30日 {_num(hl.get('prob30'), 1)}%"
                     + (f"（重叠滚动OOS {_num(hl.get('prob30_oos'), 1)}%）"
                        if hl.get("prob30_oos") is not None else ""))
            L.append(f"               60日 {_num(hl.get('prob60'), 1)}%"
                     + (f"（重叠滚动OOS {_num(hl.get('prob60_oos'), 1)}%，"
                        f"其中不用任何信号的基础胜率 {_num(_base, 1)}%，"
                        f"相对基准 {float(_up):+.1f}pp{_ci_s}）"
                        if hl.get("prob60_oos") is not None and _base is not None
                        else (f"（重叠滚动OOS {_num(hl.get('prob60_oos'), 1)}%"
                              f"{_ci_s}）"
                              if hl.get("prob60_oos") is not None or _ci_s else "")))
            L.append("               （决策主口径的独立事件去重样本外概率尚不可用；"
                     "以上仅为研究参考频率，不作为决策依据）")
            L.append(f"               90日 {_num(hl.get('prob90'), 1)}%"
                     + (f"（重叠滚动OOS {_num(hl.get('prob90_oos'), 1)}%）"
                        if hl.get("prob90_oos") is not None else ""))
        if hl.get("note"):
            L.append(f"  ※ {hl.get('note')}")
        L.append("-" * 62)
        L.append("  为什么（最多 3 条）：")
        for _line in (hl.get("why") or [])[:3]:
            L.append("   · " + str(_line))
        L.append(f"  下一次      ：{hl.get('next') or '—'}")
        L.append("*" * 62)
        L.append("")
        L.append("（下面开始是支撑上面的详细数据；时间紧张时看完上面即可。）")
        L.append("=" * 62)
    else:
        L.append("=" * 62)
    L.append(f"总资产  ：{_money(w.get('total_assets'))}"
             f"（ETF 市值 {_money(w.get('etf_value'))} + 待投资现金 {_money(w.get('cash_value'))}）")
    L.append(f"口径说明：{p['weights_source']}")
    L.append("")

    # ---------------- 今天到底怎么操作 ----------------
    L.append("-" * 62)
    L.append("【今天到底怎么操作？】")
    L.append(f"  {today.get('text', '暂无数据')}")
    L.append(f"  {today.get('sub', '')}")
    L.append("")
    L.append("  · 今天是否投入新增资金？ " + str(three.get("new_money_text")))
    if three.get("new_money_note"):
        L.append("      ↳ " + str(three.get("new_money_note")))
    L.append("  · 今天是否需要再平衡？   " + str(three.get("rebalance_text")))
    reduce_txt = str(three.get("reduce_text"))
    if three.get("reduce_targets"):
        reduce_txt += "（" + "、".join(three["reduce_targets"]) + "）"
    L.append("  · 今天是否需要降低超配资产？ " + reduce_txt)

    # ---------------- 组合未来概率 ----------------
    L.append("")
    L.append("-" * 62)
    L.append("【整个组合未来历史盈利概率】")
    L.append(f"  未来  20 日：{_num(prob.get('p20'), 1)}%")
    L.append(f"  未来  30 日：{_num(prob.get('p30'), 1)}%"
             + (f"（重叠滚动OOS {_num(prob.get('oos30'), 1)}%，研究参考）"
                if prob.get("oos30") is not None else ""))
    L.append(f"  未来  60 日：{_num(prob.get('p60'), 1)}%   ← 相似状态历史频率（研究参考）")
    L.append(f"  未来  90 日：{_num(prob.get('p90'), 1)}%"
             + (f"（重叠滚动OOS {_num(prob.get('oos90'), 1)}%，研究参考）"
                if prob.get("oos90") is not None else ""))
    L.append(f"  未来 120 日：{_num(prob.get('p120'), 1)}%")
    cons = hl.get("conservative") or {}
    if cons.get("prob") is not None:
        L.append(f"  决策主口径（独立事件去重样本外60日）：{_num(cons.get('prob'), 1)}%"
                 f"，独立事件 {cons.get('indep')} 个"
                 + (f"，95%CI {cons['ci'][0]}%~{cons['ci'][1]}%" if cons.get("ci")
                    and cons["ci"][0] is not None else "")
                 + "（统一用于首页/决策/金额/等级）")
        if cons.get("overlap_freq") is not None:
            L.append(f"  重叠滚动OOS频率：{_num(cons.get('overlap_freq'), 1)}%"
                     "（研究参考，不是独立事件口径）")
    else:
        L.append(f"  重叠滚动OOS频率：{_num(prob.get('oos60'), 1)}%"
                 f"{'（研究参考）' if prob.get('oos60') is not None else '（尚不可用）'}")
    L.append(f"  历史相似样本：{prob.get('samples60')} 次"
             f"（其中重叠滚动OOS {prob.get('oos_samples60')} 次）")
    L.append(f"  95% 置信区间（独立事件去重样本外口径）：{prob.get('ci95_low')}% ~ {prob.get('ci95_high')}%")
    L.append(f"  样本可信度  ：{prob.get('grade')}")
    L.append(f"  未来60日期望收益：{_pct(gate.get('exp60'))}   盈亏比 {gate.get('payoff')}")
    L.append(f"  未来60日最大回撤：{_pct(gate.get('dd60'))}")
    L.append("  说明：决策概率一律为【独立事件去重样本外】口径；相似状态频率与"
             "重叠滚动OOS频率均只作研究参考，不代表未来实际收益。")
    tgt_sim = (pk.get("home") or {}).get("strategic_target") or {}
    if tgt_sim:
        L.append(f"  （参照）最终战略目标组合（债券50%/股票45%/黄金5%/现金0）："
                 f"60日概率 {_num(tgt_sim.get('prob60'), 1)}%，"
                 f"期望 {_pct(tgt_sim.get('exp60'))}，"
                 f"1年回撤 {_pct(tgt_sim.get('max_dd_1y'))}，"
                 f"质量 {_num(tgt_sim.get('quality'))}"
                 " —— 仅作参照，绝不用于回答「今天该不该买」。")

    # ---------------- 仓位 ----------------
    L.append("")
    L.append("-" * 62)
    L.append("【当前实际仓位】")
    L.append(f"  债券 {_pct((cls.get('bond') or 0) * 100, sign=False)}   "
             f"股票 {_pct((cls.get('stock') or 0) * 100, sign=False)}   "
             f"黄金 {_pct((cls.get('gold') or 0) * 100, sign=False)}   "
             f"现金/待投资 {_pct((cls.get('cash') or 0) * 100, sign=False)}")
    L.append("【战略目标】")
    L.append(f"  债券 {_pct((tgt.get('bond') or 0) * 100, sign=False)}   "
             f"股票 {_pct((tgt.get('stock') or 0) * 100, sign=False)}   "
             f"黄金 {_pct((tgt.get('gold') or 0) * 100, sign=False)}   "
             f"现金 {_pct((tgt.get('cash') or 0) * 100, sign=False)}")
    # 组合完成度（§42）：只是进度指标，完成度低不等于要强制买入
    _gaps = [(tgt.get(k) or 0) * 100 - (cls.get(k) or 0) * 100
             for k in ("bond", "stock", "gold")]
    _completion = max(0.0, 100.0 - sum(abs(g) for g in _gaps) / 2.0)
    L.append(f"  组合完成度：{_completion:.0f}%（离 50/45/5 目标的加权距离；"
             "只是进度，不构成强制买入理由）")

    gold_now = (cls.get("gold") or 0) * 100
    stock_now = (cls.get("stock") or 0) * 100
    bond_now = (cls.get("bond") or 0) * 100
    L.append("")
    L.append("【当前最重要的问题】")
    if gold_now > (tgt.get("gold") or 0) * 100 + 1e-6:
        over_pp = gold_now - (tgt.get("gold") or 0) * 100
        L.append(f"  黄金超配：{over_pp:+.1f} 个百分点"
                 f"（对应金额约 {_money((cls.get('gold') or 0) * (w.get('total_assets') or 0) - (tgt.get('gold') or 0) * (w.get('total_assets') or 0))}）")
    if stock_now < (tgt.get("stock") or 0) * 100 - 1e-6:
        L.append(f"  股票低配：{stock_now - (tgt.get('stock') or 0) * 100:+.1f} 个百分点")
    if bond_now < (tgt.get("bond") or 0) * 100 - 1e-6:
        L.append(f"  债券低配：{bond_now - (tgt.get('bond') or 0) * 100:+.1f} 个百分点")
    L.append(f"  待投资资金：{_money(w.get('cash_value'))}"
             f"（占总资产 {_n(w.get('cash_pct'), 1, '%')}）")

    # ---------------- 今天买什么 / 买多少 ----------------
    L.append("")
    L.append("-" * 62)
    L.append("【今天最值得配置】")
    picks = plan.get("picks") or []
    _rps = plan.get("research_picks") or []
    if picks:
        for i in picks:
            L.append(f"  · {i['name']}（买入质量 {i['buy_quality']}）")
            L.append(f"      目标仓位 {i['target_pct']}% / 当前 {i['current_pct']}%"
                     f" → 缺口 {i['gap_pct']:+.2f}pp（{_money(i['gap_amount'])}）")
            L.append(f"      本批金额 {_money(i['first_batch_amount'])}"
                     f"，剩余等待 {_money(i['remaining_wait_amount'])}"
                     f"（共 {plan.get('batch_count')} 批）")
    elif plan.get("permission_blocked") and _rps:
        # 【BUY PERMISSION GATE】买入权限关闭：金额仅作研究参考，不转换成实际买入
        L.append("  研究参考（今天不执行）：模型模拟出的最优方案为")
        for i in _rps:
            L.append(f"    · {i['name']} {_money(i.get('first_batch_amount'))}"
                     " —— 仅研究参考，不转换成实际买入")
    else:
        total_first = plan.get("total_first_batch_amount") or 0
        if total_first <= 0:
            L.append("  暂无 → 本次第一批金额为 0 元（WAIT 是合法结论）")
    L.append(f"  本次合计投入：{_money(plan.get('total_first_batch_amount'))}"
             f"（可投资现金 {_money(plan.get('deployable_cash'))}）")
    L.append(f"  注：{plan.get('note')}")

    # ---- 样本门禁口径说明（刻意保守，非故障）----
    _sg = _sample_gate_note(snap)
    if _sg:
        L.append("")
        L.extend(_sg)

    # ---------------- 交易前后组合对比（§50/§51） ----------------
    cmp = pk.get("comparison") or {}
    if cmp.get("available"):
        b = cmp.get("before") or {}
        a = cmp.get("after") or {}
        trade = cmp.get("trade")
        imp = cmp.get("improve") or {}
        L.append("")
        L.append("【今天买不买？交易前后组合对比】")

        def _delta(key, nd=2, suffix="%"):
            if imp.get(key) is None:
                return "暂无数据"
            v = imp.get(key)
            return f"{v:+.{nd}f}{suffix}"

        def _row(label, key, nd=2, suffix="%"):
            bv = _n(b.get(key), nd, suffix)
            if not trade:
                return f"  · {label}：什么都不买 {bv}"
            av = _n(a.get(key), nd, suffix)
            return f"  · {label}：什么都不买 {bv}  →  执行建议后 {av}（{_delta(key, nd, suffix)}）"

        if trade:
            label = "建议交易" if cmp.get("executable") else "理论最优交易（今天不执行）"
            L.append(f"  {label}：买入 {trade.get('name')} "
                     f"{_money(trade.get('amount'))}"
                     f"（{trade.get('class_label') or ''}，边际改善分 "
                     f"{_num(trade.get('marginal_score'))}"
                     + ("，已按分批纪律封顶" if trade.get("capped") else "") + "）")
        else:
            L.append("  建议交易：无（今天任何买入金额都不能让整个组合变得更好）")
        _dp = hl.get("dec_prob") or {}
        if _dp.get("prob_used") is not None:
            _dci = _dp.get("ci95") or [None, None]
            L.append(f"  · 决策主口径（独立事件去重样本外60日）："
                     f"{_num(_dp.get('prob_used'), 1)}%"
                     f"，独立事件 {_dp.get('independent_events')} 个"
                     + (f"，95%CI {_num(_dci[0], 1)}%~{_num(_dci[1], 1)}%"
                        if _dci[0] is not None else "")
                     + f"，相对基础率 {_num(_dp.get('base_rate'), 1)}% 为 "
                     f"{float(_dp.get('lift_pp') or 0):+.1f}pp")
        # 交易前后"组合概率"主展示：决策主口径（不是相似状态频率）
        _bt = cmp.get("before_text") or {}
        _at = cmp.get("after_text") or {}
        if _bt.get("prob60_dec") is not None:
            _bdec = _n(_bt.get("prob60_dec"), 1, "%")
            if trade and _at.get("prob60_dec") is not None:
                _adec = _n(_at.get("prob60_dec"), 1, "%")
                _dd = float(_at["prob60_dec"]) - float(_bt["prob60_dec"])
                L.append(f"  · 组合60日概率（决策主口径）：什么都不买 {_bdec}"
                         f"  →  执行建议后 {_adec}（{_dd:+.1f}pp，"
                         "交易后为按模拟相对变化换算的估算）")
            else:
                L.append(f"  · 组合60日概率（决策主口径）：{_bdec}"
                         "（独立事件去重样本外）")
        L.append(_row("相似状态历史频率（研究参考，不参与决策）", "prob60", 1, "%"))
        _bci = (cmp.get("before_text") or {}).get("prob60_ci") or []
        _aci = (cmp.get("after_text") or {}).get("prob60_ci") or []
        if len(_bci) == 2 and len(_aci) == 2 and _bci[0] is not None \
                and _aci[0] is not None:
            L.append(f"  · 研究参考60日概率95%CI：什么都不买 {_bci[0]:.1f}%~{_bci[1]:.1f}%"
                     f"  →  执行建议后 {_aci[0]:.1f}%~{_aci[1]:.1f}%")
        L.append(_row("组合120日盈利概率", "prob120", 1, "%"))
        L.append(_row("组合60日期望收益", "exp60", 2, "%"))
        L.append(_row("组合1年最大回撤", "max_dd_1y", 2, "%"))
        L.append(_row("组合年化波动", "vol_annual", 2, "%"))
        L.append(_row("组合质量评分", "quality", 1, ""))
        L.append(f"  结论：{cmp.get('verdict')}。{cmp.get('verdict_detail') or ''}")

    L.append("")
    L.append("【四批资金进度】")
    for b in batches:
        mark = "√" if b.get("done") else "×"
        L.append(f"  第 {b['batch']} 批（{b['pct']}%）：{mark} {b['status']}"
                 f"   条件：{b['condition']}")

    L.append("")
    L.append("【下一次买入触发条件】")
    for c in triggers.get("portfolio_conditions", []):
        L.append(f"  [{'√' if c.get('ok') else '×'}] {c['name']}"
                 f"（当前：{c.get('now')}）")
    for t in triggers.get("etf_triggers", []):
        L.append(f"  · {t['name']}：{t['trigger']}")
    if any((not c.get("ok")) and "买入质量≥B" in (c.get("name") or "")
           for c in triggers.get("portfolio_conditions", [])):
        L.append("  ※ 其中「买入质量≥B」在当前样本门禁下几乎不可能满足——"
                 "原因见上方【样本门禁说明】，属刻意保守，不是程序故障。")

    # ---------------- 不要追涨 / 不要抄底 ----------------
    L.append("")
    L.append("-" * 62)
    L.append("【今天不要追涨】" + ("、".join(dec["no_chase"]) if dec["no_chase"] else "无"))
    L.append("【今天不要抄底】"
             + ("、".join(dec["no_bottom_fishing"]) if dec["no_bottom_fishing"] else "无"))

    # ---------------- 为什么不能买 ----------------
    why = pk.get("why_not") or []
    if why and not (three.get("new_money")):
        L.append("")
        L.append("【为什么今天不能（大量）买入】")
        for i, x in enumerate(why, 1):
            L.append(f"  {i}. {x}")

    # ---------------- ETF 表格 ----------------
    L.append("")
    L.append("-" * 62)
    L.append("【ETF 明细】")
    L.append(f"  {'ETF':<14}{'当前':>7}{'目标':>7}{'缺口':>7}{'60日频率':>9}"
             f"{'60日期望':>9}{'位置':>6}{'买入质量':>9}{'今天动作':>12}{'第一批':>10}")
    for i in plan.get("items", []):
        L.append(f"  {str(i['name'])[:12]:<14}"
                 f"{_n(i['current_pct'], 1, '%'):>7}{_n(i['target_pct'], 0, '%'):>7}"
                 f"{_pct(i['gap_pct']):>7}{_n(i['prob60'], 1, '%'):>9}"
                 f"{_pct(i['exp60']):>9}{str(i['position_label']):>6}"
                 f"{str(i['buy_quality'] or '-'):>9}{str(i['action']):>12}"
                 f"{_money(i['first_batch_amount']) if i['first_batch_amount'] else '0 元':>10}")

    # ---------------- 组合统计 ----------------
    L.append("")
    L.append("-" * 62)
    L.append("【组合统计】")
    L.append(f"  市场状态 {risk['state']} / 风险评分 {_num(risk['score'])}/100"
             f" / Portfolio Quality {_num(q['score'])}/100"
             f" / Portfolio Entry Score {_num((pk.get('entry_score') or {}).get('score'))}/100")
    L.append(f"  组合1年最大回撤 {_pct((p['stats']['dd_windows'].get('1y') or {}).get('max_drawdown_pct'))}"
             f" / 2年 {_pct((p['stats']['dd_windows'].get('2y') or {}).get('max_drawdown_pct'))}")
    L.append(f"  年化波动 {_pct(p['stats'].get('annual_vol_pct'), sign=False)}"
             f" / Sharpe {_num(p['stats'].get('sharpe'))}"
             f" / Sortino {_num(p['stats'].get('sortino'))}")

    # ---------------- 黄金 / 再平衡 ----------------
    L.append("")
    L.append("【黄金】")
    ms = (snap.get("macro") or {}).get("gold_macro_score") or {}
    L.append(f"  当前 {_num(gold.get('current_pct'))}% / 上限 {_num(gold.get('cap_pct'))}%"
             f" / 宏观评分 {_num(ms.get('score'))}/100（{ms.get('grade_label') or '暂无数据'}）"
             f" / 底部雷达 {gold.get('radar')}（{gold.get('radar_status')}）")
    if ms.get("drivers"):
        L.append(f"  主要利多：{'、'.join(ms['drivers'])}")
    if ms.get("drags"):
        L.append(f"  主要拖累：{'、'.join(ms['drags'])}")
    radar = (snap.get("macro") or {}).get("gold_radar") or {}
    for c in radar.get("conditions", []):
        mark = "√" if c.get("ok") else ("×" if c.get("ok") is False else "？")
        L.append(f"    [{mark}] {c.get('no')}. {c.get('name')}：{c.get('detail')}")
    if radar.get("unknown"):
        L.append(f"    （已知 {radar.get('known')}/{radar.get('total')} 项，"
                 f"未知 {radar.get('unknown')} 项——缺失数据不算『不满足』）")
    L.append(f"  {gold.get('advice')}")
    L.append("")
    L.append("【组合再平衡】")
    L.append(f"  {'需要再平衡' if reb['need'] else '暂不需要再平衡'}")
    for i in reb["items"]:
        L.append(f"  · {i['name']}：当前 {i['current_pct']}% / 目标 {i['target_pct']}%"
                 f"（偏离 {i['deviation_pct']:+.2f}pp，{i['level']}）→ {i['action']}")
    if not reb["items"]:
        L.append("  所有 ETF 偏离均在阈值内")

    # ---------------- 再平衡模拟（§42/§43） ----------------
    rbs = pk.get("rebal_sim") or {}
    if rbs.get("available") and rbs.get("options"):
        L.append("")
        L.append("【再平衡模拟：黄金降到各档位后整个组合会怎样】")
        L.append("  · 不做机械清仓，按『组合质量是否改善』择优；")
        for o in rbs["options"]:
            mark = " ← 最优" if (rbs.get("best") or {}).get("target_pct") == o.get("target_pct") else ""
            shares_txt = (f"{int(o['sell_shares']):,} 份 × {_num(o.get('sell_price'))} 元"
                          if o.get("sell_shares") else "整手候选模拟不可用")
            L.append(f"  · 降到 {_num(o.get('target_pct'))}%：卖出 {_money(o.get('sell_amount'))}"
                     f" = {shares_txt}，实际占比 {_num(o.get('realized_pct'))}%，"
                     f"卖出后现金 {_money(o.get('cash_after'))}｜组合60日概率 "
                     f"{_n(o.get('prob60'), 1, '%')}（{_pct(o.get('improve_prob60'), 1)}）"
                     f"｜1年回撤 {_n(o.get('max_dd_1y'), 2, '%')}"
                     f"｜组合质量 {_num(o.get('quality'))}"
                     f"（{_pct(o.get('improve_quality'), 1)}）{mark}")
        best = rbs.get("best") or {}
        if best:
            sh_txt = (f"卖出 {int(best['sell_shares']):,} 份"
                      if best.get("sell_shares")
                      else f"卖出约 {_money(best.get('sell_amount'))}")
            L.append(f"  → 最合理方案：黄金降到 {_num(best.get('target_pct'))}%（{sh_txt}）")

    # ---------------- 小资金适配 / 最小合理组合资金（§21/§22） ----------------
    cap_fit = ((pk.get("home") or {}).get("capital_fit")) or []
    if cap_fit:
        L.append("")
        L.append("【小资金适配：100 份整数倍下的最小合理组合资金】")
        L.append(f"  {'ETF':<16}{'100份需':>10}{'目标权重':>9}{'最小组合规模':>13}")
        for it in cap_fit:
            L.append(f"  {it['name']:<16}{_money(it.get('min_amount')):>10}"
                     f"{_num(it.get('target_weight_pct'))+'%':>9}"
                     f"{_money(it.get('min_total')):>13}")
        min_total = max((it.get("min_total") or 0) for it in cap_fit)
        L.append(f"  → 完整配置全部 ETF 的理论最低资金：约 {_money(min_total)}")
        for label, key in (("2 万元", "fits_20k"), ("5 万元", "fits_50k"),
                           ("10 万元", "fits_100k")):
            fits = [it["name"] for it in cap_fit if it.get(key)]
            notfits = [it["name"] for it in cap_fit if not it.get(key)]
            txt = f"  · {label}：可配 {'、'.join(fits) if fits else '无'}"
            if notfits:
                txt += f"；暂不适合 {'、'.join(notfits)}（100 份即超配，不强行配置）"
            L.append(txt)

    # ---------------- 结论 ----------------
    L.append("")
    L.append("-" * 62)
    L.append("【今日最终结论】")
    for line in (pk.get("explain") or dec["lines"])[:5]:
        L.append("  · " + line)
    # ---- 【BUY PERMISSION GATE】最终结论必须显式写明买入权限状态 ----
    _hl2 = (pk.get("home") or {}).get("headline") or {}
    if _hl2.get("permission_blocked"):
        _bp2 = _hl2.get("buy_permission") or {}
        _lvl2 = _bp2.get("trust_level") or "研究级"
        _rr2 = _hl2.get("research_reference")
        if _rr2:
            L.append(f"  · 研究参考：模型模拟买入 {_rr2.get('name')} "
                     f"{int(_rr2.get('amount') or 0):,} 元后组合质量最高（仅研究，不执行）。")
        L.append(f"  · {_lvl2}：当前不开放买入权限，因此今天不执行"
                 "（可信等级决定买入权限，不是买入等级）。")

    # ---------------- 一致性与数据源 ----------------
    cons = pk.get("consistency") or {}
    L.append("")
    L.append("【数据一致性检查】")
    if cons.get("ok"):
        L.append("  通过（持仓/市值/总资产/现金/大类权重/权重合计误差均在"
                 f" {cons.get('tolerance_pct')}pp 内）")
    else:
        for e in cons.get("errors", []):
            L.append("  × " + e)
        L.append("  → 已禁止生成任何买入/卖出金额建议，请先修正『设置』。")

    L.append("")
    L.append("【数据源状态】")
    for s in snap.get("data_status", []):
        L.append(f"  {'√' if s['ok'] else '×'} {s['name']}：{s['label']}"
                 f"（{s.get('data_time') or '-'}）{s.get('message') or ''}")

    # ---- 数据来源与有效性（每项参数：来源 / 状态 / 数据日期 / 读取时间到秒 / 网址）----
    prov = snap.get("provenance") or {}
    if prov.get("rows"):
        from .data_provenance import render_block
        L.append("")
        L.append("【数据来源与有效性】" + (prov.get("headline") or ""))
        L.append(f"  核对时间：{prov.get('checked_at')}")
        L.extend("  " + x for x in render_block(prov))
        L.append("  说明：✅=本次联网取到实时 ｜ ○缓存=本次用未过期缓存未联网 ｜ "
                 "⚠缓存=实时源失败退回旧缓存 ｜ ❌=连不上且无可用数据")
    if snap.get("warnings"):
        L.append("")
        L.append("※ 提示：")
        for x in snap["warnings"]:
            L.append("  · " + str(x))
    L.append("=" * 62)
    return "\n".join(L)


def render_questions(snap: dict) -> list[tuple[str, str]]:
    """对应需求『每天必须能回答这 10 个问题』。"""
    p = snap["portfolio"]
    e = p["entry"]
    pk = p.get("plan") or {}
    gate = pk.get("gate") or {}
    plan = pk.get("plan") or {}
    three = pk.get("three") or {}
    today = pk.get("today") or {}
    prob = pk.get("prob") or {}
    triggers = pk.get("triggers") or {}
    g = p["gold_control"]
    w = p["weights"]

    picks = plan.get("picks") or []
    first_pick = picks[0] if picks else None
    reduce_txt = str(three.get("reduce_text"))
    if three.get("reduce_targets"):
        reduce_txt += "（" + "、".join(three["reduce_targets"]) + "）"
    rbs = pk.get("rebal_sim") or {}
    rb_best = rbs.get("best") or {}
    cmp = pk.get("comparison") or {}
    gold_txt = (f"当前 {_num(g.get('current_pct'))}% / 上限 {_num(g.get('cap_pct'))}%"
                f" → {'已超配' if g.get('over_cap') else '未超配'}")
    if rb_best.get("target_pct") is not None:
        gold_txt += (f"；模拟最合理方案：降到 {_num(rb_best.get('target_pct'))}%"
                     f"（卖出约 {_money(rb_best.get('sell_amount'))}）")

    cons = ((pk.get("home") or {}).get("conservative")
            or (pk.get("headline") or {}).get("conservative") or {})

    return [
        ("1. 现在要不要买？",
         f"{today.get('text', '暂无数据')}；"
         f"投入新增资金={three.get('new_money_text')}"),
        ("2. 整个组合未来60日历史上赚钱概率大概多少？",
         (f"决策主口径 {cons.get('prob')}%（独立事件去重样本外，"
          f"独立事件 {cons.get('indep')} 个"
          + ("；样本极少，仅供研究" if cons.get("few_samples") else "")
          + "；重叠滚动OOS频率 "
          f"{cons.get('overlap_freq')}% 仅研究参考）")
         if cons.get("prob") is not None else
         f"{_num(prob.get('p60'), 1)}%（相似状态历史频率，研究参考；"
         f"重叠滚动OOS {_num(prob.get('oos60'), 1)}%，"
         f"样本 {prob.get('samples60')} 次，可信度 {prob.get('grade')}）"),
        ("3. 现在买什么？",
         "、".join(f"{i['name']}（{i['buy_quality']}）" for i in picks)
         if picks else "暂不买入（WAIT）"),
        ("4. 第一笔多少钱？",
         _money(plan.get("total_first_batch_amount")) if picks
         else "0 元（本次不投入新增资金）"),
        ("5. 下一笔什么时候买？",
         (triggers.get("focus") or {}).get("trigger") or "等待触发条件满足"),
        ("6. 为什么？",
         "；".join((pk.get("explain") or [])[:2]) or "—"),
        ("7. 如果现在不买，要等什么条件？",
         "；".join(c["name"] for c in triggers.get("portfolio_conditions", [])
                   if not c.get("ok")) or "全部条件已满足"),
        ("8. 黄金现在是不是超配？", gold_txt),
        ("9. 是不是需要再平衡？",
         f"{three.get('rebalance_text')}；降低超配资产={reduce_txt}"),
        ("10. 现在是否应该什么都不做，继续 WAIT？",
         ("是，继续 WAIT" if not three.get("new_money")
          else "否，可按分批计划执行第一批")
         + (f"（交易前后对比结论：{cmp.get('verdict')}）"
            if cmp.get("available") else "")),
    ]
