"""组合总览页（首页）。

第二阶段首页只回答普通用户 5 秒内想知道的事：
    总资产 / 今天到底怎么操作 / 组合未来60日历史盈利概率（含样本外、样本数、
    置信区间、可信度）/ 当前实际仓位（含现金）/ 战略目标 / 当前最重要的问题 /
    下一次买入条件 / 今天最值得配置（买什么、第一笔多少钱）/ 待投资资金 /
    不要追涨 / 不要抄底 / 今日最终结论。

专业模式：原有全部指标（相关性、风险贡献、再平衡、宏观、回测）在下方标签页；
新手模式：只保留"买不买、买什么、买多少、为什么、下一笔什么时候买、黄金"。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QComboBox, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
                               QMessageBox, QPlainTextEdit, QPushButton, QScrollArea,
                               QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout,
                               QWidget)

from .theme import palette
from .widgets import (BarChart, Card, DataTable, HeatmapTable, LineChart, Tile,
                      kpi_grid)
from .worker import BacktestWorker

PERIODS = [("1个月", 21), ("3个月", 63), ("6个月", 126), ("1年", 252),
           ("2年", 504), ("5年", 1260), ("全部", 10 ** 9)]


def _fmt(v, nd=2, suffix="", sign=False):
    if v is None:
        return "暂无数据"
    s = f"{v:+.{nd}f}" if sign else f"{v:.{nd}f}"
    return f"{s}{suffix}"


def _money(v):
    return "暂无数据" if v is None else f"{v:,.0f} 元"


def _sample_gate_text(snap, pk) -> str:
    """单只 ETF「样本门禁」说明（刻意保守，非故障）。无需要提示时返回 ''。"""
    items = ((pk.get("plan") or {}).get("items") or [])
    short = [i for i in items
             if i.get("asset_class") == "stock"
             and (i.get("buy_quality") or "") == "样本不足"]
    if not short:
        return ""
    rows = {str((r.get("asset") or {}).get("name")): r
            for r in ((snap or {}).get("rows") or [])}
    parts = []
    for i in short:
        r = rows.get(str(i.get("name")))
        n = None
        if r:
            n = (((r.get("hist") or {}).get(60) or {}).get("samples"))
        parts.append(str(i.get("name"))[:10] + (f" {n} 个" if n is not None else ""))
    return ("※ 样本门禁说明：单只 ETF 买入评级要求相似样本 ≥50 个、相似距离 ≤3×"
            "（超出不自动放宽）。当前 " + "、".join(parts)
            + " 样本不足 → 买入质量只能判「样本不足」，因此按当前口径不会给出"
            "股票买入建议。这是刻意保守的设定（宁可不出信号，也不用远距离样本凑数），"
            "不是程序故障。")


class PortfolioPage(QWidget):
    # 执行台账：用户在结论卡点「我已按此执行」后，通知主窗重新计算
    config_changed = Signal()

    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.snap: dict | None = None
        self.bt_worker: BacktestWorker | None = None
        self.simple = False
        self._exec_payload: dict | None = None
        # 实时持仓面板状态
        self._hold_loading = False
        self._hold_dirty = False
        self._hold_row_ids: list = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        outer.addWidget(self.scroll)

        self.container = QWidget()
        self.scroll.setWidget(self.container)
        self.root = QVBoxLayout(self.container)
        self.root.setContentsMargins(2, 2, 2, 2)
        self.root.setSpacing(10)

        self.lbl_loading = QLabel("正在加载数据…首次运行通常 10-60 秒，"
                                  "不可达的数据源会自动跳过并标注『暂无数据』")
        self.lbl_loading.setAlignment(Qt.AlignCenter)
        self.lbl_loading.setWordWrap(True)
        self.lbl_loading.setStyleSheet("padding:40px; font-size:14px;")
        self.root.addWidget(self.lbl_loading)

        self._build_verdict()      # 第一屏结论（最重要，永远在最上面）
        self._build_holdings()     # 我的持仓（实时）：改份额→立即重算判断
        self._build_summary()      # 总资产 / 数据时间
        self._build_simple()       # 新手模式
        self._build_full()         # 专业模式（完整首页）
        self._build_tabs()

        self.root.addStretch(1)
        self.set_loading(True)

    # ==================================================================
    # 第一屏：今天怎么做（结论优先，5 秒看懂）
    # ==================================================================
    def _build_verdict(self) -> None:
        from PySide6.QtWidgets import QFrame
        self.card_verdict = QFrame()
        self.card_verdict.setObjectName("VerdictCard")
        v = QVBoxLayout(self.card_verdict)
        v.setContentsMargins(18, 14, 18, 14)
        v.setSpacing(8)

        head = QLabel("【今天怎么做】")
        head.setObjectName("CardTitle")
        v.addWidget(head)

        # ---- 数据陈旧横幅（FRESHNESS GATE 结果回流首屏）----
        # 默认隐藏；仅当关键行情超过允许新鲜度（或缺失）时显示：
        # 直接把「数据日期 + 陈旧天数 + 非实时」顶到第一屏，避免用户拿旧价当实时价。
        self.v_stale = QLabel("")
        self.v_stale.setWordWrap(True)
        self.v_stale.setVisible(False)
        v.addWidget(self.v_stale)

        self.v_big = QLabel("正在计算…")
        self.v_big.setObjectName("HugeValue")
        self.v_big.setWordWrap(True)
        v.addWidget(self.v_big)

        self.v_answer = QLabel("")
        self.v_answer.setWordWrap(True)
        self.v_answer.setObjectName("Muted")
        v.addWidget(self.v_answer)

        # ---- 执行台账：把"界面建议"落成"已执行的持仓变更" ----
        # 本程序无券商接口，唯一"知道持仓"的入口是 config 的 assets[].shares。
        # 这里给一个按钮，让用户点一次就把建议落到持仓上，并留一条带日期的记录，
        # 于是同一条建议不会再天天重复出现。
        self.v_ledger = QLabel("")
        self.v_ledger.setWordWrap(True)
        self.v_ledger.setObjectName("Muted")
        v.addWidget(self.v_ledger)

        self.v_exec_row = QWidget()
        _er = QHBoxLayout(self.v_exec_row)
        _er.setContentsMargins(0, 0, 0, 0)
        self.v_exec_btn = QPushButton("我已按此执行（更新持仓与现金）")
        self.v_exec_btn.setToolTip(
            "本程序没有券商接口，不知道你是否真的成交。\n"
            "点此按钮会按上面的建议改动本地持仓份额与现金，并记一条台账，\n"
            "于是这条建议不会再重复出现。\n"
            "不会自动下单，也不会联网。")
        self.v_exec_btn.clicked.connect(self._on_execute)
        _er.addWidget(self.v_exec_btn)
        _er.addStretch(1)
        v.addWidget(self.v_exec_row)
        self.v_exec_row.setVisible(False)

        # ---- 指令栏：你主动对系统「说话」的渠道 ----
        # 上面那个按钮只接受「程序自己那条建议」；这里让你用一句话表达任何意图：
        # 卖/买、改现金/总资产、改上限、改目标、或者「忽略/备注」。
        # 解析失败会明说「没听懂」并给示例，绝不乱猜。
        self.cmd_hint = QLabel(
            "也可以用一句话告诉它（例：卖黄金800@8.62　现金20万　上限黄金6%　忽略）")
        self.cmd_hint.setObjectName("Muted")
        self.cmd_hint.setWordWrap(True)
        v.addWidget(self.cmd_hint)

        self.cmd_row = QWidget()
        _cr = QHBoxLayout(self.cmd_row)
        _cr.setContentsMargins(0, 0, 0, 0)
        self.cmd_edit = QLineEdit()
        self.cmd_edit.setPlaceholderText(
            "卖黄金800@8.62　·　买纳指500　·　现金20万　·　上限黄金6%　·　忽略")
        self.cmd_edit.setToolTip(
            "支持：卖出/买入（写「@成交价」会同时登记现金）、现金/总资产、\n"
            "黄金上限、目标配置（债/股/金）、忽略、备注。\n"
            "回车或点「执行」；本程序不会自动下单，只改本地记录。")
        self.cmd_edit.returnPressed.connect(self._on_command)
        self.cmd_btn = QPushButton("执行")
        self.cmd_btn.clicked.connect(self._on_command)
        _cr.addWidget(self.cmd_edit, 1)
        _cr.addWidget(self.cmd_btn)
        v.addWidget(self.cmd_row)

        g = QGridLayout()
        g.setSpacing(10)
        self.v_t_amt = Tile("今天投入", "—")
        self.v_t_name = Tile("买哪只", "—")
        self.v_t_prob30 = Tile("组合未来30日盈利概率", "—")
        self.v_t_prob = Tile("组合未来60日历史概率（保守口径）", "—")
        self.v_t_prob90 = Tile("组合未来90日盈利概率", "—")
        for i, t in enumerate((self.v_t_amt, self.v_t_name,
                               self.v_t_prob30, self.v_t_prob, self.v_t_prob90)):
            g.addWidget(t, 0, i)
        v.addLayout(g)

        self.v_why = QLabel("")
        self.v_why.setWordWrap(True)
        v.addWidget(self.v_why)

        self.v_next = QLabel("")
        self.v_next.setWordWrap(True)
        v.addWidget(self.v_next)

        self.root.addWidget(self.card_verdict)

    # ==================================================================
    # 我的持仓（实时）：改份额 → 立即重算权重与逐只判断
    # ==================================================================
    def _build_holdings(self) -> None:
        self.card_hold = Card("我的持仓（实时判断）")
        hint = QLabel(
            "直接改下面任意一格的「持有份额」，权重与逐只判断会立即重算"
            "（不联网、不重跑模型）——这就是你「现在的真实持仓」。\n"
            "改完点「保存为新持仓」才会写入配置；未保存的改动只用于本次判断。")
        hint.setObjectName("Muted")
        hint.setWordWrap(True)
        self.card_hold.add(hint)

        # 价格口径（把「判断用的日线收盘价」与「盘中参考价」明确分开）
        self.hold_price = QLabel("")
        self.hold_price.setObjectName("Muted")
        self.hold_price.setWordWrap(True)
        self.card_hold.add(self.hold_price)

        self.tbl_hold = QTableWidget()
        self.tbl_hold.setColumnCount(9)
        self.tbl_hold.setHorizontalHeaderLabels(
            ["名称", "类别", "持有份额", "现价(日线收盘)", "参考现价(盘中)",
             "市值(元)", "当前权重%", "目标权重%", "判断"])
        self.tbl_hold.verticalHeader().setVisible(False)
        self.tbl_hold.setMinimumHeight(200)
        self.tbl_hold.itemChanged.connect(self._on_share_edited)
        self.card_hold.add(self.tbl_hold)

        self.hold_sum = QLabel("—")
        self.hold_sum.setWordWrap(True)
        self.card_hold.add(self.hold_sum)

        self.hold_perm = QLabel("")
        self.hold_perm.setObjectName("Muted")
        self.hold_perm.setWordWrap(True)
        self.card_hold.add(self.hold_perm)

        row = QHBoxLayout()
        self.hold_save = QPushButton("保存为新持仓")
        self.hold_save.setToolTip(
            "把当前表格里的份额写入 config/config.json（写前自动备份）。\n"
            "本程序不会自动下单，也不会联网。")
        self.hold_save.clicked.connect(self._on_save_holdings)
        self.hold_reset = QPushButton("放弃改动")
        self.hold_reset.setToolTip("丢弃未保存的份额改动，恢复为配置里的当前持仓。")
        self.hold_reset.clicked.connect(lambda: self._fill_holdings(self.snap))
        row.addWidget(self.hold_save)
        row.addWidget(self.hold_reset)
        row.addStretch(1)
        self.card_hold.add_layout(row)
        self.hold_save.setEnabled(False)
        self.hold_reset.setEnabled(False)

        self.root.addWidget(self.card_hold)

    def _ro_cell(self, i: int, j: int, text) -> None:
        """写入一个只读单元格。"""
        it = QTableWidgetItem("" if text is None else str(text))
        it.setFlags(it.flags() & ~Qt.ItemIsEditable)
        self.tbl_hold.setItem(i, j, it)

    def _upd_cell(self, i: int, j: int, text) -> None:
        """更新已有单元格文字（保持只读）。"""
        it = self.tbl_hold.item(i, j)
        if it is None:
            it = QTableWidgetItem()
            it.setFlags(it.flags() & ~Qt.ItemIsEditable)
            self.tbl_hold.setItem(i, j, it)
        it.setText("" if text is None else str(text))

    def _fill_holdings(self, snap=None) -> None:
        from core import live_holdings as lh
        snap = snap if snap is not None else self.snap
        self._hold_loading = True
        try:
            if not snap or not snap.get("ok"):
                self.tbl_hold.setRowCount(0)
                self._hold_row_ids = []
                self.hold_sum.setText("暂无数据，无法展示持仓。")
                self.hold_perm.setText("")
                self.hold_price.setText("")
                return
            rows = lh.table_rows(snap)
            self._hold_row_ids = [r.get("id") for r in rows]
            self.tbl_hold.setRowCount(len(rows))
            self.hold_price.setText("价格口径：" + lh.price_caption(snap))
            cls_label = {"bond": "债券", "stock": "股票", "gold": "黄金"}
            for i, r in enumerate(rows):
                self._ro_cell(i, 0, r.get("name"))
                self._ro_cell(i, 1, cls_label.get(r.get("asset_class"),
                                                 r.get("asset_class")))
                it = QTableWidgetItem(f"{r.get('shares') or 0:,.0f}")
                it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                it.setFlags(it.flags() | Qt.ItemIsEditable)
                self.tbl_hold.setItem(i, 2, it)
                self._ro_cell(i, 3, _fmt(r.get("price"), 3))
                # 参考现价（盘中）：只显示，绝不参与判断
                rp, rpc = r.get("ref_price"), r.get("ref_change_pct")
                ref_txt = _fmt(rp, 3) if rp is not None else "—"
                if rp is not None and rpc is not None:
                    try:
                        ref_txt = f"{_fmt(rp, 3)}  ({float(rpc):+.2f}%)"
                    except (TypeError, ValueError):
                        pass
                self._ro_cell(i, 4, ref_txt)
                self._ro_cell(i, 5, f"{r.get('market_value') or 0:,.0f}")
                self._ro_cell(i, 6, _fmt(r.get("weight_pct"), 2))
                self._ro_cell(i, 7, _fmt(r.get("target_pct"), 2))
                self._ro_cell(i, 8, "")
            self.tbl_hold.resizeColumnsToContents()
            self._recompute_holdings()
        finally:
            self._hold_loading = False
        self._hold_dirty = False
        self.hold_save.setEnabled(False)
        self.hold_reset.setEnabled(False)

    def _on_share_edited(self, item) -> None:
        """用户在「持有份额」列改了一格 → 立即重算判断。"""
        if self._hold_loading or item is None or item.column() != 2:
            return
        self._hold_dirty = True
        self.hold_save.setEnabled(True)
        self.hold_reset.setEnabled(True)
        self._recompute_holdings()

    def _read_table_shares(self) -> dict:
        """从表格读回份额；非法或空值保留配置原值，绝不因输入错误而崩。"""
        out = {}
        for i, aid in enumerate(self._hold_row_ids):
            it = self.tbl_hold.item(i, 2)
            txt = (it.text().strip() if it else "").replace(",", "").replace("，", "")
            if txt == "":
                out[aid] = 0.0
                continue
            try:
                out[aid] = max(0.0, float(txt))
            except ValueError:
                pass
        return out

    def _recompute_holdings(self) -> None:
        from core import live_holdings as lh
        snap = self.snap
        if not snap or not snap.get("ok"):
            return
        res = lh.recompute(snap, self._read_table_shares(), self.cfg)
        if not res.get("ok"):
            self.hold_sum.setText(res.get("message") or "无法重算")
            return
        pal = palette()
        self._hold_loading = True
        try:
            for i, r in enumerate(res["rows"]):
                self._upd_cell(i, 5, f"{r['market_value']:,.0f}")
                self._upd_cell(i, 6, f"{r['weight_pct']:.2f}")
                self._upd_cell(i, 7, f"{r['target_pct']:.2f}")
                act = r.get("action") or "持有"
                self._upd_cell(i, 8, f"{act}（{r['deviation_pct']:+.2f}pp）")
                jt = self.tbl_hold.item(i, 8)
                if jt is not None:
                    if "加仓" in act:
                        jt.setForeground(QColor(pal["up"]))
                    elif "减仓" in act:
                        jt.setForeground(QColor(pal["down"]))
                    else:
                        jt.setForeground(QColor(pal["muted"]))
        finally:
            self._hold_loading = False
        self.hold_sum.setText(lh.summary_text(res))
        self.hold_sum.setStyleSheet(
            f"color: {pal['danger']};" if res.get("gold_over") else "")
        self._render_hold_perm()

    def _render_hold_perm(self) -> None:
        pk = ((self.snap or {}).get("portfolio") or {}).get("plan") or {}
        hl = ((pk.get("home") or {}).get("headline")) or {}
        txt = "今日买入权限（模型级，取决于当日市场状态，不随份额变）："
        if hl.get("permission_blocked"):
            bp = hl.get("buy_permission") or {}
            txt += f"{bp.get('trust_level') or '研究级'} —— 不开放买入，今天不实际买入。"
        else:
            txt += "见首页结论卡。"
        txt += " 本面板只做「再平衡级」判断：加 / 减 / 持有。"
        self.hold_perm.setText(txt)

    def _on_save_holdings(self) -> None:
        """把表格里的份额写入配置（写前备份、二次确认、绝不联网下单）。"""
        from core import live_holdings as lh
        snap = self.snap
        if not snap or not snap.get("ok"):
            QMessageBox.information(self, "暂无数据", "还没有可用持仓数据。")
            return
        rows = lh.table_rows(snap)
        new = self._read_table_shares()
        lines = []
        for r in rows:
            b = float(r.get("shares") or 0.0)
            a = float(new.get(r.get("id"), b))
            if abs(a - b) > 1e-9:
                lines.append(f"· {r.get('name')}：{b:,.0f} → {a:,.0f} 份")
        if not lines:
            QMessageBox.information(self, "无改动", "份额没有变化。")
            return
        ok = QMessageBox.question(
            self, "确认保存持仓",
            "将把以下份额写入本地配置（写前自动备份）：\n\n" + "\n".join(lines)
            + "\n\n本程序不会自动下单，也不会联网。是否继续？")
        if ok != QMessageBox.Yes:
            return
        try:
            cfg = self.cfg
            assets = [dict(a) for a in cfg.assets]
            by_id = {str(a.get("id")): a for a in assets}
            for aid, sh in new.items():
                if str(aid) in by_id:
                    by_id[str(aid)]["shares"] = float(sh)
            cfg.backup()
            cfg.set_assets(assets)
            cfg.save()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "保存失败", f"{type(exc).__name__}: {exc}")
            return
        try:
            from core import execution_ledger as led
            led.record_event("SET", note=f"手动更新持仓份额（{len(lines)} 项）")
        except Exception:  # noqa: BLE001
            pass
        self._hold_dirty = False
        self.hold_save.setEnabled(False)
        self.hold_reset.setEnabled(False)
        QMessageBox.information(self, "已保存",
                                "持仓已写入 config/config.json，正在重新计算。")
        self.config_changed.emit()

    # ==================================================================
    # 顶部：总资产
    # ==================================================================
    def _build_summary(self) -> None:
        self.card_title = Card("我的全天候 ETF 组合")
        self.lbl_total = QLabel("—")
        self.lbl_total.setObjectName("BigValue")
        self.card_title.add(self.lbl_total)
        self.lbl_summary = QLabel("")
        self.lbl_summary.setWordWrap(True)
        self.lbl_summary.setObjectName("Muted")
        self.card_title.add(self.lbl_summary)
        self.root.addWidget(self.card_title)

    # ==================================================================
    # 新手模式
    # ==================================================================
    def _build_simple(self) -> None:
        self.simple_box = QWidget()
        lay = QVBoxLayout(self.simple_box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)

        # 新手模式只保留 4 张卡：概率 / 买不买对比 / 仓位 / 黄金。
        # "今天怎么做、买什么、买多少、什么时候买"已全部在第一屏结论卡里，
        # 这里只补充"为什么"和"现状"，避免参数堆叠。
        self.s_prob_card = Card("【组合未来盈利概率（30 / 60 / 90 日）】")
        self.s_prob = QLabel("—")
        self.s_prob.setObjectName("BigValue")
        self.s_prob_card.add(self.s_prob)
        self.s_prob_sub = QLabel("")
        self.s_prob_sub.setWordWrap(True)
        self.s_prob_sub.setObjectName("Muted")
        self.s_prob_card.add(self.s_prob_sub)
        lay.addWidget(self.s_prob_card)

        # 交易前后对比（需求 45/50/51）：什么都不买 vs 执行建议
        self.s_cmp_card = Card("【如果什么都不买 / 如果执行建议】")
        self.s_cmp_no = QLabel("—")
        self.s_cmp_no.setWordWrap(True)
        self.s_cmp_card.add(self.s_cmp_no)
        self.s_cmp_yes = QLabel("")
        self.s_cmp_yes.setWordWrap(True)
        self.s_cmp_card.add(self.s_cmp_yes)
        self.s_cmp_verdict = QLabel("")
        self.s_cmp_verdict.setWordWrap(True)
        self.s_cmp_card.add(self.s_cmp_verdict)
        lay.addWidget(self.s_cmp_card)

        self.s_pos_card = Card("【实际仓位 vs 战略目标】")
        self.s_pos = QLabel("—")
        self.s_pos.setWordWrap(True)
        self.s_pos_card.add(self.s_pos)
        lay.addWidget(self.s_pos_card)

        self.s_gold_card = Card("【黄金】")
        self.s_gold = QLabel("—")
        self.s_gold.setWordWrap(True)
        self.s_gold_card.add(self.s_gold)
        lay.addWidget(self.s_gold_card)

        self.simple_box.setVisible(False)
        self.root.addWidget(self.simple_box)

    # ==================================================================
    # 专业模式（完整首页）
    # ==================================================================
    def _build_full(self) -> None:
        self.full_box = QWidget()
        lay = QVBoxLayout(self.full_box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        self._build_today(lay)
        self._build_pick(lay)
        self._build_compare(lay)
        self._build_prob(lay)
        self._build_position(lay)
        self._build_issues(lay)
        self._build_next(lay)
        self._build_cash(lay)
        self._build_nono(lay)
        self._build_etf_table(lay)
        self._build_batch(lay)
        self._build_conclusion(lay)
        self.root.addWidget(self.full_box)

    # ---------------------------------------------------------------- 今天
    def _build_today(self, lay) -> None:
        self.card_today = Card("【详细判断：7 个状态 + 三个独立问题】")
        self.lbl_today = QLabel("—")
        self.lbl_today.setObjectName("MidValue")
        self.card_today.add(self.lbl_today)
        self.lbl_today_sub = QLabel("")
        self.lbl_today_sub.setWordWrap(True)
        self.lbl_today_sub.setObjectName("Muted")
        self.card_today.add(self.lbl_today_sub)

        self.grid_q = QGridLayout()
        self.grid_q.setSpacing(9)
        self.card_today.add_layout(self.grid_q)
        lay.addWidget(self.card_today)

        self.card_warn = Card("数据一致性检查")
        self.lbl_cons = QLabel("—")
        self.lbl_cons.setWordWrap(True)
        self.card_warn.add(self.lbl_cons)
        self.card_warn.setVisible(False)
        lay.addWidget(self.card_warn)

    # ---------------------------------------------------------------- 概率
    def _build_prob(self, lay) -> None:
        self.card_prob = Card("【整个组合未来历史盈利概率】")
        self.card_prob.setToolTip(
            "概率怎么来的（需求廿三/廿四）：\n"
            "· 用一套因果特征（趋势 / 位置 / 波动 / 回撤 / 相关性等）刻画『今天』的市场状态；\n"
            "· 在历史上找最相似的若干交易日（kNN，z-score 加权距离），统计它们之后\n"
            "  20 / 30 / 60 / 90 / 120 日的真实收益分布；\n"
            "· 只用当天及以前已发生的数据（无未来函数），并做样本外（Walk-Forward）验证；\n"
            "· 优先显示样本外胜率；样本外不可用时明确写『样本外统计尚不可用』并下调信心；\n"
            "· 样本可信度分级：<50 低 / 50-199 中 / ≥200 高；置信区间越宽，可信度越低。\n"
            "概率只是历史条件频率，不代表未来实际收益。")
        self.prob_grid = QGridLayout()
        self.prob_grid.setSpacing(9)
        self.card_prob.add_layout(self.prob_grid)
        self.lbl_prob_note = QLabel("")
        self.lbl_prob_note.setWordWrap(True)
        self.lbl_prob_note.setObjectName("Muted")
        self.card_prob.add(self.lbl_prob_note)
        lay.addWidget(self.card_prob)

    # ---------------------------------------------------------------- 仓位
    def _build_position(self, lay) -> None:
        w = QWidget()
        g = QGridLayout(w)
        g.setContentsMargins(0, 0, 0, 0)
        g.setSpacing(10)
        self.card_pos = Card("【当前实际仓位】")
        self.tbl_pos = DataTable()
        self.card_pos.add(self.tbl_pos)
        self.card_tgt = Card("【战略目标】")
        self.tbl_tgt = DataTable()
        self.card_tgt.add(self.tbl_tgt)
        g.addWidget(self.card_pos, 0, 0)
        g.addWidget(self.card_tgt, 0, 1)
        g.setColumnStretch(0, 3)
        g.setColumnStretch(1, 2)
        lay.addWidget(w)

    # ---------------------------------------------------------------- 问题
    def _build_issues(self, lay) -> None:
        self.card_issues = Card("【当前最重要的问题】")
        self.issues_grid = QGridLayout()
        self.issues_grid.setSpacing(9)
        self.card_issues.add_layout(self.issues_grid)
        lay.addWidget(self.card_issues)

    # ---------------------------------------------------------------- 下一次
    def _build_next(self, lay) -> None:
        self.card_next = Card("【下一次买入条件】")
        self.tbl_next = DataTable()
        self.card_next.add(self.tbl_next)
        self.lbl_next_focus = QLabel("")
        self.lbl_next_focus.setWordWrap(True)
        self.card_next.add(self.lbl_next_focus)
        lay.addWidget(self.card_next)

    # ---------------------------------------------------------------- 今天买什么
    def _build_pick(self, lay) -> None:
        self.card_pick = Card("【今天最值得配置】")
        self.tbl_pick = DataTable()
        self.card_pick.add(self.tbl_pick)
        self.lbl_pick_note = QLabel("")
        self.lbl_pick_note.setWordWrap(True)
        self.lbl_pick_note.setObjectName("Muted")
        self.card_pick.add(self.lbl_pick_note)
        lay.addWidget(self.card_pick)

    # ------------------------------------------------ 交易前后对比（§50/§51）
    def _build_compare(self, lay) -> None:
        self.card_cmp = Card("【今天买不买：交易前后组合对比】")
        self.tbl_cmp = DataTable()
        self.card_cmp.add(self.tbl_cmp)
        self.lbl_cmp_verdict = QLabel("")
        self.lbl_cmp_verdict.setWordWrap(True)
        self.card_cmp.add(self.lbl_cmp_verdict)
        lay.addWidget(self.card_cmp)

    # ---------------------------------------------------------------- 现金
    def _build_cash(self, lay) -> None:
        self.card_cash = Card("【当前待投资资金】")
        self.cash_grid = QGridLayout()
        self.cash_grid.setSpacing(9)
        self.card_cash.add_layout(self.cash_grid)
        self.lbl_cash_advice = QLabel("")
        self.lbl_cash_advice.setWordWrap(True)
        self.card_cash.add(self.lbl_cash_advice)
        lay.addWidget(self.card_cash)

    # ---------------------------------------------------------------- 不要追涨/抄底
    def _build_nono(self, lay) -> None:
        w = QWidget()
        g = QGridLayout(w)
        g.setContentsMargins(0, 0, 0, 0)
        g.setSpacing(10)
        self.card_nochase = Card("【今天不要追涨】")
        self.lbl_nochase = QLabel("无")
        self.lbl_nochase.setWordWrap(True)
        self.card_nochase.add(self.lbl_nochase)
        self.card_nobottom = Card("【今天不要抄底】")
        self.lbl_nobottom = QLabel("无")
        self.lbl_nobottom.setWordWrap(True)
        self.card_nobottom.add(self.lbl_nobottom)
        g.addWidget(self.card_nochase, 0, 0)
        g.addWidget(self.card_nobottom, 0, 1)
        lay.addWidget(w)

    # ---------------------------------------------------------------- ETF 表格
    def _build_etf_table(self, lay) -> None:
        self.card_etf = Card("【今天各 ETF 怎么处理】")
        self.tbl_etf = DataTable()
        self.card_etf.add(self.tbl_etf)
        self.card_etf.add(QLabel("说明：『买入质量』= A/B/C/D/E，是综合概率、期望、"
                                 "盈亏比、回撤、技术确认后的结论，不是胜率。"))
        lay.addWidget(self.card_etf)

    # ---------------------------------------------------------------- 四批
    def _build_batch(self, lay) -> None:
        self.card_batch = Card("【四批资金进度】")
        self.tbl_batch = DataTable()
        self.card_batch.add(self.tbl_batch)
        self.lbl_batch_note = QLabel("")
        self.lbl_batch_note.setWordWrap(True)
        self.lbl_batch_note.setObjectName("Muted")
        self.card_batch.add(self.lbl_batch_note)
        lay.addWidget(self.card_batch)

    # ---------------------------------------------------------------- 结论
    def _build_conclusion(self, lay) -> None:
        self.card_concl = Card("【今日最终结论】")
        self.lbl_concl = QLabel("—")
        self.lbl_concl.setWordWrap(True)
        self.lbl_concl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.card_concl.add(self.lbl_concl)
        self.lbl_warn = QLabel("")
        self.lbl_warn.setWordWrap(True)
        self.card_concl.add(self.lbl_warn)
        lay.addWidget(self.card_concl)

    # ==================================================================
    # 专业模式标签页
    # ==================================================================
    def _build_tabs(self) -> None:
        self.tabs = QTabWidget()
        self.root.addWidget(self.tabs)
        self._tab_stats()
        self._tab_corr()
        self._tab_risk()
        self._tab_rebal()
        self._tab_marginal()
        self._tab_macro()
        self._tab_backtest()

    def _tab_stats(self) -> None:
        w = QWidget()
        lay = QVBoxLayout(w)
        top = QHBoxLayout()
        top.addWidget(QLabel("图表区间"))
        self.cmb_period = QComboBox()
        self.cmb_period.addItems([p[0] for p in PERIODS])
        self.cmb_period.setCurrentIndex(3)
        self.cmb_period.currentIndexChanged.connect(self._redraw_charts)
        top.addWidget(self.cmb_period)
        top.addStretch(1)
        lay.addLayout(top)
        self.tbl_hist = DataTable()
        lay.addWidget(self._titled("组合未来历史统计（相似状态法）", self.tbl_hist))
        self.tbl_stats = DataTable()
        lay.addWidget(self._titled("组合风险与收益统计", self.tbl_stats))
        self.tbl_yearly = DataTable()
        lay.addWidget(self._titled("年度收益", self.tbl_yearly))
        self.chart_nav = LineChart("组合净值（当前权重，按日复利）", 240)
        lay.addWidget(self.chart_nav)
        self.chart_dd = LineChart("组合回撤（%）", 200)
        lay.addWidget(self.chart_dd)
        self.chart_risk = LineChart("组合风险评分（0-100）", 190)
        lay.addWidget(self.chart_risk)
        self.tabs.addTab(w, "组合统计")

    def _tab_corr(self) -> None:
        w = QWidget()
        lay = QVBoxLayout(w)
        top = QHBoxLayout()
        top.addWidget(QLabel("窗口"))
        self.cmb_corr = QComboBox()
        self.cmb_corr.addItems(["20d", "60d", "120d", "1y"])
        self.cmb_corr.setCurrentIndex(1)
        self.cmb_corr.currentIndexChanged.connect(self._redraw_corr)
        top.addWidget(self.cmb_corr)
        top.addStretch(1)
        lay.addLayout(top)
        self.heat = HeatmapTable()
        self.heat.setMinimumHeight(260)
        lay.addWidget(self.heat)
        self.tbl_corr = DataTable()
        lay.addWidget(self._titled("相关性摘要", self.tbl_corr))
        self.tabs.addTab(w, "相关性")

    def _tab_risk(self) -> None:
        w = QWidget()
        lay = QVBoxLayout(w)
        self.tbl_rc = DataTable()
        lay.addWidget(self._titled("组合风险贡献（仓位贡献 vs 风险贡献）", self.tbl_rc))
        self.chart_rc = BarChart("风险贡献对比（实心=风险贡献，虚线=仓位贡献）", 240)
        lay.addWidget(self.chart_rc)
        self.tbl_vol = DataTable()
        lay.addWidget(self._titled("各资产波动率", self.tbl_vol))
        self.tabs.addTab(w, "风险贡献")

    def _tab_rebal(self) -> None:
        w = QWidget()
        lay = QVBoxLayout(w)
        self.lbl_rebal_head = QLabel("—")
        self.lbl_rebal_head.setWordWrap(True)
        lay.addWidget(self.lbl_rebal_head)
        self.tbl_rebal = DataTable()
        lay.addWidget(self._titled("权重偏离与再平衡建议", self.tbl_rebal))
        self.tbl_rebsim = DataTable()
        lay.addWidget(self._titled("再平衡模拟（黄金降到各档位后整个组合会怎样）",
                                   self.tbl_rebsim))
        self.tbl_target = DataTable()
        lay.addWidget(self._titled("战略目标 / 今日战术目标 / 当前仓位", self.tbl_target))
        self.lbl_opt_note = QLabel("")
        self.lbl_opt_note.setWordWrap(True)
        lay.addWidget(self.lbl_opt_note)
        self.tbl_wait = DataTable()
        lay.addWidget(self._titled("等待资金与风险资产缺口", self.tbl_wait))
        self.tabs.addTab(w, "再平衡与配置")

    def _tab_marginal(self) -> None:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(QLabel(
            "边际组合分析：把『第一批资金』从现金换入某只 ETF 之后，用同一套历史相似"
            "状态统计重新计算整个组合的概率、期望、回撤与波动——用来回答"
            "『买这个 ETF，对整个组合到底有没有好处』。"))
        self.tbl_marginal = DataTable()
        lay.addWidget(self.tbl_marginal)
        self.lbl_marginal_note = QLabel("")
        self.lbl_marginal_note.setWordWrap(True)
        self.lbl_marginal_note.setObjectName("Muted")
        lay.addWidget(self.lbl_marginal_note)
        self.tabs.addTab(w, "边际组合分析")

    def _tab_macro(self) -> None:
        w = QWidget()
        lay = QVBoxLayout(w)
        self.tbl_radar = DataTable()
        lay.addWidget(self._titled("黄金底部雷达（8 项条件）", self.tbl_radar))
        self.tbl_macro = DataTable()
        lay.addWidget(self._titled("宏观与黄金指标", self.tbl_macro))
        self.chart_gold = LineChart("国际金价", 200)
        lay.addWidget(self.chart_gold)
        self.chart_bpgdm = LineChart("BPGDM 金矿股广度（真实值优先；代理时标注非真实）", 200)
        lay.addWidget(self.chart_bpgdm)
        self.chart_dxy = LineChart("美元指数 DXY", 180)
        lay.addWidget(self.chart_dxy)
        self.chart_real = LineChart("美国10年期实际利率（%）", 180)
        lay.addWidget(self.chart_real)
        self.tabs.addTab(w, "黄金与宏观")

    def _tab_backtest(self) -> None:
        w = QWidget()
        lay = QVBoxLayout(w)
        row = QHBoxLayout()
        self.btn_bt = QPushButton("运行组合回测（含 Walk-Forward 样本外验证）")
        self.btn_bt.setObjectName("Primary")
        self.btn_bt.clicked.connect(self._run_backtest)
        row.addWidget(self.btn_bt)
        self.lbl_bt = QLabel("回测会重新加载历史数据并逐日模拟，通常需要 1-3 分钟。")
        self.lbl_bt.setObjectName("Muted")
        self.lbl_bt.setWordWrap(True)
        row.addWidget(self.lbl_bt, 1)
        lay.addLayout(row)
        self.bt_text = QPlainTextEdit()
        self.bt_text.setReadOnly(True)
        self.bt_text.setMinimumHeight(420)
        self.bt_text.setPlaceholderText("点击上方按钮运行回测，结果会显示在这里。")
        lay.addWidget(self.bt_text)
        self.tabs.addTab(w, "历史回测")

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

    # ==================================================================
    def set_loading(self, loading: bool) -> None:
        self.lbl_loading.setVisible(loading)

    def set_progress(self, msg: str) -> None:
        self.lbl_loading.setText(f"{msg}\n（数据源不可达时会自动跳过并标注『暂无数据』）")
        self.lbl_loading.setVisible(True)

    def set_simple(self, simple: bool) -> None:
        """切换新手模式 / 专业模式。"""
        self.simple = bool(simple)
        self.simple_box.setVisible(self.simple)
        self.full_box.setVisible(not self.simple)
        self.tabs.setVisible(not self.simple)
        self.card_title.setVisible(True)
        if self.snap:
            self.set_snapshot(self.snap)

    def refresh_theme(self) -> None:
        if self.snap:
            self.set_snapshot(self.snap)

    # ==================================================================
    def set_snapshot(self, snap: dict) -> None:
        self.snap = snap
        self.lbl_loading.setVisible(False)
        if not snap or not snap.get("ok"):
            self.lbl_total.setText("暂无数据")
            self.lbl_summary.setText(
                "数据不足，无法生成组合判断。\n" +
                "\n".join(snap.get("warnings", []) if snap else []))
            self._fill_verdict({})
            self._fill_holdings(snap)
            return
        p = snap["portfolio"]
        self._fill_title(snap, p)
        pk = p.get("plan") or {}
        self._fill_verdict(pk)
        self._fill_holdings(snap)
        self._fill_simple(snap, p, pk)
        self._fill_today(snap, p, pk)
        self._fill_prob(snap, p, pk)
        self._fill_position(snap, p, pk)
        self._fill_issues(snap, p, pk)
        self._fill_next(snap, p, pk)
        self._fill_pick(snap, p, pk)
        self._fill_compare(snap, p, pk)
        self._fill_cash(snap, p, pk)
        self._fill_nono(snap, p, pk)
        self._fill_etf_table(snap, p, pk)
        self._fill_batch(snap, p, pk)
        self._fill_conclusion(snap, p, pk)
        self._fill_marginal(snap, p, pk)
        self._fill_stats(snap, p)
        self._fill_corr(p)
        self._fill_risk(p)
        self._fill_rebal(p)
        self._fill_macro(snap)
        self._redraw_charts()

    # ---------------------------------------------------------------- 标题
    def _fill_title(self, snap, p) -> None:
        w = p["weights"]
        self.lbl_total.setText(f"总资产 {_money(w.get('total_assets'))}")
        # 各数据源各自的数据日期（验收 §34：不得统一写「更新时间：今天」）
        macro = snap.get("macro") or {}
        parts = []
        etf_dates = [r.get("snapshot", {}).get("data_time")
                     for r in (snap.get("rows") or []) if r.get("snapshot")]
        etf_dates = [d for d in etf_dates if d]
        if etf_dates:
            parts.append(f"A股ETF数据 {max(etf_dates)}")
        gm = macro.get("gold_metrics") or {}
        if gm.get("data_time"):
            parts.append(f"黄金 {gm['data_time']}")
        um = macro.get("usd_metrics") or {}
        if um.get("data_time"):
            parts.append(f"DXY {um['data_time']}")
        rm = macro.get("real_rate_metrics") or {}
        if rm.get("data_time"):
            parts.append(f"10Y实际利率 {rm['data_time']}")
        self.lbl_summary.setText(
            f"组合：{p.get('portfolio_name') or '当前组合'}（{p.get('portfolio_id') or '-'}）"
            f" · ETF 市值 {_money(w.get('etf_value'))}"
            f" · 待投资现金 {_money(w.get('cash_value'))}"
            f" · 获取时间 {snap['ts']:%Y-%m-%d %H:%M:%S}"
            f" · 计算耗时 {snap.get('elapsed_sec')}s\n"
            + ("数据日期：" + " · ".join(parts) + "\n" if parts else "")
            + f"口径：{p['weights_source']}")

    # ---------------------------------------------------------------- 第一屏结论
    def _render_stale_banner(self) -> None:
        """首屏「数据陈旧」横幅：数据日期 + 陈旧天数 + 明确「非实时」。

        文案由 core.freshness_gate.banner_text 统一生成（新鲜时返回空串），
        保证 GUI 与 CLI 口径一致、且可被自检直接覆盖。
        """
        from core.freshness_gate import banner_text
        text = banner_text((self.snap or {}).get("freshness") or {})
        if not text:
            self.v_stale.setVisible(False)
            return
        pal = palette()
        self.v_stale.setText(text)
        self.v_stale.setStyleSheet(
            f"color: {pal['danger']}; font-weight: 700;"
            f" background: {pal['card']}; border: 2px solid {pal['danger']};"
            " border-radius: 8px; padding: 8px 10px;")
        self.v_stale.setVisible(True)

    def _fill_verdict(self, pk) -> None:
        """把 headline 渲染成第一屏：一句话 + 三个关键数字 + ≤3 条理由。"""
        self._render_stale_banner()
        pal = palette()
        hl = ((pk.get("home") or {}).get("headline")) or {}
        if not hl:
            self.v_big.setText("数据不足，暂无法给出今天的结论")
            self.v_big.setStyleSheet(f"color: {pal['muted']};")
            self.v_answer.setText("")
            for t in (self.v_t_amt, self.v_t_name,
                      self.v_t_prob30, self.v_t_prob, self.v_t_prob90):
                t.set("暂无数据", "")
            self.v_why.setText("")
            self.v_next.setText("")
            if hasattr(self, "v_ledger"):
                self.v_ledger.setVisible(False)
                self.v_exec_row.setVisible(False)
            self._exec_payload = None
            return

        color = pal.get(hl.get("color") or "muted", pal["text"])
        self.v_big.setText(str(hl.get("big") or "—"))
        self.v_big.setStyleSheet(f"color: {color};")

        badge = {
            "GREEN_BUY": "结论：今天就该分批买入，别犹豫也别一次打满。",
            "GREEN_PROBE": "结论：可以小仓试探，先买一点点试试。",
            "YELLOW_HOLD": "结论：今天不买，先拿着手里的。",
            "ORANGE_PAUSE": "结论：今天暂停加仓（当前不开放买入权限）。",
            "BLUE_REBALANCE": "结论：今天只做再平衡，不加风险资产。",
            "RED_DEFEND": "结论：今天别买，以防守为主。",
            "WHITE_WAIT": "结论：今天按兵不动，继续等更好的机会。",
        }.get(hl.get("key"), "结论：今天维持现状。")
        # ---- 【BUY PERMISSION GATE】可信等级决定买入权限（不是买入等级）----
        if hl.get("permission_blocked"):
            _bp = hl.get("buy_permission") or {}
            _lvl = _bp.get("trust_level") or "研究级"
            _rr = hl.get("research_reference")
            badge = f"结论：{_lvl} —— 当前不开放买入权限，因此今天不执行实际买入。"
            if _rr:
                badge += (f"\n研究参考：模型模拟买入 {_rr.get('name')} "
                          f"{int(_rr.get('amount') or 0):,} 元后组合质量最高"
                          "（仅研究，不转换成实际买入）。")
        self.v_answer.setText(badge)

        amount = float(hl.get("amount") or 0.0)
        if amount > 0:
            self.v_t_amt.set(_money(amount), "今天要投入的第一批金额（不会自动下单）", "up")
        else:
            self.v_t_amt.set("0 元", "今天不投入任何新增资金", "muted")
        if hl.get("name"):
            self.v_t_name.set(str(hl["name"]), str(hl.get("class_label") or ""), "up")
        else:
            self.v_t_name.set("不买", "今天没有要买的 ETF", "muted")
        p60 = hl.get("prob60")
        cons = hl.get("conservative") or {}
        if cons.get("prob") is not None:
            # ---- 保守主口径：独立事件去重后的逐日样本外（最终修正） ----
            sub_parts = []
            sub_parts.append(f"独立样本外事件 {cons.get('indep')}"
                             + (f"（逐日预测点 {cons.get('points')}，窗口重叠）"
                                if cons.get("points") else ""))
            _ci = cons.get("ci") or []
            if len(_ci) == 2 and _ci[0] is not None:
                sub_parts.append(f"95%CI {_fmt(_ci[0], 1, '%')}~{_fmt(_ci[1], 1, '%')}")
            if cons.get("base") is not None:
                sub_parts.append(f"无条件基础胜率 {_fmt(cons.get('base'), 1, '%')}")
            if cons.get("uplift") is not None:
                sub_parts.append(f"模型相对基准 {float(cons['uplift']):+.1f}pp")
            if cons.get("overlap_freq") is not None:
                sub_parts.append(f"重叠滚动OOS频率 {_fmt(cons.get('overlap_freq'), 1, '%')}"
                                 "（研究参考，不参与决策）")
            if cons.get("few_samples"):
                sub_parts.append("独立事件不足30个：样本极少，仅供研究")
            if cons.get("no_edge"):
                sub_parts.append("当前模型没有证明额外择时优势")
            self.v_t_prob.set(_fmt(cons.get("prob"), 1, "%"),
                              " · ".join(sub_parts), "warn")
        elif p60 is not None:
            base_p = hl.get("prob60_base")
            up = hl.get("prob60_uplift")
            sub_parts = []
            if base_p is not None:
                sub_parts.append(f"基础胜率 {_fmt(base_p, 1, '%')}")
            if up is not None:
                sub_parts.append(f"相对基准 {float(up):+.1f}pp"
                                 + ("（无明显择时优势）" if abs(float(up)) < 2 else ""))
            if hl.get("prob60_oos") is not None:
                sub_parts.append(f"重叠滚动OOS频率 {_fmt(hl.get('prob60_oos'), 1, '%')}"
                                 "（研究参考，不参与决策）")
            _ci = hl.get("prob60_ci") or []
            if len(_ci) == 2 and _ci[0] is not None:
                sub_parts.append(f"95%CI {_fmt(_ci[0], 1, '%')}~{_fmt(_ci[1], 1, '%')}")
            if hl.get("prob60_samples") is not None:
                sub_parts.append(f"样本 {hl.get('prob60_samples')}（历史频率，非预测能力）")
            self.v_t_prob.set(_fmt(p60, 1, "%"), " · ".join(sub_parts),
                              "up" if float(p60) >= 65 else "warn")
        else:
            self.v_t_prob.set("暂无数据", "")
        for tile, k_p, k_oos, nd in (
                (self.v_t_prob30, "prob30", "prob30_oos", 30),
                (self.v_t_prob90, "prob90", "prob90_oos", 90)):
            pv = hl.get(k_p)
            if pv is not None:
                psub = (f"重叠滚动OOS频率 {_fmt(hl.get(k_oos), 1, '%')}"
                        "（研究参考）"
                        if hl.get(k_oos) is not None else "窗口统计暂无数据")
                tile.set(_fmt(pv, 1, "%"), psub,
                         "up" if float(pv) >= 65 else "warn")
            else:
                tile.set("暂无数据", "")

        why = list(hl.get("why") or [])[:3]
        body = ""
        if why:
            body += "为什么：\n" + "\n".join(f"  {i + 1}. {w}"
                                            for i, w in enumerate(why))
        if hl.get("note"):
            body += ("\n" if body else "") + "※ " + str(hl["note"])
        _sg = _sample_gate_text(getattr(self, "snap", None), pk)
        if _sg:
            body += ("\n\n" if body else "") + _sg
        self.v_why.setText(body)

        nxt = hl.get("next") or ""
        self.v_next.setText(f"下一次：{nxt}" if nxt else "")
        self.v_next.setObjectName("Muted")

        # ---- 执行台账状态 + 「我已按此执行」按钮 ----
        self._fill_ledger(pk, hl)

    # ---------------------------------------------------------------- 执行台账
    def _fill_ledger(self, pk, hl) -> None:
        """渲染执行台账状态；有可执行建议时显示「我已按此执行」按钮。"""
        try:
            from core import execution_ledger as led
        except Exception:  # noqa: BLE001
            self.v_ledger.setVisible(False)
            self.v_exec_row.setVisible(False)
            self._exec_payload = None
            return
        rebal_sim = ((pk.get("plan") or {}).get("rebal_sim")
                     or pk.get("rebal_sim") or {})
        st = led.status_text(hl, rebal_sim)
        self.v_ledger.setText(str(st.get("label") or ""))
        self.v_ledger.setVisible(bool(st.get("label")))

        rb = rebal_sim.get("best") or {}
        aid = rebal_sim.get("gold_id") or rb.get("asset_id")
        can = (hl.get("key") == "BLUE_REBALANCE"
               and bool(rb.get("sell_shares")) and bool(aid))
        self._exec_payload = None
        if can:
            self._exec_payload = {
                "asset_id": aid,
                "name": rebal_sim.get("gold_name") or rb.get("name") or "黄金",
                "sell_shares": int(rb.get("sell_shares") or 0),
                "price": rb.get("sell_price"),
                "amount": float(rb.get("sell_amount") or 0.0),
                "target_pct": rb.get("target_pct"),
                "current_pct": rebal_sim.get("current_pct"),
            }
        self.v_exec_row.setVisible(bool(self._exec_payload))
        self.v_exec_btn.setEnabled(bool(self._exec_payload))

    def _on_execute(self) -> None:
        """用户点「我已按此执行」：确认后把建议落到持仓并写台账。"""
        p = self._exec_payload
        if not p:
            return
        from core import execution_ledger as led

        sh = int(p.get("sell_shares") or 0)
        amt = float(p.get("amount") or 0.0)
        price = p.get("price")
        cur, tgt = p.get("current_pct"), p.get("target_pct")
        price_txt = (f"　× {price} 元 ≈ {amt:,.0f} 元" if price
                     else f"　约 {amt:,.0f} 元")
        pos_txt = (f"\n当前占比 {cur}%　→　目标 {tgt}%\n" if cur is not None else "\n")
        msg = (f"确认这笔已经成交？\n\n卖出：{p.get('name')}　{sh:,} 份"
               + price_txt + pos_txt
               + "\n点『确认』后：\n"
                 f"　· 持仓份额减少 {sh:,} 份；\n"
                 "　· 现金按成交额增加；\n"
                 "　· 写一条带日期的执行台账，界面随后自动重算。\n\n"
                 "本程序不会自动下单；请确认你已在券商端真实成交。")
        if QMessageBox.warning(self, "确认已执行", msg,
                               QMessageBox.Ok | QMessageBox.Cancel,
                               QMessageBox.Cancel) != QMessageBox.Ok:
            return
        try:
            led.execute_rebalance(self.cfg, asset_id=p["asset_id"], name=p["name"],
                                  sell_shares=sh, price=price, amount=amt,
                                  target_pct=tgt, note="界面『我已按此执行』")
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "执行失败", f"未能写入持仓：{exc}")
            return
        self.v_exec_btn.setEnabled(False)
        QMessageBox.information(
            self, "已登记",
            f"已把 {p.get('name')} 减 {sh:,} 份、现金 +{amt:,.0f} 元记入持仓，"
            "并写入执行台账。\n界面将自动重新计算。")
        self.config_changed.emit()

    # ---------------------------------------------------------------- 指令栏
    def _on_command(self) -> None:
        """把用户在指令栏里的一句话解析成动作，确认后落地。"""
        from core import command_bar as cb

        text = self.cmd_edit.text().strip()
        if not text:
            return
        cmd = cb.parse(text, self.cfg)
        if cmd.action in ("EMPTY", "ERROR"):
            QMessageBox.information(
                self, "没听懂",
                f"{cmd.error}\n\n可用示例：\n"
                "　卖黄金800@8.62　（卖出 800 份，@ 后是成交价）\n"
                "　买纳指500\n"
                "　现金20万　/　总资产30万\n"
                "　上限黄金6%\n"
                "　目标 债50 股45 金5\n"
                "　忽略　/　备注 今天没空")
            return
        pv = cb.preview(cmd, self.cfg)
        if cmd.action in ("REBALANCE", "BUY", "SET_CASH", "SET_TOTAL",
                          "SET_GOLD_CAP", "SET_TARGET"):
            if QMessageBox.warning(
                    self, "确认执行",
                    pv + "\n\n（只改本机的持仓/配置记录，不会下单、不联网）",
                    QMessageBox.Ok | QMessageBox.Cancel,
                    QMessageBox.Cancel) != QMessageBox.Ok:
                return
        res = cb.apply(self.cfg, cmd)
        self.cmd_edit.clear()
        if not res.get("ok"):
            QMessageBox.critical(self, "未执行", str(res.get("message")))
            return
        QMessageBox.information(self, "已执行",
                                str(res.get("message")) + "\n\n界面将自动重新计算。")
        self.config_changed.emit()

    # ---------------------------------------------------------------- 新手模式
    def _fill_simple(self, snap, p, pk) -> None:
        pal = palette()
        prob = pk.get("prob") or {}
        gate = pk.get("gate") or {}
        gold = p["gold_control"]
        home = pk.get("home") or {}
        comp = pk.get("comparison") or {}

        cons = ((pk.get("home") or {}).get("conservative")
                or (pk.get("headline") or {}).get("conservative") or {})
        if cons.get("prob") is not None:
            self.s_prob.setText(
                f"决策主口径 60日 {cons.get('prob')}%（独立事件去重样本外）")
            self.s_prob_sub.setText(
                f"独立样本外事件 {cons.get('indep')} 个"
                + (f"（逐日预测点 {cons.get('points')}，窗口重叠）"
                   if cons.get("points") else "")
                + f" · 95%CI {cons.get('ci', [None])[0]}% ~ "
                f"{(cons.get('ci') or [None, None])[1]}%\n"
                f"无条件基础胜率 {_fmt(cons.get('base'), 1, '%')}"
                + (f" · 模型相对基准 {float(cons['uplift']):+.1f}pp"
                   if cons.get("uplift") is not None else "")
                + (f" · 重叠滚动OOS频率 {cons.get('overlap_freq')}%（研究参考，不参与决策）"
                   if cons.get("overlap_freq") is not None else "")
                + ("\n※ 独立事件不足 30 个：样本极少，仅供研究。"
                   if cons.get("few_samples") else "")
                + ("\n※ 当前模型没有证明额外择时优势。"
                   if cons.get("no_edge") else "")
                + ("\n这是独立事件去重后的样本外统计，不代表未来实际收益。"))
        else:
            self.s_prob.setText(
                f"30日 {_fmt(prob.get('p30'), 1, '%')} · "
                f"60日 {_fmt(prob.get('p60'), 1, '%')} · "
                f"90日 {_fmt(prob.get('p90'), 1, '%')}（相似状态历史频率，研究参考）")
            self.s_prob_sub.setText(
                f"基础胜率（不用任何信号）{_fmt(prob.get('base60'), 1, '%')}"
                + (f" · 相对基准 {float(prob['uplift60']):+.1f}pp"
                   + ("（无明显择时优势）" if abs(float(prob.get("uplift60") or 0)) < 2 else "")
                   if prob.get("uplift60") is not None else "")
                + f"\n重叠滚动OOS频率（研究参考，不参与决策）：30日 {_fmt(prob.get('oos30'), 1, '%')}"
                f" · 60日 {_fmt(prob.get('oos60'), 1, '%')}"
                f" · 90日 {_fmt(prob.get('oos90'), 1, '%')}\n"
                f"样本 {prob.get('samples60')} 次（重叠滚动OOS {prob.get('oos_samples60')} 次）"
                f" · 95% 置信区间 {_fmt(prob.get('ci95_low'), 1, '%')} ~ "
                f"{_fmt(prob.get('ci95_high'), 1, '%')}"
                f" · 可信度 {prob.get('grade')}\n"
                f"这是历史相似市场状态下的统计概率，不代表未来实际收益。"
                + ("\n※ 高概率主要来自组合本身长期上涨特征，目前没有证明择时信号显著提高胜率。"
                   if prob.get("uplift60") is not None
                   and abs(float(prob["uplift60"])) < 2 else ""))

        # ---- 如果不买 / 如果执行建议（需求 45/50/51）----
        b = comp.get("before") or {}
        a = comp.get("after") or {}
        trade = comp.get("trade")
        self.s_cmp_no.setText(
            "如果今天什么都不买：相似状态历史频率（研究参考） "
            f"{_fmt(b.get('prob60'), 1, '%')}，期望收益 {_fmt(b.get('exp60'), 2, '%', True)}，"
            f"1年最大回撤 {_fmt(b.get('max_dd_1y'), 2, '%')}，组合质量 "
            f"{_fmt(b.get('quality'), 1, '')}。")
        if trade:
            label = "如果执行建议" if comp.get("executable") else "如果执行【理论最优】"
            self.s_cmp_yes.setText(
                f"{label}（买 {trade.get('name')} {_money(trade.get('amount'))}）："
                f"组合60日盈利概率 {_fmt(a.get('prob60'), 1, '%')}，期望收益 "
                f"{_fmt(a.get('exp60'), 2, '%', True)}，1年最大回撤 "
                f"{_fmt(a.get('max_dd_1y'), 2, '%')}，组合质量 {_fmt(a.get('quality'), 1, '')}。")
            self.s_cmp_yes.setStyleSheet("")
        else:
            self.s_cmp_yes.setText(
                "如果执行建议：今天没有任何买入金额能让整个组合变得更好，"
                "最好的做法就是维持现状（0 元 / WAIT）。")
            self.s_cmp_yes.setStyleSheet(f"color: {pal['warn']};")
        verdict = comp.get("verdict") or "—"
        detail = comp.get("verdict_detail") or ""
        self.s_cmp_verdict.setText(f"结论：{verdict}。{detail}")
        self.s_cmp_verdict.setStyleSheet(
            f"color: {pal['danger']};" if comp.get("worse") else "")

        # ---- 实际仓位 vs 战略目标（需求 45）----
        cn, ct, cd = (home.get("classes_now") or {}, home.get("classes_target") or {},
                      home.get("classes_dev") or {})
        names = {"bond": "债券", "stock": "股票", "gold": "黄金", "cash": "现金"}
        pos_lines = []
        for k in ("bond", "stock", "gold", "cash"):
            if k not in cn and k not in ct:
                continue
            dev = cd.get(k)
            tag = ""
            if dev is not None:
                tag = "（超配 +%.1fpp）" % dev if dev > 0.05 else (
                    "（低配 %.1fpp）" % dev if dev < -0.05 else "（达标）")
            pos_lines.append(f"{names.get(k, k)}：实际 {_fmt(cn.get(k), 1, '%')}"
                             f" / 目标 {_fmt(ct.get(k), 1, '%')}{tag}")
        if not pos_lines:
            pos_lines = [f"债券 {_fmt(gate.get('bond_pct'), 1, '%')} 等（数据不足）"]
        _sg = _sample_gate_text(snap, pk)
        if _sg:
            pos_lines.append("")
            pos_lines.append(_sg)
        self.s_pos.setText("\n".join(pos_lines))

        self.s_gold.setText(
            f"当前 {_fmt(gold.get('current_pct'), 1, '%')}，上限 "
            f"{_fmt(gold.get('cap_pct'), 1, '%')}。\n{gold.get('advice')}")
        self.s_gold.setStyleSheet(
            f"color: {pal['danger']};" if gold.get("over_cap") else "")

    # ---------------------------------------------------------------- 今天
    def _permission_line(self, today: dict) -> str:
        """【BUY PERMISSION GATE】结论区补充：买入权限状态（可信等级决定买入权限）。"""
        perm = (today or {}).get("buy_permission") or {}
        if not perm:
            return ""
        if perm.get("allowed"):
            return (f"\n\n【买入权限】已开放（可信等级 {perm.get('trust_level')}"
                    f" ≥ 门槛 {perm.get('min_level')}）。")
        rr = (today or {}).get("research_reference")
        txt = (f"\n\n【买入权限】关闭 —— 可信等级 {perm.get('trust_level')}"
               f" 未达开放门槛 {perm.get('min_level')}，因此今天不执行实际买入"
               "（可信等级决定买入权限，不是买入等级）。")
        if rr:
            txt += (f"\n研究参考：模型模拟买入 {rr.get('name')} "
                    f"{int(rr.get('amount') or 0):,} 元后组合质量最高"
                    "（仅作研究，不转换成实际买入）。")
        return txt

    def _fill_today(self, snap, p, pk) -> None:
        pal = palette()
        today = pk.get("today") or {}
        three = pk.get("three") or {}
        gate = pk.get("gate") or {}
        self.lbl_today.setText(today.get("full") or "暂无数据")
        self.lbl_today.setStyleSheet(
            f"color: {pal.get(today.get('color') or 'muted', pal['text'])};")
        # ---- 【BUY PERMISSION GATE】可信等级决定买入权限（不是买入等级）----
        _perm = today.get("buy_permission") or {}
        _perm_line = ""
        if _perm:
            if _perm.get("allowed"):
                _perm_line = (f"\n买入权限：已开放（可信等级 {_perm.get('trust_level')}"
                              f" ≥ 门槛 {_perm.get('min_level')}）")
            else:
                _rr = today.get("research_reference")
                _perm_line = (f"\n买入权限：关闭（可信等级 {_perm.get('trust_level')}"
                              f" < 门槛 {_perm.get('min_level')}）"
                              "—— 今天不执行实际买入"
                              + (f"；研究参考：模拟买入 {_rr.get('name')} "
                                 f"{int(_rr.get('amount') or 0):,} 元后组合质量最高"
                                 if _rr else ""))
        self.lbl_today_sub.setText(
            f"{today.get('sub') or ''}\n"
            f"组合买入门槛：{gate.get('grade') or '—'} 级"
            f"（概率来源：{gate.get('prob_source') or '暂无数据'}）" + _perm_line)

        while self.grid_q.count():
            it = self.grid_q.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        items = [
            ("今天是否投入新增资金？", three.get("new_money_text", "—"),
             (three.get("new_money_note") or "YES 才会给出第一批金额"),
             "up" if three.get("new_money") else "muted"),
            ("今天是否需要再平衡？", three.get("rebalance_text", "—"),
             "与『是否买入』完全独立", "warn" if three.get("rebalance") else "muted"),
            ("今天是否需要降低超配资产？", three.get("reduce_text", "—"),
             "、".join(three.get("reduce_targets") or []) or "无需降低",
             "danger" if three.get("reduce_overweight") else "muted"),
        ]
        for i, (a, b, c, col) in enumerate(items):
            t = Tile(a, b, c)
            t.set(b, c, col)
            self.grid_q.addWidget(t, 0, i)

        cons = pk.get("consistency") or {}
        if cons.get("ok"):
            self.card_warn.setVisible(False)
        else:
            self.card_warn.setVisible(True)
            self.lbl_cons.setText(
                "【数据一致性异常】\n" + "\n".join("· " + e for e in cons.get("errors", []))
                + "\n→ 已按规则禁止生成任何买入/卖出金额建议，请先去『设置』修正。")
            self.lbl_cons.setStyleSheet(f"color: {pal['danger']};")

    # ---------------------------------------------------------------- 概率
    def _fill_prob(self, snap, p, pk) -> None:
        prob = pk.get("prob") or {}
        gate = pk.get("gate") or {}
        tgt = (pk.get("home") or {}).get("strategic_target") or {}
        cons = ((pk.get("home") or {}).get("conservative")
                or (pk.get("headline") or {}).get("conservative") or {})
        h60 = (p["hist"].get(60) or {})
        items = []
        if cons.get("prob") is not None:
            _ci = cons.get("ci") or []
            items.append((
                "决策主口径：60日概率（独立事件去重样本外）",
                _fmt(cons.get("prob"), 1, "%"),
                f"独立事件 {cons.get('indep')} 个"
                + (f"；95%CI {_ci[0]}~{_ci[1]}%" if len(_ci) == 2
                   and _ci[0] is not None else "")
                + ("；样本极少，仅供研究" if cons.get("few_samples") else ""),
                "warn"))
        items.extend([
            ("相似状态历史频率60日（研究参考）", _fmt(prob.get("p60"), 1, "%"),
             f"95%CI {prob.get('ci95_low')}~{prob.get('ci95_high')}%", None),
            ("重叠滚动OOS频率（研究参考）",
             _fmt(cons.get("overlap_freq") if cons else prob.get("oos60"), 1, "%"),
             "逐日预测点窗口重叠，不能当独立样本；不参与决策", "muted"),
            ("无条件基础胜率", _fmt(cons.get("base") if cons else prob.get("base60"), 1, "%"),
             "不用任何信号的同窗口基准", None),
            ("模型相对基准",
             (f"{float(cons['uplift']):+.1f}pp" if cons and cons.get("uplift") is not None
              else _fmt(prob.get("uplift60"), 1, "%", True)),
             "当前模型没有证明额外择时优势" if (cons and cons.get("no_edge"))
             else "方向仅供参考，有效性以策略有效性页为准", None),
            ("未来30日盈利概率", _fmt(prob.get("p30"), 1, "%"),
             "短期（6周）；重叠滚动OOS频率 " + (_fmt(prob.get("oos30"), 1, "%")
             if prob.get("oos30") is not None else "暂无"), None),
            ("未来90日盈利概率", _fmt(prob.get("p90"), 1, "%"),
             "季度（约4个月）；重叠滚动OOS频率 " + (_fmt(prob.get("oos90"), 1, "%")
             if prob.get("oos90") is not None else "暂无"), None),
            ("历史相似样本", str(prob.get("samples60") or "暂无数据"),
             f"其中重叠滚动OOS {prob.get('oos_samples60')} 次", None),
            ("样本可信度", str(cons.get("calibration") or prob.get("grade") or "暂无数据")
             if cons else str(prob.get("grade") or "暂无数据"),
             "样本越少、置信区间越宽，可信度越低", None),
            ("未来60日期望收益", _fmt(gate.get("exp60"), 2, "%", True),
             f"盈亏比 {_fmt(gate.get('payoff'))}",
             "up" if (gate.get("exp60") or 0) > 0 else "down"),
            ("未来60日最大回撤", _fmt(gate.get("dd60"), 2, "%"),
             f"未来120日概率 {_fmt(prob.get('p120'), 1, '%')}", "down"),
        ])
        if tgt:
            items.append(
                ("〔参照〕战略目标组合60日概率", _fmt(tgt.get("prob60"), 1, "%"),
                 "完全达到目标时的样子，仅供参照、不参与今日判断", None))
        while self.prob_grid.count():
            it = self.prob_grid.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        for i, (a, b, c, col) in enumerate(items):
            t = Tile(a, b, c)
            if col:
                t.set(b, c, col)
            self.prob_grid.addWidget(t, i // 3, i % 3)
        self.lbl_prob_note.setText(
            "核心周期取 60 日（对应 6-12 个月投资周期）；20 日用于短期辅助、120 日用于中期确认。"
            "「这是历史相似市场状态下的统计概率，不代表未来实际收益。」\n"
            "口径提醒：以上概率一律以【当前真实持仓 + 待投资现金】计算；"
            "带〔参照〕的那一项是『完全达到战略目标』的假想组合，"
            "仅用来对比目标长什么样，绝不用它回答今天买不买。")

    # ---------------------------------------------------------------- 仓位
    def _fill_position(self, snap, p, pk) -> None:
        w = p["weights"]
        cls = w["by_class"]
        tgt = p.get("target_class_with_cash") or {**p["target_class"], "cash": 0.0}
        total = w.get("total_assets") or 0
        rows = []
        for k, label in (("bond", "债券"), ("stock", "股票"), ("gold", "黄金"),
                         ("cash", "现金/待投资")):
            cur = (cls.get(k) or 0) * 100
            t = (tgt.get(k) or 0) * 100
            rows.append([label, _fmt(cur, 2, "%"), _fmt((cur - t), 2, "pp", True),
                         _money(cur / 100 * total if total else None)])
        rows.append(["合计", _fmt(sum((cls.get(k) or 0) for k in
                                      ("bond", "stock", "gold", "cash")) * 100, 2, "%"),
                     "", _money(total)])
        self.tbl_pos.set_rows(["大类", "当前占比", "相对目标", "对应金额"], rows,
                              color_cols={2}, bold_cols={1})
        self.tbl_pos.setMaximumHeight(170)

        trows = [[label, _fmt((tgt.get(k) or 0) * 100, 2, "%")]
                 for k, label in (("bond", "债券"), ("stock", "股票"),
                                  ("gold", "黄金"), ("cash", "现金"))]
        self.tbl_tgt.set_rows(["大类", "战略目标"], trows, bold_cols={1})
        self.tbl_tgt.setMaximumHeight(170)

    # ---------------------------------------------------------------- 问题
    def _fill_issues(self, snap, p, pk) -> None:
        w = p["weights"]
        cls = w["by_class"]
        tgt = p["target_class"]
        total = w.get("total_assets") or 0
        items = []
        for k, label in (("gold", "黄金超配"), ("stock", "股票低配"), ("bond", "债券低配")):
            cur = (cls.get(k) or 0) * 100
            t = (tgt.get(k) or 0) * 100
            dev = cur - t
            if k != "gold" and dev >= -1e-6:
                continue
            if k == "gold" and dev <= 1e-6:
                continue
            items.append((label, f"{dev:+.1f} 个百分点",
                          f"对应金额 {_money(abs(dev) / 100 * total)}",
                          "danger" if k == "gold" else "warn"))
        items.append(("待投资资金", _money(w.get("cash_value")),
                      f"占总资产 {_fmt(w.get('cash_pct'), 1, '%')}", None))
        while self.issues_grid.count():
            it = self.issues_grid.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        for i, (a, b, c, col) in enumerate(items):
            t = Tile(a, b, c)
            if col:
                t.set(b, c, col)
            self.issues_grid.addWidget(t, 0, i)

    # ---------------------------------------------------------------- 下一次
    def _fill_next(self, snap, p, pk) -> None:
        trig = pk.get("triggers") or {}
        rows = [[("√" if c.get("ok") else "×"), c.get("name"), c.get("now")]
                for c in trig.get("portfolio_conditions", [])]
        self.tbl_next.set_rows(["#", "条件", "当前状态"], rows,
                               stretch_cols=[1, 2])
        self.tbl_next.setMinimumHeight(200)
        focus = trig.get("focus")
        self.lbl_next_focus.setText(
            (f"【下一批】{focus['name']}：{focus['trigger']}"
             f"（预计下一批金额 {_money(focus.get('next_batch_amount'))}）")
            if focus else "暂无明确标的")
        self.lbl_next_focus.setWordWrap(True)

    # ---------------------------------------------------------------- 今天买什么
    def _fill_pick(self, snap, p, pk) -> None:
        plan = pk.get("plan") or {}
        picks = plan.get("picks") or []
        rows = []
        for i in picks:
            rows.append([i["name"], i["buy_quality"], _fmt(i["target_pct"], 1, "%"),
                         _fmt(i["current_pct"], 1, "%"), _fmt(i["gap_pct"], 2, "pp", True),
                         _money(i["gap_amount"]), _money(i["first_batch_amount"]),
                         _money(i["remaining_wait_amount"])])
        self.tbl_pick.set_rows(
            ["ETF", "买入质量", "目标", "当前", "缺口", "目标缺口金额",
             "第一批金额", "剩余等待"],
            rows or [["本次不投入新增资金（WAIT）", "—", "", "", "", "", "0 元", ""]],
            bold_cols={6}, stretch_cols=[0])
        self.tbl_pick.setMinimumHeight(150)
        _note = plan.get("note") or ""
        if plan.get("permission_blocked"):
            _rps = plan.get("research_picks") or []
            if _rps:
                _note += ("\n研究参考（今天不执行）：" + "；".join(
                    f"{i['name']} {_money(i.get('first_batch_amount'))}"
                    for i in _rps) + " —— 仅研究，不转换成实际买入")
        self.lbl_pick_note.setText(_note)

    # -------------------------------------------- 交易前后对比（§50/§51）
    def _fill_compare(self, snap, p, pk) -> None:
        pal = palette()
        comp = pk.get("comparison") or {}
        b = comp.get("before") or {}
        a = comp.get("after") or {}
        trade = comp.get("trade")
        imp = comp.get("improve") or {}

        def row(label, key, nd, suffix="", sign=False):
            bv = _fmt(b.get(key), nd, suffix, sign)
            if not trade:
                return [label, bv, "—", "—"]
            av = _fmt(a.get(key), nd, suffix, sign)
            d = _fmt(imp.get(key), nd, suffix, True)
            return [label, bv, av, d]

        rows = [
            row("组合60日盈利概率", "prob60", 1, "%"),
            row("组合120日盈利概率", "prob120", 1, "%"),
            row("组合60日期望收益", "exp60", 2, "%", True),
            row("组合1年最大回撤", "max_dd_1y", 2, "%"),
            row("组合年化波动", "vol_annual", 2, "%"),
            row("组合质量评分", "quality", 1),
        ]
        head = (f"【什么都不买】", (f"【买入 {trade.get('name')} "
                f"{_money(trade.get('amount'))}】" if trade else "【执行建议】"),
                "变化")
        if trade and not comp.get("executable"):
            head = (head[0], f"【理论最优：买 {trade.get('name')} "
                    f"{_money(trade.get('amount'))}】", head[2])
        self.tbl_cmp.set_rows(["项目", head[0], head[1], head[2]], rows,
                              color_cols={3}, bold_cols={2}, stretch_cols=[0])
        self.tbl_cmp.setMinimumHeight(190)
        verdict = comp.get("verdict") or "—"
        detail = comp.get("verdict_detail") or comp.get("note") or ""
        self.lbl_cmp_verdict.setText(f"结论：{verdict}。{detail}")
        self.lbl_cmp_verdict.setStyleSheet(
            f"color: {pal['danger']};" if comp.get("worse") else "")

    # ---------------------------------------------------------------- 现金
    def _fill_cash(self, snap, p, pk) -> None:
        w = p["weights"]
        plan = pk.get("plan") or {}
        three = pk.get("three") or {}
        first = plan.get("total_first_batch_amount") or 0
        cash = w.get("cash_value") or 0
        while self.cash_grid.count():
            it = self.cash_grid.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        items = [
            ("当前待投资资金", _money(cash), "总资产 - 当前 ETF 市值（真实计算，不虚构）", None),
            ("占总资产比例", _fmt(w.get("cash_pct"), 1, "%"), "现金/待投资 独立成项", None),
            ("本次建议投入", _money(first), "按目标缺口与分批计划计算", "up" if first else "muted"),
            ("本次后剩余等待", _money(max(0.0, cash - first)), "继续等待下一批触发条件", None),
        ]
        for i, (a, b, c, col) in enumerate(items):
            t = Tile(a, b, c)
            if col:
                t.set(b, c, col)
            self.cash_grid.addWidget(t, 0, i)
        if first > 0:
            self.lbl_cash_advice.setText(f"建议：第一批配置 {_money(first)}，"
                                         f"剩余 {_money(max(0.0, cash - first))} 继续等待。"
                                         "（模型建议金额，不会自动下单）")
        else:
            self.lbl_cash_advice.setText("建议：全部等待（本次不投入新增资金）。")

    # ---------------------------------------------------------------- 不要
    def _fill_nono(self, snap, p, pk) -> None:
        pal = palette()
        plan = pk.get("plan") or {}
        no_chase, no_bottom = [], []
        for i in plan.get("items", []):
            r = i.get("reason") or ""
            if i.get("blocked") and "追涨" in r:
                no_chase.append(i["name"])
            if i.get("blocked") and "抄底" in r:
                no_bottom.append(i["name"])
        self.lbl_nochase.setText("、".join(no_chase) if no_chase else "无")
        self.lbl_nochase.setStyleSheet(
            f"color: {pal['warn']};" if no_chase else f"color: {pal['muted']};")
        self.lbl_nobottom.setText("、".join(no_bottom) if no_bottom else "无")
        self.lbl_nobottom.setStyleSheet(
            f"color: {pal['danger']};" if no_bottom else f"color: {pal['muted']};")

    # ---------------------------------------------------------------- ETF 表
    def _fill_etf_table(self, snap, p, pk) -> None:
        plan = pk.get("plan") or {}
        rows = []
        for i in plan.get("items", []):
            rows.append([
                i["name"], _fmt(i["current_pct"], 1, "%"), _fmt(i["target_pct"], 0, "%"),
                _fmt(i["gap_pct"], 1, "pp", True), _fmt(i["prob60"], 1, "%"),
                _fmt(i["exp60"], 2, "%", True), i["position_label"],
                i["buy_quality"] or "—", i["action"],
                _money(i["first_batch_amount"]) if i["first_batch_amount"] else "0 元",
            ])
        self.tbl_etf.set_rows(
            ["ETF", "当前仓位", "目标", "缺口", "60日胜率", "60日期望", "当前位",
             "买入质量", "今天动作", "第一批金额"],
            rows, color_cols={3, 5}, stretch_cols=[0])
        self.tbl_etf.setMinimumHeight(210)

    # ---------------------------------------------------------------- 四批
    def _fill_batch(self, snap, p, pk) -> None:
        batches = pk.get("batches") or []
        rows = []
        for b in batches:
            amt = b.get("amount")
            rows.append([
                f"第 {b['batch']} 批",
                (_money(amt) if amt else "0 元"),
                f"{b['pct']}%",
                ("√ 可执行" if b.get("done") else "× 等待"),
                b.get("status") or "",
                b.get("reason") or b.get("condition") or "",
            ])
        self.tbl_batch.set_rows(["批次", "本批金额", "占缺口", "状态", "结论", "说明"],
                                rows, stretch_cols=[5], bold_cols={1, 2},
                                color_cols={0})
        self.tbl_batch.setMinimumHeight(180)
        note = ((pk.get("ladder") or {}).get("note") or "")
        if note:
            self.lbl_batch_note.setText(note)
        else:
            self.lbl_batch_note.setText(
                "每批金额都不是固定比例：第 1 批在当前组合上模拟；后续批在"
                "「前一批已执行」的新组合上重新模拟，市场恶化时后面几批会自动变成 0 元（WAIT）。")

    # ---------------------------------------------------------------- 边际
    def _fill_marginal(self, snap, p, pk) -> None:
        marg = pk.get("marginal") or {}
        per = marg.get("per_asset") or {}
        base = marg.get("baseline") or {}
        rows = []
        if base:
            rows.append([
                "（今天什么都不买）", "0 元",
                _fmt(base.get("prob60"), 1, "%"),
                _fmt(base.get("exp60"), 2, "%", True),
                _fmt(base.get("max_dd_1y"), 2, "%"),
                _fmt(base.get("vol_annual"), 2, "%"),
                "50.0", "—",
            ])
        for aid, m in per.items():
            if not m.get("available"):
                continue
            b = m.get("baseline") or base
            s = m.get("best") or {}
            amt = float(m.get("amount") or 0)
            wait = amt <= 0
            rows.append([
                m.get("name"),
                ("等待（0 元）" if wait else _money(amt)),
                (f"{_fmt(b.get('prob60'), 1, '%')} → {_fmt(s.get('prob60'), 1, '%')}"
                 if not wait else _fmt(b.get("prob60"), 1, "%")),
                f"{_fmt(b.get('exp60'), 2, '%', True)} → {_fmt(s.get('exp60'), 2, '%', True)}",
                f"{_fmt(b.get('max_dd_1y'), 2, '%')} → {_fmt(s.get('max_dd_1y'), 2, '%')}",
                f"{_fmt(b.get('vol_annual'), 2, '%')} → {_fmt(s.get('vol_annual'), 2, '%')}",
                _fmt(s.get("marginal_score"), 1),
                _fmt(s.get("improve_quality"), 1, "", True),
            ])
        self.tbl_marginal.set_rows(
            ["ETF / 方案", "本批金额", "组合60日盈利概率", "组合60日期望收益",
             "组合1年最大回撤", "组合年化波动", "边际改善分", "质量变化"],
            rows or [["暂无数据", "", "", "", "", "", "", ""]],
            stretch_cols=[0, 2, 3], color_cols={6, 7})
        self.tbl_marginal.setMinimumHeight(220)
        if rows and len(rows) > 1:
            self.lbl_marginal_note.setText(
                "「边际改善分 50 = 与不交易完全一样」，> 50 才说明买入让整个组合变好；"
                "做法：先把现金换成某只 ETF，再用【同一批历史相似窗口】重算整个组合"
                " 20/60/120 日表现，因此对比差异只来自组合构成变化，不含未来数据。")
        else:
            self.lbl_marginal_note.setText("")

    # ---------------------------------------------------------------- 结论
    def _fill_conclusion(self, snap, p, pk) -> None:
        pal = palette()
        today = pk.get("today") or {}
        lines = pk.get("explain") or (p["decision"].get("lines") or [])
        why = pk.get("why_not") or []
        body = list(lines[:5])
        self.lbl_concl.setText(
            f"【今天：{today.get('text') or '—'}】\n"
            + "\n".join(f"{i + 1}. {x}" for i, x in enumerate(body))
            + (("\n\n不能买的原因：\n" + "\n".join(
                f"· {x}" for x in why[:5])) if why and not (pk.get("three") or {}).get("new_money") else "")
            + self._permission_line(today))
        warns = list(snap.get("warnings", []))
        if warns:
            self.lbl_warn.setText("※ 提示：\n" + "\n".join("· " + w for w in warns))
            self.lbl_warn.setStyleSheet(f"color: {pal['warn']};")
        else:
            self.lbl_warn.setText("")

    # ==================================================================
    # 以下为原有标签页填充（保持不动）
    # ==================================================================
    def _fill_stats(self, snap, p) -> None:
        h = p["hist"]
        rows = []
        for hz in (5, 20, 60, 120):
            d = h.get(hz) or {}
            rows.append([
                f"{hz} 交易日",
                d.get("win_rate_pct"), d.get("win_rate_net_pct"),
                f"{d.get('ci95_low')}~{d.get('ci95_high')}",
                d.get("expected_return_pct"), d.get("median_return_pct"),
                d.get("avg_win_pct"), d.get("avg_loss_pct"),
                d.get("payoff_ratio"), d.get("max_drawdown_pct"),
                d.get("prob_ge_5_pct"), d.get("prob_le_minus5_pct"),
                d.get("samples"), d.get("sample_grade"),
                d.get("distance_mult"),
                d.get("all_history_win_rate_pct"), d.get("edge_vs_baseline_pct"),
                d.get("oos_win_rate_pct"), d.get("oos_samples"),
            ])
        self.tbl_hist.set_rows(
            ["周期", "盈利概率%", "扣成本概率%", "95%CI", "期望收益%", "中位收益%",
             "平均盈利%", "平均亏损%", "盈亏比", "最大回撤%", "≥5%概率%", "≤-5%概率%",
             "样本数", "样本评级", "距离倍数", "全部历史胜率%", "相对基准%",
             "样本外胜率%", "样本外样本数"],
            rows, color_cols={1, 4, 5, 6, 7, 15, 16, 17}, bold_cols={1})
        self.tbl_hist.setMinimumHeight(140)

        s = p["stats"]
        st = [
            ["组合历史区间", f"{s['span'].get('start')} ~ {s['span'].get('end')}",
             f"约 {s['span'].get('years')} 年 / {s['span'].get('bars')} 个交易日", ""],
            ["年化收益", _fmt(s.get("annual_return_pct"), 2, "%"), "", ""],
            ["年化波动率", _fmt(s.get("annual_vol_pct"), 2, "%"), "", ""],
            ["20日年化波动率", _fmt(s.get("vol20_annual_pct"), 2, "%"), "", ""],
            ["Sharpe（rf=2%）", _fmt(s.get("sharpe")), "", ""],
            ["Sortino", _fmt(s.get("sortino")), "", ""],
            ["最大回撤（全区间）", _fmt(s.get("max_drawdown_full_pct"), 2, "%"), "", ""],
            ["1年最大回撤",
             _fmt((s["dd_windows"].get("1y") or {}).get("max_drawdown_pct"), 2, "%"),
             f"当前 {_fmt((s['dd_windows'].get('1y') or {}).get('current_drawdown_pct'), 2, '%')}", ""],
            ["2年最大回撤",
             _fmt((s["dd_windows"].get("2y") or {}).get("max_drawdown_pct"), 2, "%"),
             f"当前 {_fmt((s['dd_windows'].get('2y') or {}).get('current_drawdown_pct'), 2, '%')}", ""],
            ["5年最大回撤",
             _fmt((s["dd_windows"].get("5y") or {}).get("max_drawdown_pct"), 2, "%"),
             "数据不足时显示暂无数据", ""],
            ["单日 VaR(95%)", _fmt(s.get("var95_daily_pct"), 2, "%"), "", ""],
            ["最大连续亏损", str(s.get("max_losing_streak", "暂无数据")), "个交易日", ""],
            ["最长水下期",
             str((s.get("underwater") or {}).get("longest_days", "暂无数据")), "个交易日",
             "已恢复" if (s.get("underwater") or {}).get("recovered") else "尚未恢复"],
        ]
        self.tbl_stats.set_rows(["项目", "数值", "说明", ""], st, color_cols={1})
        self.tbl_stats.setColumnHidden(3, True)

        yrs = s.get("annual_returns") or {}
        self.tbl_yearly.set_rows(["年份", "组合收益%"],
                                 [[y, v] for y, v in yrs.items()],
                                 color_cols={1}, bold_cols={1})
        self.tbl_yearly.setMaximumHeight(150)

    def _fill_corr(self, p) -> None:
        self._redraw_corr()

    def _fill_risk(self, p) -> None:
        rc = p["risk_contribution"]
        rows = []
        if rc.get("available"):
            for r in rc["rows"]:
                rows.append([r["name"], r["weight_pct"], r["risk_contribution_pct"],
                             r["excess_pct"], r["vol_annual_pct"], r["flag"]])
        self.tbl_rc.set_rows(
            ["ETF", "仓位贡献%", "风险贡献%", "差额(pp)", "年化波动%", "结论"],
            rows or [["暂无数据", "", "", "", "", ""]],
            color_cols={3}, bold_cols={2}, stretch_cols=[5])
        self.tbl_rc.setMinimumHeight(200)
        if rc.get("available"):
            self.chart_rc.set_items(
                [(r["name"][:10], r["risk_contribution_pct"], r["weight_pct"])
                 for r in rc["rows"]])
            self.tbl_vol.set_rows(
                ["项目", "数值"],
                [["组合年化波动率", _fmt(rc.get("portfolio_vol_annual_pct"), 2, "%")],
                 ["有效资产数（分散度）", _fmt(rc.get("effective_assets"))],
                 ["计算窗口", f"{rc.get('window')} 个交易日"],
                 ["相关性分散度评分",
                  _fmt(p["correlation"].get("diversification_score"), 1)]],
                color_cols={1})
        else:
            self.chart_rc.set_items([])
            self.tbl_vol.set_rows(["项目", "数值"], [["暂无数据", ""]])

    def _fill_rebal(self, p) -> None:
        rb = p["rebalance"]
        self.lbl_rebal_head.setText(
            f"{'需要再平衡' if rb['need'] else '暂不需要再平衡'} · "
            f"观察阈值 {rb['watch_threshold']:.0f}pp / 优先阈值 {rb['priority_threshold']:.0f}pp · "
            f"黄金 {rb['gold_current_pct']}% / 上限 {rb['gold_cap_pct']}%"
            f"（{rb['gold_state']}）。注意：不会仅仅因为涨多了就机械卖出，"
            "必须结合趋势、波段顶底、风险贡献与历史统计一起判断。")
        self.tbl_rebal.set_rows(
            ["ETF", "当前%", "目标%", "偏离(pp)", "级别", "建议"],
            [[i["name"], i["current_pct"], i["target_pct"], i["deviation_pct"],
              i["level"], i["action"]] for i in rb["items"]]
            or [["—", "", "", "", "—", "所有 ETF 偏离均在阈值内"]],
            color_cols={3}, stretch_cols=[5])
        self.tbl_rebal.setMinimumHeight(200)

        # ---- 再平衡模拟（需求 42/43）----
        rb_sim = (p.get("plan") or {}).get("rebal_sim") or {}
        opts = rb_sim.get("options") or []
        sim_rows = []
        for o in opts:
            sim_rows.append([
                f"降到 {_fmt(o.get('target_pct'), 1, '%')}",
                _money(o.get("sell_amount")),
                _money(o.get("cash_after")),
                _fmt(o.get("prob60"), 1, "%"),
                _fmt(o.get("exp60"), 2, "%", True),
                _fmt(o.get("max_dd_1y"), 2, "%"),
                _fmt(o.get("quality"), 1),
                _fmt(o.get("improve_quality"), 1, "", True),
                "← 最优" if (rb_sim.get("best") or {}).get("target_pct") == o.get("target_pct") else "",
            ])
        self.tbl_rebsim.set_rows(
            ["方案", "卖出金额", "卖出后现金", "组合60日概率", "60日期望",
             "1年回撤", "组合质量", "质量变化", ""],
            sim_rows or [["暂无数据（黄金未超配或模拟不可用）", "", "", "", "", "", "", "", ""]],
            color_cols={7}, stretch_cols=[0])
        self.tbl_rebsim.setMinimumHeight(140)
        self.tbl_rebsim.setColumnHidden(8, True)

        opt = p["optimizer"]
        tgt = p["target_class"]
        names = {r["asset"]["id"]: r["asset"].get("name") for r in self.snap["rows"]
                 if r.get("snapshot")}
        rows = []
        for aid, wv in sorted(opt["weights"].items(), key=lambda x: -x[1]):
            cls = next((r["asset"].get("asset_class") for r in self.snap["rows"]
                        if r["asset"]["id"] == aid), "")
            cur = p["weights"]["by_asset_total"].get(aid)
            t = next((r["asset"].get("target_weight") for r in self.snap["rows"]
                      if r["asset"]["id"] == aid), None)
            rows.append([names.get(aid, aid), cls, _fmt((t or 0) * 100, 1),
                         _fmt(wv * 100, 1),
                         _fmt((cur or 0) * 100, 1),
                         _fmt((wv - (cur or 0)) * 100, 2, "", True)])
        self.tbl_target.set_rows(
            ["ETF", "类别", "战略目标%", "今日战术目标%", "当前%", "目标-当前(pp)"],
            rows, color_cols={5}, bold_cols={3}, stretch_cols=[0])
        self.tbl_target.setMinimumHeight(210)
        self.lbl_opt_note.setText(
            f"{opt['notes']}　战略大类：债券 {tgt.get('bond', 0) * 100:.0f}% / "
            f"股票 {tgt.get('stock', 0) * 100:.0f}% / 黄金 {tgt.get('gold', 0) * 100:.0f}%")

        w = p["waiting"]
        ww = p["weights"]
        self.tbl_wait.set_rows(
            ["项目", "数值", "说明"],
            [["当前待投资资金", _money(ww.get("cash_value")),
              f"占总资产 {_fmt(ww.get('cash_pct'), 1, '%')}（现金独立成项）"],
             ["风险资产等待比例", _fmt(w.get("wait_ratio_pct"), 1, "%"), w.get("note", "")],
             ["风险资产当前占比", _fmt(w.get("risk_current_pct"), 2, "%"), "股票+黄金"],
             ["风险资产目标占比", _fmt(w.get("risk_target_pct"), 2, "%"), "股票+黄金"],
             ["风险资产缺口", _fmt(w.get("gap_pct"), 2, "%"), "目标-当前（不小于0）"]],
            color_cols={1}, stretch_cols=[2])

    def _fill_macro(self, snap) -> None:
        macro = snap.get("macro") or {}
        radar = macro.get("gold_radar") or {}
        rows = []
        for c in radar.get("conditions", []):
            ok = c.get("ok")
            rows.append([c.get("no"), c.get("name"),
                         "满足" if ok else ("不满足" if ok is False else "暂无数据"),
                         c.get("detail")])
        self.tbl_radar.set_rows(
            ["#", "条件", "状态", "说明"],
            rows or [["—", "暂无数据", "—", "黄金数据源不可用时如实显示暂无数据"]],
            stretch_cols=[3])
        self.tbl_radar.setMinimumHeight(260)

        def mm(d, key, nd=2):
            return _fmt((d or {}).get(key), nd)

        gm, um = macro.get("gold_metrics") or {}, macro.get("usd_metrics") or {}
        rm = macro.get("real_rate_metrics") or {}
        nm = macro.get("nominal_rate_metrics") or {}
        bm = macro.get("bpgdm_metrics") or {}
        gv, pm = macro.get("gvz_metrics") or {}, macro.get("policy_metrics") or {}
        fg = macro.get("fear_greed") or {}
        proxy = macro.get("gold_sentiment_proxy") or {}
        ms = macro.get("gold_macro_score") or {}
        rows = [
            ["黄金宏观评分(8指标加权)",
             f"{_fmt(ms.get('score'), 1)}/100" if ms.get("available") else "暂无数据",
             "", ms.get("grade_label") or "50=中性"],
            ["黄金底部雷达分级", radar.get("status") or "暂无数据", "",
             f"{radar.get('hit')}/{radar.get('total')} 条满足"
             + ("（有条件数据缺失）" if radar.get("missing") else "")],
            ["国际金价", mm(gm, "last"), gm.get("data_time") or "",
             f"MA200 {mm(gm, 'ma200', 0)}"],
            ["金价 20日/60日变动", f"{mm(gm, 'ret20_pct')}% / {mm(gm, 'ret60_pct')}%", "", ""],
            ["金价偏离 MA200", f"{mm(gm, 'dev_ma200_pct')}%", "", "正=在长期均线上方"],
            ["52周高点/低点", f"{mm(gm, 'high_252')} / {mm(gm, 'low_252')}", "", ""],
            ["距52周高点", f"{mm(gm, 'dd_from_high_pct')}%", "",
             "已停止创新低" if gm.get("stopped_new_low") else "仍在创新低"],
            ["美元指数 DXY", mm(um, "last"), um.get("data_time") or "",
             um.get("direction") or ""],
            ["DXY 20日变动", f"{mm(um, 'ret20_pct')}%", "",
             "美元转弱（利多黄金）" if um.get("weak_confirmed") else ""],
            ["美国10年期实际利率", f"{mm(rm, 'last')}%", rm.get("data_time") or "",
             rm.get("direction") or ""],
            ["实际利率 20日变化", f"{mm(rm, 'chg_20d')}", "", "负=下降（利多黄金）"],
            ["美国10年期国债收益率", f"{mm(nm, 'last')}%", nm.get("data_time") or "",
             nm.get("direction") or ""],
            ["Fed利率预期(2Y-3M)", mm(pm, "last") if pm.get("available") else "暂无数据",
             pm.get("data_time") or "",
             pm.get("direction") or "负=市场定价未来降息（利多黄金）"],
            ["黄金波动率指数 GVZ",
             mm(gv, "gvz") if gv.get("available") else "暂无数据",
             gv.get("gvz_data_date") or "",
             (f"来源 {gv.get('gvz_source') or ''}｜系统研究分档『{gv.get('band_name') or ''}』"
              f"（非官方阈值）"
              + (f"，52周分位 {gv.get('gvz_52w_percentile')}%"
                 if gv.get("gvz_52w_percentile") is not None else "")
              + ("，低位拐头向上" if gv.get("turning_up") else "")
              + ("，短期骤升" if gv.get("gvz_spike") else ""))],
            ["BPGDM 真实值（StockCharts/StockQ）",
             _fmt(bm.get("bpgdm_real")) if bm.get("real_available") else "暂无数据",
             bm.get("bpgdm_real_data_date") or "",
             bm.get("bpgdm_real_source") or "真实 BPGDM 源不可用，不伪造"],
            ["BPGDM 代理值【非真实BPGDM】",
             _fmt(bm.get("bpgdm_proxy")) if bm.get("proxy_available") else "—",
             bm.get("bpgdm_proxy_data_date") or "",
             bm.get("bpgdm_proxy_algo") or "无代理数据"],
            ["BPGDM 研究区间 / P&F",
             bm.get("zone_name") or "暂无数据", "",
             (bm.get("pnf_note") or "P&F 数据缺失则为 UNKNOWN")],
            ["BPGDM 均线", "　".join(
                f"MA{k}={_fmt(bm.get('ma' + k), 1)}"
                for k in ("5", "10", "20", "60", "120", "260")
                if bm.get("ma" + k) is not None) or "暂无数据", "", ""],
            ["BPGDM 2年分位",
             f"{_fmt(bm.get('percentile_2y'), 1)}%" if bm.get("available") else "暂无数据",
             "", "低位=金矿股超卖" + ("，已拐头" if bm.get("turning_up") else "")],
            ["Gold Fear & Greed",
             _fmt(fg.get("score"), 1) if fg.get("score") is not None else "暂无数据",
             fg.get("updated") or "", fg.get("zone") or "官方接口不可用时不伪造数据"],
            ["黄金情绪（本地代理）",
             _fmt(proxy.get("score"), 1) if proxy.get("available") else "暂无数据",
             "本地计算", proxy.get("label") or ""],
        ]
        self.tbl_macro.set_rows(["指标", "数值", "数据时间", "说明"], rows,
                                color_cols={1}, stretch_cols=[3])
        self.tbl_macro.setMinimumHeight(460)
        self._macro_series = macro

    # ---------------------------------------------------------------- 画图
    def _period_slice(self, n: int) -> slice:
        bars = PERIODS[self.cmb_period.currentIndex()][1]
        return slice(max(0, n - bars), n)

    def _redraw_charts(self) -> None:
        if not self.snap or not self.snap.get("ok"):
            return
        p = self.snap["portfolio"]
        panel = p["panel"]
        n = len(panel.dates)
        sl = self._period_slice(n)
        dates = panel.dates[sl]

        self.chart_nav.set_series(
            [{"name": "组合净值", "values": panel.nav[sl], "fill": True}],
            dates, y_fmt=".3f")
        dd = []
        peak = None
        for v in panel.nav:
            peak = v if peak is None or v > peak else peak
            dd.append((v / peak - 1.0) * 100 if peak else None)
        self.chart_dd.set_series(
            [{"name": "组合回撤%", "values": dd[sl], "color": palette()["down"], "fill": True}],
            dates, zero_line=True, y_fmt=".1f")
        risk_series = p["stats"].get("risk_series") or []
        self.chart_risk.set_series(
            [{"name": "风险评分", "values": (risk_series or [None] * n)[sl],
              "color": palette()["warn"], "fill": True}],
            dates, y_fmt=".0f")

        macro = getattr(self, "_macro_series", {}) or {}

        def series_of(d, m=1):
            if not d:
                return [], []
            return d.get("dates", []), [v * m if isinstance(v, (int, float)) else None
                                        for v in d.get("close", [])]

        gx, gy = series_of(macro.get("gold"))
        self.chart_gold.set_series([{"name": "国际金价", "values": gy, "fill": True}], gx)
        bx, by = series_of(macro.get("bpgdm"))
        bm_m = macro.get("bpgdm_metrics") or {}
        bm_name = ("真实BPGDM" if bm_m.get("real_available")
                   else "BPGDM代理（非真实BPGDM）")
        bs = [{"name": bm_name, "values": by, "fill": True}]
        for wv, col in ((20, palette()["series"][1]), (60, palette()["series"][3])):
            if len(by) >= wv:
                ma = [None] * (wv - 1) + [
                    sum(by[i - wv + 1:i + 1]) / wv for i in range(wv - 1, len(by))]
                bs.append({"name": f"MA{wv}", "values": ma, "color": col, "width": 1.2})
        self.chart_bpgdm.set_series(bs, bx, y_fmt=".1f")
        for key, chart, title in (("usd", self.chart_dxy, "美元指数 DXY"),
                                  ("real_rate", self.chart_real, "美国10年期实际利率（%）")):
            x, y = series_of(macro.get(key))
            chart.set_series([{"name": title, "values": y, "fill": True}], x)

    def _redraw_corr(self) -> None:
        if not self.snap or not self.snap.get("ok"):
            return
        corr = self.snap["portfolio"]["correlation"]
        key = self.cmb_corr.currentText()
        m = (corr.get("matrices") or {}).get(key)
        if not m:
            return
        self.heat.set_matrix(corr["labels"], m["matrix"])
        hi = m.get("highest_pairs") or []
        lo = m.get("lowest_pairs") or []
        rows = [
            ["平均相关性", m.get("avg_corr"), "越低说明分散效果越好"],
            ["高相关组合（分散效果差）",
             "；".join(f"{pp['a']}~{pp['b']} {pp['corr']}" for pp in hi) or "暂无数据", ""],
            ["低相关组合（分散效果好）",
             "；".join(f"{pp['a']}~{pp['b']} {pp['corr']}" for pp in lo) or "暂无数据", ""],
            ["相关性分散度评分", corr.get("diversification_score"), "0-100，越高越分散"],
        ]
        self.tbl_corr.set_rows(["项目", "数值", "说明"], rows, color_cols={1},
                               stretch_cols=[2])
        self.tbl_corr.setMaximumHeight(160)

    # ---------------------------------------------------------------- 回测
    def _run_backtest(self) -> None:
        if self.bt_worker and self.bt_worker.isRunning():
            return
        self.btn_bt.setEnabled(False)
        self.bt_text.setPlainText("回测中，请稍候…（会重新加载历史数据并逐日模拟）")
        self.bt_worker = BacktestWorker(self.cfg)
        self.bt_worker.progress.connect(lambda m: self.bt_text.setPlainText(
            (self.bt_text.toPlainText() + "\n" + m).strip()[-4000:]))
        self.bt_worker.done.connect(self._on_backtest_done)
        self.bt_worker.failed.connect(self._on_backtest_failed)
        self.bt_worker.start()

    def _on_backtest_done(self, res: dict) -> None:
        self.btn_bt.setEnabled(True)
        from core.backtest import render_backtest_md
        self.bt_text.setPlainText(render_backtest_md(res))

    def _on_backtest_failed(self, text: str) -> None:
        self.btn_bt.setEnabled(True)
        self.bt_text.setPlainText("回测失败：\n" + text[-3000:])
