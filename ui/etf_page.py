"""ETF 明细页：单只 ETF 的全部指标与图表。"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QComboBox, QGridLayout, QHBoxLayout, QLabel, QListWidget,
                               QListWidgetItem, QScrollArea, QTabWidget, QVBoxLayout, QWidget)

from .theme import palette
from .widgets import BarChart, Card, DataTable, LineChart, Tile, badge

PERIODS = [("1个月", 21), ("3个月", 63), ("6个月", 126), ("1年", 252),
           ("2年", 504), ("5年", 1260), ("全部", 10 ** 9)]


def _f(v, nd=2, suffix="", sign=False):
    if v is None:
        return "暂无数据"
    s = f"{v:+.{nd}f}" if sign else f"{v:.{nd}f}"
    return f"{s}{suffix}"


def _money(v) -> str:
    if v is None:
        return "暂无数据"
    try:
        return f"{float(v):,.0f} 元"
    except (TypeError, ValueError):
        return "暂无数据"


class EtfPage(QWidget):
    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.snap: dict | None = None
        self.rows: list[dict] = []
        self.cur = 0

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        body = QHBoxLayout()
        outer.addLayout(body)

        left = Card("ETF 列表")
        left.setFixedWidth(212)
        self.list = QListWidget()
        self.list.currentRowChanged.connect(self._on_select)
        left.add(self.list)
        body.addWidget(left)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        body.addWidget(self.scroll, 1)
        self.container = QWidget()
        self.scroll.setWidget(self.container)
        self.root = QVBoxLayout(self.container)
        self.root.setContentsMargins(0, 0, 4, 4)
        self.root.setSpacing(10)

        self.lbl_head = QLabel("暂无数据")
        self.lbl_head.setObjectName("SectionTitle")
        self.lbl_head.setWordWrap(True)
        self.root.addWidget(self.lbl_head)

        self.grid_head = QGridLayout()
        self.grid_head.setSpacing(9)
        self.root.addLayout(self.grid_head)

        # ---- 需求廿一：买入质量（A/B/C/D/E）+ 60日胜率/期望/盈亏比 +
        #      【当前是否适合买】YES / WAIT ----
        self.lbl_decide = QLabel("【当前是否适合买】暂无数据")
        self.lbl_decide.setObjectName("SectionTitle")
        self.lbl_decide.setWordWrap(True)
        self.root.addWidget(self.lbl_decide)
        self.grid_decide = QGridLayout()
        self.grid_decide.setSpacing(9)
        self.root.addLayout(self.grid_decide)

        bar = QHBoxLayout()
        bar.addWidget(QLabel("图表区间"))
        self.cmb_period = QComboBox()
        self.cmb_period.addItems([p[0] for p in PERIODS])
        self.cmb_period.setCurrentIndex(3)
        self.cmb_period.currentIndexChanged.connect(self._redraw)
        bar.addWidget(self.cmb_period)
        bar.addStretch(1)
        self.lbl_grade = QLabel("")
        bar.addWidget(self.lbl_grade)
        self.root.addLayout(bar)

        self.tabs = QTabWidget()
        self.root.addWidget(self.tabs)
        self._tab_charts()
        self._tab_ind()
        self._tab_hist()
        self._tab_marginal()
        self.root.addStretch(1)

    # ------------------------------------------------------------------
    def _tab_charts(self) -> None:
        w = QWidget()
        lay = QVBoxLayout(w)
        self.ch_price = LineChart("价格与均线", 260)
        lay.addWidget(self.ch_price)
        self.ch_dd = LineChart("当前回撤（%，相对窗口内最高收盘）", 190)
        lay.addWidget(self.ch_dd)
        self.ch_ret = LineChart("20日涨跌幅（%）", 180)
        lay.addWidget(self.ch_ret)
        self.ch_bias = LineChart("20日乖离率 BIAS20（%）", 180)
        lay.addWidget(self.ch_bias)
        self.ch_rel = LineChart("相对价位（0-100，1年/2年）", 190)
        lay.addWidget(self.ch_rel)
        self.ch_wr = LineChart("Williams %R(14)（-100 ~ 0）", 180)
        lay.addWidget(self.ch_wr)
        self.ch_swing = LineChart("波段状态分（高=接近波段底部）", 180)
        lay.addWidget(self.ch_swing)
        self.ch_buy = LineChart("买入质量代理（历史序列，近似；不是胜率）", 180)
        lay.addWidget(self.ch_buy)
        self.tabs.addTab(w, "图表")

    def _tab_ind(self) -> None:
        w = QWidget()
        lay = QVBoxLayout(w)
        self.tbl_ma = DataTable()
        lay.addWidget(self._titled("均线（MA5~MA200）", self.tbl_ma))
        self.tbl_tech = DataTable()
        lay.addWidget(self._titled("技术指标", self.tbl_tech))
        self.tbl_score = DataTable()
        lay.addWidget(self._titled("评分", self.tbl_score))
        self.tbl_gold = DataTable()
        self.w_gold = self._titled("黄金专用指标", self.tbl_gold)
        lay.addWidget(self.w_gold)
        self.tbl_flags = DataTable()
        lay.addWidget(self._titled("风控标记", self.tbl_flags))
        self.tabs.addTab(w, "指标明细")

    def _tab_hist(self) -> None:
        w = QWidget()
        lay = QVBoxLayout(w)
        self.tbl_hist = DataTable()
        lay.addWidget(self._titled("历史相似状态统计（含样本外与基准对比）", self.tbl_hist))
        self.lbl_hist_note = QLabel("")
        self.lbl_hist_note.setWordWrap(True)
        self.lbl_hist_note.setObjectName("Muted")
        lay.addWidget(self.lbl_hist_note)
        self.tabs.addTab(w, "历史统计")

    def _tab_marginal(self) -> None:
        """需求廿六：加入这个 ETF 以后，整个组合会怎样。"""
        w = QWidget()
        lay = QVBoxLayout(w)
        self.lbl_marginal_head = QLabel("")
        self.lbl_marginal_head.setWordWrap(True)
        self.lbl_marginal_head.setObjectName("CardTitle")
        lay.addWidget(self.lbl_marginal_head)
        self.tbl_marginal = DataTable()
        lay.addWidget(self.tbl_marginal)
        self.lbl_marginal_note = QLabel("")
        self.lbl_marginal_note.setWordWrap(True)
        self.lbl_marginal_note.setObjectName("Muted")
        lay.addWidget(self.lbl_marginal_note)
        lay.addStretch(1)
        self.tabs.addTab(w, "边际组合分析")

    def _titled(self, title: str, widget: QWidget) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        lbl = QLabel(title)
        lbl.setObjectName("CardTitle")
        lay.addWidget(lbl)
        lay.addWidget(widget)
        return w

    # ------------------------------------------------------------------
    def set_snapshot(self, snap: dict) -> None:
        self.snap = snap
        self.rows = [r for r in (snap.get("rows") or []) if r.get("snapshot")]
        self.list.clear()
        for r in self.rows:
            s = r["snapshot"]
            scor = r.get("scores") or {}
            txt = (f"{s.get('name')}\n"
                   f"{s.get('price')} · 买入质量 {scor.get('grade', '—')} · "
                   f"60日胜率 {('%.1f%%' % scor['prob60']) if scor.get('prob60') is not None else '暂无'}")
            it = QListWidgetItem(txt)
            self.list.addItem(it)
        if self.rows:
            self.list.setCurrentRow(min(self.cur, len(self.rows) - 1))
        else:
            self.lbl_head.setText("暂无可用 ETF 数据")

    def refresh_theme(self) -> None:
        if self.snap:
            self.set_snapshot(self.snap)

    def _on_select(self, idx: int) -> None:
        if idx < 0 or idx >= len(self.rows):
            return
        self.cur = idx
        self._fill()

    # ------------------------------------------------------------------
    def _fill(self) -> None:
        r = self.rows[self.cur]
        s = r["snapshot"]
        sc = r.get("scores") or {}
        a = r["asset"]
        pal = palette()

        self.lbl_head.setText(
            f"{s.get('name')}（{s.get('thscode')}）　类别："
            f"{ {'bond': '债券', 'stock': '股票', 'gold': '黄金'}.get(a.get('asset_class'), a.get('asset_class')) }"
            f"　数据日：{s.get('data_time')}　K线：{s.get('bars')}")
        self.lbl_grade.setText(
            f"等级 {sc.get('grade', '—')} · {sc.get('grade_note', '')}")

        # 顶部指标块
        while self.grid_head.count():
            it = self.grid_head.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        pnl = s.get("pnl")
        items = [
            ("最新价", _f(s.get("price"), 3), f"日涨跌 {_f(s.get('change_pct'), 2, '%', True)}",
             "up" if (s.get("change_pct") or 0) >= 0 else "down"),
            ("持仓", f"{s.get('shares', 0):,.0f} 份",
             f"成本 {_f(s.get('cost'), 4)} · 市值 {_f(s.get('market_value'), 0)} 元", None),
            ("盈亏", _f(pnl, 0), f"{_f(s.get('pnl_pct'), 2, '%', True)}", 
             "up" if (pnl or 0) >= 0 else "down"),
            ("当前权重 / 目标", f"{_f(r.get('weight_pct'), 2, '%')} / "
                             f"{_f(r.get('target_weight_pct'), 1, '%')}",
             f"目标缺口 {_f(r.get('gap_pct'), 2, '%', True)}", None),
            ("趋势分 / 状态", _f(sc.get("trend"), 1), sc.get("trend_class", ""), None),
            ("位置分（高=高估）", _f(sc.get("position"), 1),
             f"1年 {_f(s['relpos'].get('1y'), 1)} · 2年 {_f(s['relpos'].get('2y'), 1)}", None),
            ("短线分（高=超买）", _f(sc.get("shortterm"), 1),
             sc.get("shortterm_class", ""), None),
            ("波段分（高=底部）", _f(sc.get("swing"), 1),
             (s.get("swing") or {}).get("state", ""), None),
        ]
        for i, (lbl, val, sub, col) in enumerate(items):
            t = Tile(lbl, val, sub)
            if col:
                t.set(val, sub, col)
            self.grid_head.addWidget(t, i // 4, i % 4)

        self._fill_decide(r)
        self._fill_ind(r)
        self._fill_hist(r)
        self._fill_marginal(r)
        self._redraw()

    # ------------------------------------------------------------------
    def _plan_item(self, aid: str) -> dict:
        """从组合决策包里取出该 ETF 的分配计划项（需求十一/廿一）。"""
        pkg = (self.snap or {}).get("portfolio", {}).get("plan") or {}
        plan = pkg.get("plan") or {}
        for it in (plan.get("items") or []):
            if str(it.get("id")) == str(aid):
                return it
        return {}

    def _fill_decide(self, r) -> None:
        """需求廿一：【买入质量】A/B/C/D/E + 60日胜率/期望/盈亏比 + 【是否适合买】。"""
        sc = r.get("scores") or {}
        aid = r["asset"].get("id")
        item = self._plan_item(aid)
        grade = sc.get("grade") or item.get("buy_quality") or "—"
        prob60 = sc.get("prob60")
        if prob60 is None:
            prob60 = item.get("prob60")
        exp60 = sc.get("exp60")
        if exp60 is None:
            exp60 = item.get("exp60")
        payoff = sc.get("payoff")
        if payoff is None:
            payoff = item.get("payoff")

        # 是否适合买：以组合决策包的"今日动作"为准（组合同意 + 该 ETF 可执行）
        action = item.get("action") or ""
        suitable = bool(item.get("actionable"))
        if item.get("actionable"):
            verdict, vcolor = "YES（可执行第一批）", "up"
        elif "再平衡" in action or "减仓" in action:
            verdict, vcolor = "NO（先再平衡/减配）", "accent"
        elif "不追涨" in action or "不抄底" in action or "WAIT" in action:
            verdict, vcolor = "WAIT（暂不适合）", "muted"
        elif "持有" in action:
            verdict, vcolor = "NO（已达标，持有即可）", "warn"
        elif item:
            verdict, vcolor = "WAIT（暂不适合）", "muted"
        else:
            verdict, vcolor = "暂无组合决策（先刷新）", "muted"

        self.lbl_decide.setText(f"【当前是否适合买】{verdict}　"
                                f"（买入质量 {grade} 级 · 今日动作：{action or '—'}）")

        while self.grid_decide.count():
            it = self.grid_decide.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        pos_lbl = item.get("position_label") or "—"
        dec_items = [
            ("买入质量", f"{grade} 级",
             "A≥65% / B 58-65% / C 50-58% / D<50%（60日历史盈利概率口径）", None),
            ("60日历史盈利概率", _f(prob60, 1, "%"),
             "历史相似状态下的统计概率，不代表未来收益", None),
            ("60日期望收益", _f(exp60, 2, "%", True),
             f"扣成本后参考；当前位：{pos_lbl}", None),
            ("盈亏比", _f(payoff, 2),
             "平均盈利 / 平均亏损（60日）", None),
            ("第一批建议金额",
             (f"{int(item.get('first_batch_amount') or 0):,} 元"
              if item else "暂无数据"),
             (item.get("reason") or "按目标缺口分 4 批")[:40], None),
            ("是否适合买", "YES" if suitable else "WAIT",
             verdict, vcolor),
        ]
        for i, (lbl, val, sub, col) in enumerate(dec_items):
            t = Tile(lbl, val, sub)
            if col:
                t.set(val, sub, col)
            self.grid_decide.addWidget(t, i // 3, i % 3)

    def _fill_ind(self, r) -> None:
        s, sc = r["snapshot"], r["scores"]
        ind = r["ind"]
        ma = s["ma"]
        slopes = {k: v for k, v in
                  {f"ma{w}": (ind.get("ma_slope") or {}).get(f"ma{w}", [None])[-1]
                   for w in (5, 10, 20, 30, 50, 60, 120, 200)}.items()}
        self.tbl_ma.set_rows(
            ["均线", "数值", "价格相对均线%", "20日斜率(%/bar)"],
            [[k.upper(), _f(ma.get(k), 3),
              _f((float(s["price"]) / ma[k] - 1) * 100, 2, "%", True) if ma.get(k) else None,
              _f(slopes.get(k), 3)]
             for k in ("ma5", "ma10", "ma20", "ma30", "ma50", "ma60", "ma120", "ma200")],
            color_cols={2, 3})
        self.tbl_ma.setMinimumHeight(240)

        m = s.get("macd") or {}
        dd = s["dd"]
        rows = [
            ["MACD DIF / DEA / HIST", f"{_f(m.get('dif'), 4)} / {_f(m.get('dea'), 4)} / "
                                      f"{_f(m.get('hist'), 4)}", "", ""],
            ["RSI(14)", _f(s.get("rsi14"), 2), "", ""],
            ["ATR(14) / ATR%", f"{_f(s.get('atr14'), 3)} / {_f(s.get('atr_pct'), 2)}%", "", ""],
            ["WR(14)", _f(s.get("wr14"), 2), s.get("wr_class", ""), ""],
            ["WR 状态", "超卖反转已确认" if (s.get("wr_reversal") or {}).get("confirmed_reversal")
             else "未确认反转",
             f"满足条件 {(s.get('wr_reversal') or {}).get('score')}/4", ""],
            ["20日涨跌幅", _f(s.get("ret20"), 2, "%", True), s.get("ret20_class", ""), ""],
            ["20日乖离率 BIAS20", _f(s.get("bias20"), 2, "%", True), s.get("bias20_class", ""), ""],
            ["20日年化波动率", _f(s.get("vol20_annual_pct"), 2, "%"), "", ""],
            ["1年最大回撤 / 当前回撤",
             f"{_f((dd.get('1y') or {}).get('max_drawdown_pct'), 2, '%')} / "
             f"{_f((dd.get('1y') or {}).get('current_drawdown_pct'), 2, '%')}", "", ""],
            ["2年最大回撤 / 当前回撤",
             f"{_f((dd.get('2y') or {}).get('max_drawdown_pct'), 2, '%')} / "
             f"{_f((dd.get('2y') or {}).get('current_drawdown_pct'), 2, '%')}", "", ""],
            ["1年相对价位", _f(s["relpos"].get("1y"), 1), s["relpos_class"].get("1y", ""), ""],
            ["2年相对价位", _f(s["relpos"].get("2y"), 1), s["relpos_class"].get("2y", ""), ""],
            ["近期高低点（60日）",
             f"{_f((s['high_low'].get('60d') or {}).get('high'), 3)} / "
             f"{_f((s['high_low'].get('60d') or {}).get('low'), 3)}", "", ""],
            ["52周高低点",
             f"{_f((s['high_low'].get('52w') or {}).get('high'), 3)} / "
             f"{_f((s['high_low'].get('52w') or {}).get('low'), 3)}", "", ""],
            ["波段状态", (s.get("swing") or {}).get("state", ""),
             (s.get("swing") or {}).get("detail", ""), ""],
        ]
        self.tbl_tech.set_rows(["指标", "数值", "分类/说明", ""], rows,
                               color_cols={1}, stretch_cols=[2])
        self.tbl_tech.setColumnHidden(3, True)
        self.tbl_tech.setMinimumHeight(430)

        bf = sc.get("buy_factors") or {}
        ef = sc.get("entry_factors") or {}
        rows = [
            ["Trend Score 趋势分", sc.get("trend"), "高=趋势越强（上涨结构越完整）"],
            ["Position Score 位置分", sc.get("position"), "高=位置越高（越贵）"],
            ["ShortTerm Score 短线分", sc.get("shortterm"), "高=越超买/越热"],
            ["Swing Score 波段分", sc.get("swing"), "高=越接近波段底部"],
            ["买入质量（Buy Score）", sc.get("buy_score"), "0-100 辅助分，不是胜率；等级见上方 A/B/C/D/E"],
            ["Entry Quality 入场质量", sc.get("entry_quality"), "60日胜率/期望/盈亏比/风险/技术确认/位置/波段 加权"],
            ["技术确认分", sc.get("technical_confirm"), "止跌与趋势确认（不含未来信息）"],
            ["买入质量等级", sc.get("grade"), sc.get("grade_note", "")],
        ]
        self.tbl_score.set_rows(["评分", "数值", "含义"], rows, color_cols={1},
                                bold_cols={1}, stretch_cols=[2])
        self.tbl_score.setMaximumHeight(230)

        rows = [[k, _f(v, 1)] for k, v in bf.items()]
        rows += [["—— 入场质量分项 ——", ""]] + [[k, _f(v, 1)] for k, v in ef.items()]
        self.tbl_score.setToolTip("")

        flags = sc.get("flags") or {}
        frows = [
            ["高位强趋势（禁止追涨）", "是" if flags.get("no_chase") else "否",
             flags.get("block_reason") if flags.get("no_chase") else ""],
            ["深度超跌（1年/2年低位 + 大跌 + 超卖）",
             "是" if flags.get("deep_oversold") else "否", ""],
            ["尚未止跌（创新低 + MA20向下 + 底部未确认）",
             "是" if flags.get("not_stopped_falling") else "否", ""],
            ["禁止抄底", "是" if flags.get("no_bottom_fishing") else "否",
             flags.get("block_reason") if flags.get("no_bottom_fishing") else ""],
            ["WR 超卖反转已确认", "是" if flags.get("wr_reversal_confirmed") else "否", ""],
            ["是否被风控阻断（影响买入等级）", "是" if flags.get("blocked") else "否",
             flags.get("block_reason") or ""],
        ]
        self.tbl_flags.set_rows(["风控项", "状态", "说明"], frows, stretch_cols=[2])
        self.tbl_flags.setMinimumHeight(200)

        # 黄金专用
        if r["asset"].get("asset_class") == "gold" and self.snap.get("macro"):
            self.w_gold.setVisible(True)
            m = self.snap["macro"]
            gm, um = m.get("gold_metrics") or {}, m.get("usd_metrics") or {}
            rm, nm = m.get("real_rate_metrics") or {}, m.get("nominal_rate_metrics") or {}
            bm = m.get("bpgdm_metrics") or {}
            gv, pm = m.get("gvz_metrics") or {}, m.get("policy_metrics") or {}
            radar = m.get("gold_radar") or {}
            ms = m.get("gold_macro_score") or {}
            proxy = m.get("gold_sentiment_proxy") or {}
            rows = [
                ["国际金价", _f(gm.get("last"), 2), gm.get("data_time") or ""],
                ["金价 20日/60日变动", f"{_f(gm.get('ret20_pct'), 2)}% / "
                                      f"{_f(gm.get('ret60_pct'), 2)}%", ""],
                ["金价偏离 MA200", _f(gm.get("dev_ma200_pct"), 2, "%"), "正=长期均线上方"],
                ["距52周高点", _f(gm.get("dd_from_high_pct"), 2, "%"), ""],
                ["美元指数 DXY", _f(um.get("last"), 2), um.get("direction") or ""],
                ["美国10年期实际利率", _f(rm.get("last"), 2, "%"), rm.get("direction") or ""],
                ["美国10年期国债收益率", _f(nm.get("last"), 2, "%"), nm.get("direction") or ""],
                ["Fed利率预期(2Y-3M)", _f(pm.get("last"), 2) if pm.get("available") else "暂无数据",
                 pm.get("direction") or "负=市场定价未来降息（利多黄金）"],
                ["黄金波动率指数 GVZ",
                 _f(gv.get("gvz"), 2) if gv.get("available") else "暂无数据",
                 (f"来源 {gv.get('gvz_source') or ''}｜系统研究分档『{gv.get('band_name') or ''}』"
                  f"（非官方阈值）"
                  + (f"，52周分位 {gv.get('gvz_52w_percentile')}%"
                     if gv.get("gvz_52w_percentile") is not None else "")
                  + ("，低位拐头向上" if gv.get("turning_up") else "")
                  + ("，短期骤升" if gv.get("gvz_spike") else ""))],
                ["BPGDM 真实值（StockQ $BPGDM）",
                 _f(bm.get("bpgdm_real"), 2) if bm.get("real_available") else "❌ 取数失败（错误）",
                 (str(bm.get("bpgdm_real_source") or "")
                  + ("｜说明：源站对本机 IP 段 403，已经 r.jina.ai 阅读器**转发**"
                     "同一页面（来源仍是 StockQ）" if bm.get("bpgdm_relay") else "")
                  + f"｜数据日 {bm.get('bpgdm_real_data_date') or '—'}"
                  if bm.get("real_available")
                  else "真实源不可用：" + str(bm.get("real_error") or "本次未取到")
                       + "｜网址：" + str(bm.get("real_url")
                                      or "https://en.stockq.org/index/BPGDM.php")
                       + "（绝不拿代理冒充，详见设置页『数据来源与有效性』）")],
                ["BPGDM 代理值【非真实BPGDM】",
                 _f(bm.get("bpgdm_proxy"), 2) if bm.get("proxy_available") else "—",
                 ((bm.get("bpgdm_proxy_algo") or "无代理数据")
                  + "｜仅作方向参考、不参与宏观评分")],
                ["BPGDM 研究区间",
                 bm.get("zone_name") or "暂无数据",
                 ("示意：" + (bm.get("pnf_note") or "P&F 数据缺失则为 UNKNOWN"))],
                ["BPGDM 2年分位",
                 _f(bm.get("percentile_2y"), 1, "%") if bm.get("available") else "暂无数据",
                 "低位拐头" if bm.get("turning_up") else ""],
                ["黄金宏观评分(8指标加权)",
                 f"{_f(ms.get('score'), 1)}/100" if ms.get("available") else "暂无数据",
                 ms.get("grade_label") or "50=中性"],
                ["Gold Fear & Greed（官方）", "暂无数据",
                 "官方接口不可用时不伪造，改用下面的本地代理"],
                ["黄金情绪（本地代理）",
                 _f(proxy.get("score"), 1) if proxy.get("available") else "暂无数据",
                 proxy.get("label") or ""],
                ["黄金底部雷达", f"{radar.get('hit')}/{radar.get('total')}",
                 radar.get("status") or ""],
                ["本轮金价是否停止创新低",
                 "是" if gm.get("stopped_new_low") else "否", ""],
            ]
            self.tbl_gold.set_rows(["指标", "数值", "说明"], rows, color_cols={1},
                                   stretch_cols=[2])
            self.tbl_gold.setMinimumHeight(400)
        else:
            self.w_gold.setVisible(False)

        self.lbl_hist_note.setText(
            "胜率来自『历史上与今天状态最相似的真实交易日』的未来收益分布；"
            "样本数、样本评级、距离倍数、全部历史基准胜率、样本外胜率全部如实展示。"
            "低位置≠可买、高胜率≠重仓，必须与止跌确认、盈亏比、回撤一起看。")

    def _fill_hist(self, r) -> None:
        h = r.get("hist") or {}
        rows = []
        for hz in (5, 20, 60, 120):
            d = h.get(hz) or {}
            rows.append([
                f"{hz} 交易日", d.get("win_rate_pct"), d.get("win_rate_net_pct"),
                f"{d.get('ci95_low')}~{d.get('ci95_high')}",
                d.get("avg_return_pct"), d.get("expected_return_pct"),
                d.get("expected_return_net_pct"), d.get("median_return_pct"),
                d.get("avg_win_pct"), d.get("avg_loss_pct"), d.get("payoff_ratio"),
                d.get("max_win_pct"), d.get("max_loss_pct"),
                d.get("max_drawdown_pct"), d.get("avg_max_drawdown_pct"),
                d.get("prob_ge_3_pct"), d.get("prob_ge_5_pct"), d.get("prob_ge_10_pct"),
                d.get("prob_ge_20_pct"), d.get("prob_le_minus5_pct"),
                d.get("prob_le_minus10_pct"), d.get("prob_le_minus20_pct"),
                d.get("samples"), d.get("pool_size"), d.get("sample_grade"),
                d.get("credibility"), d.get("distance_mult"),
                d.get("all_history_win_rate_pct"), d.get("recent_5y_win_rate_pct"),
                d.get("edge_vs_baseline_pct"), d.get("oos_samples"),
                d.get("oos_win_rate_pct"),
            ])
        self.tbl_hist.set_rows(
            ["周期", "盈利概率%", "扣成本后%", "95%CI", "平均收益%", "期望收益%",
             "扣成本期望%", "中位收益%", "平均盈利%", "平均亏损%", "盈亏比",
             "最大盈利%", "最大亏损%", "最大回撤%", "平均最大回撤%",
             "≥3%概率%", "≥5%概率%", "≥10%概率%", "≥20%概率%",
             "≤-5%概率%", "≤-10%概率%", "≤-20%概率%",
             "样本数", "候选池", "样本评级", "可信度", "距离倍数",
             "全部历史胜率%", "最近5年胜率%", "相对基准%", "样本外样本数", "样本外胜率%"],
            rows, color_cols={1, 4, 5, 6, 7, 8, 9, 11, 12, 13, 29}, bold_cols={1})
        self.tbl_hist.setMinimumHeight(170)

    # ------------------------------------------------------------------
    def _redraw(self) -> None:
        if not self.rows:
            return
        r = self.rows[self.cur]
        ind, feats, s = r["ind"], r.get("features") or {}, r["snapshot"]
        n = ind["n"]
        bars = PERIODS[self.cmb_period.currentIndex()][1]
        a = max(0, n - bars)
        sl = slice(a, n)
        dates = ind["dates"][sl]
        pal = palette()
        series_colors = pal["series"]

        price = [{"name": "收盘", "values": ind["close"][sl], "width": 1.8}]
        for i, w in enumerate((20, 60, 120, 200)):
            ser = (ind.get("ma") or {}).get(f"ma{w}")
            if ser:
                price.append({"name": f"MA{w}", "values": ser[sl],
                              "color": series_colors[(i + 1) % len(series_colors)],
                              "width": 1.2})
        self.ch_price.set_series(price, dates, y_fmt=".3f")

        dd = []
        cd = ind.get("curdd_series") or {}
        c1 = cd.get("1y") or [None] * n
        c2 = cd.get("2y") or [None] * n
        self.ch_dd.set_series(
            [{"name": "当前1年回撤", "values": c1[sl], "fill": True},
             {"name": "当前2年回撤", "values": c2[sl],
              "color": pal["series"][3], "width": 1.2}],
            dates, zero_line=True, y_fmt=".1f")

        self.ch_ret.set_series(
            [{"name": "20日涨跌%", "values": ind["ret20"][sl], "fill": True}],
            dates, zero_line=True, y_fmt=".1f")
        self.ch_bias.set_series(
            [{"name": "BIAS20%", "values": ind["bias20"][sl], "fill": True}],
            dates, zero_line=True, y_fmt=".1f")
        rp = ind.get("relpos_series") or {}
        self.ch_rel.set_series(
            [{"name": "1年相对价位", "values": (rp.get("1y") or [None] * n)[sl], "fill": True},
             {"name": "2年相对价位", "values": (rp.get("2y") or [None] * n)[sl],
              "color": pal["series"][3], "width": 1.2}],
            dates, y_fmt=".0f")
        self.ch_wr.set_series(
            [{"name": "WR(14)", "values": ind["wr14"][sl], "fill": True}],
            dates, y_fmt=".0f")

        sw = feats.get("swing") or [None] * n
        self.ch_swing.set_series(
            [{"name": "波段分", "values": sw[sl], "fill": True}],
            dates, y_fmt=".0f")
        tr = feats.get("trend") or [None] * n
        ps = feats.get("position") or [None] * n
        approx = []
        for i in range(n):
            parts = []
            if tr[i] is not None:
                parts.append((tr[i], 0.40))
            if ps[i] is not None:
                parts.append((100 - ps[i], 0.30))
            if sw[i] is not None:
                parts.append((sw[i], 0.30))
            approx.append(sum(v * w for v, w in parts) / sum(w for _, w in parts)
                          if parts else None)
        self.ch_buy.set_series(
            [{"name": "买入质量代理（近似）", "values": approx[sl], "fill": True}],
            dates, y_fmt=".0f")

    # ------------------------------------------------------------------
    def _fill_marginal(self, r) -> None:
        """需求廿六 / 需求九：把现金换入这只 ETF 后，整个组合会怎样（金额网格）。"""
        pkg = (self.snap or {}).get("portfolio", {}).get("plan") or {}
        marg = pkg.get("marginal") or {}
        aid = str(r["asset"].get("id"))
        m = (marg.get("per_asset") or {}).get(aid)
        cur = (self.snap or {}).get("portfolio", {}).get("weights") or {}
        total = float(cur.get("total_assets") or 0)
        if not m or not m.get("available"):
            self.lbl_marginal_head.setText(
                f"{r['asset'].get('name')}：暂无边际组合分析（可能未填写总资产/持仓，"
                "或该标的不在候选内）。")
            self.tbl_marginal.set_rows([], [])
            self.lbl_marginal_note.setText("")
            return

        sims = m.get("simulations") or []
        best_amt = float(m.get("amount") or 0)
        opt_amt = float(m.get("optimal_amount") or 0)
        base = m.get("baseline") or marg.get("baseline") or {}
        head = (f"{r['asset'].get('name')}：把【待投资现金】换入该 ETF 之后，"
                f"整个组合的统计变化（总资产 {total:,.0f} 元）\n"
                f"模型最优金额 {_money(opt_amt)}；"
                + ("本批建议金额 " + _money(best_amt) if best_amt > 0
                   else "本批建议金额 0 元（WAIT：买入无法让整个组合变得更好）")
                + f"；不交易基准 60日概率 {_f(base.get('prob60'), 1, '%')}")
        self.lbl_marginal_head.setText(head)

        rows = []
        for s in sims:
            amt = float(s.get("amount") or 0)
            rows.append([
                "0 元（不交易）" if amt <= 0 else _money(amt),
                _f(s.get("prob60"), 1, "%"),
                _f(s.get("prob60_oos"), 1, "%"),
                _f(s.get("exp60"), 2, "%", True),
                _f(s.get("dd60"), 2, "%"),
                _f(s.get("max_dd_1y"), 2, "%"),
                _f(s.get("vol_annual"), 2, "%"),
                _f(s.get("quality"), 1),
                _f(s.get("marginal_score"), 1),
                "← 本批建议" if abs(amt - best_amt) < 1e-9 and amt > 0 else "",
            ])
        self.tbl_marginal.set_rows(
            ["买入金额", "组合60日概率", "样本外", "60日期望", "60日回撤",
             "1年回撤", "年化波动", "组合质量", "边际改善分", ""],
            rows or [["暂无数据", "", "", "", "", "", "", "", "", ""]],
            color_cols={8}, bold_cols={0}, stretch_cols=[0])
        self.tbl_marginal.setColumnHidden(9, True)
        self.tbl_marginal.setMinimumHeight(200)
        self.lbl_marginal_note.setText(
            "「边际改善分 50 = 与不交易完全一样」，> 50 才说明这笔买入让整个组合变好；"
            "所有金额档位共用【同一批历史相似窗口】，因此差异只来自组合构成变化，"
            "不含未来数据；概率只是历史条件频率，不代表未来实际收益。")
