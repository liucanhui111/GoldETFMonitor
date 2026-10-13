"""设置页：持仓、目标比例、风险参数、数据源、自动刷新、自动启动、日志、缓存、导出。"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QGridLayout,
                               QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget,
                               QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSpinBox,
                               QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from core import autostart
from core.config import LOG_DIR, get_config
from core.storage import export_all, list_history_files

from .theme import palette
from .widgets import Card


class SettingsPage(QWidget):
    config_changed = Signal()

    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.snap: dict | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        outer.addWidget(scroll)
        self.container = QWidget()
        scroll.setWidget(self.container)
        self.root = QVBoxLayout(self.container)
        self.root.setContentsMargins(2, 2, 2, 2)
        self.root.setSpacing(10)

        self._build_account()
        self._build_assets()
        self._build_targets()
        self._build_params()
        self._build_sources()
        self._build_autostart()
        self._build_maintenance()
        self.root.addStretch(1)

    # ==================================================================
    def _build_account(self) -> None:
        c = Card("持仓账户（组合隔离 + 资金口径）")
        c.add(QLabel(
            "每个组合有独立的 ID：持仓、成本、现金、目标权重、历史数据都按组合隔离，"
            "绝不跨组合读取持仓数量。历史数据保存在 data/<组合ID>/ 目录下。"))
        g = QGridLayout()
        self.ed_pid = QLineEdit()
        self.ed_pid.setPlaceholderText("组合 ID，例如 all_weather_01")
        self.ed_pname = QLineEdit()
        self.ed_pname.setPlaceholderText("组合名称，例如 当前全天候组合")
        self.sp_total = QDoubleSpinBox()
        self.sp_total.setRange(0, 1e9)
        self.sp_total.setDecimals(2)
        self.sp_total.setSuffix(" 元")
        self.sp_total.setToolTip("账户总资产（含待投资现金）。填了它，现金 = 总资产 - ETF 市值。")
        self.ed_cash = QLineEdit()
        self.ed_cash.setPlaceholderText("留空 = 由『总资产 - ETF市值』自动计算")
        g.addWidget(QLabel("组合 ID"), 0, 0)
        g.addWidget(self.ed_pid, 0, 1)
        g.addWidget(QLabel("组合名称"), 0, 2)
        g.addWidget(self.ed_pname, 0, 3)
        g.addWidget(QLabel("账户总资产（含现金）"), 1, 0)
        g.addWidget(self.sp_total, 1, 1)
        g.addWidget(QLabel("或直接填现金（元）"), 1, 2)
        g.addWidget(self.ed_cash, 1, 3)
        c.add_layout(g)

        # ---- 多组合：切换 / 新建（复用 config 的组合隔离后端）----
        # 每个组合的持仓/现金/目标/历史各自独立；切换只改「活动组合 ID」，不动任何持仓。
        mg = QHBoxLayout()
        self.cmb_portfolio = QComboBox()
        self.cmb_portfolio.setMinimumWidth(220)
        self.cmb_portfolio.setToolTip("列出所有已存在的组合；选中后点『切换到此组合』。")
        self.btn_switch = QPushButton("切换到此组合")
        self.btn_switch.setObjectName("Primary")
        self.btn_switch.clicked.connect(self._switch_portfolio)
        self.btn_new = QPushButton("新建组合…")
        self.btn_new.clicked.connect(self._new_portfolio)
        mg.addWidget(QLabel("已有组合"))
        mg.addWidget(self.cmb_portfolio, 1)
        mg.addWidget(self.btn_switch)
        mg.addWidget(self.btn_new)
        c.add_layout(mg)

        self.lbl_account_check = QLabel("—")
        self.lbl_account_check.setWordWrap(True)
        c.add(self.lbl_account_check)
        self.root.addWidget(c)

    # ==================================================================
    def _build_assets(self) -> None:
        c = Card("持仓与权重设置（所有持仓数据都可以修改）")
        self.tbl = QTableWidget()
        self.tbl.setColumnCount(9)
        self.tbl.setHorizontalHeaderLabels(
            ["ID", "名称", "证券代码", "类别", "持有份额", "成本价", "目标权重%",
             "最小权重%", "最大权重%"])
        self.tbl.verticalHeader().setVisible(False)
        self.tbl.setMinimumHeight(240)
        c.add(self.tbl)
        row = QHBoxLayout()
        btn = QPushButton("从行情自动识别代码（按名称/代码搜索）")
        btn.clicked.connect(self._resolve_code)
        row.addWidget(btn)
        row.addStretch(1)
        c.add_layout(row)

        self.search_box = QWidget()
        sb = QVBoxLayout(self.search_box)
        sb.setContentsMargins(0, 0, 0, 0)
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("输入 ETF 名称或代码，例如：港股红利 / 159915")
        self.search_go = QPushButton("搜索")
        self.search_go.clicked.connect(lambda: self._do_search())
        self.search_input.returnPressed.connect(lambda: self._do_search())
        line = QHBoxLayout()
        line.addWidget(self.search_input, 1)
        line.addWidget(self.search_go)
        sb.addLayout(line)
        self.search_list = QListWidget()
        self.search_list.setMaximumHeight(120)
        self.search_list.itemDoubleClicked.connect(self._apply_search)
        sb.addWidget(self.search_list)
        self.search_box.setVisible(False)
        c.add(self.search_box)
        self.root.addWidget(c)

    def _resolve_code(self) -> None:
        self.search_box.setVisible(not self.search_box.isVisible())

    def _do_search(self) -> None:
        kw = self.search_input.text().strip()
        if not kw:
            return
        from data_sources.etf_source import ETFDataSource

        ds = ETFDataSource(self.cfg)
        res = ds.resolve_by_name(kw)
        self.search_list.clear()
        if not res:
            self.search_list.addItem("未找到结果（数据源不可用或关键词无匹配）")
            return
        for r in res:
            self.search_list.addItem(
                f"{r.get('name')}  {r.get('thscode')}  [{r.get('type') or r.get('asset_type') or ''}]")

    def _apply_search(self, item) -> None:
        parts = item.text().split()
        if len(parts) < 2 or "." not in parts[1]:
            return
        thscode = parts[1]
        row = self.tbl.currentRow()
        if row < 0:
            QMessageBox.information(self, "提示", "请先在表格中选中要填充的行。")
            return
        self.tbl.setItem(row, 2, QTableWidgetItem(thscode))
        self.tbl.setItem(row, 1, QTableWidgetItem(parts[0]))
        QMessageBox.information(self, "已填充",
                                f"已把第 {row + 1} 行代码设置为 {thscode}，"
                                "记得点击下方『保存全部设置』。")

    # ==================================================================
    def _build_targets(self) -> None:
        c = Card("战略目标比例（黄金硬上限 5%，不可突破）")
        g = QGridLayout()
        self.sp_bond = QDoubleSpinBox()
        self.sp_stock = QDoubleSpinBox()
        self.sp_gold = QDoubleSpinBox()
        for sp in (self.sp_bond, self.sp_stock, self.sp_gold):
            sp.setRange(0, 100)
            sp.setDecimals(1)
            sp.setSuffix(" %")
        self.sp_goldmax = QDoubleSpinBox()
        self.sp_goldmax.setRange(0, 100)
        self.sp_goldmax.setDecimals(1)
        self.sp_goldmax.setSuffix(" %")
        self.sp_batches = QSpinBox()
        self.sp_batches.setRange(1, 12)
        self.sp_rebw = QDoubleSpinBox()
        self.sp_rebw.setRange(0.5, 30)
        self.sp_rebw.setSuffix(" pp")
        self.sp_rebp = QDoubleSpinBox()
        self.sp_rebp.setRange(0.5, 50)
        self.sp_rebp.setSuffix(" pp")

        g.addWidget(QLabel("债券目标"), 0, 0)
        g.addWidget(self.sp_bond, 0, 1)
        g.addWidget(QLabel("股票目标"), 0, 2)
        g.addWidget(self.sp_stock, 0, 3)
        g.addWidget(QLabel("黄金目标"), 0, 4)
        g.addWidget(self.sp_gold, 0, 5)
        g.addWidget(QLabel("黄金绝对上限"), 1, 0)
        g.addWidget(self.sp_goldmax, 1, 1)
        g.addWidget(QLabel("分批份数"), 1, 2)
        g.addWidget(self.sp_batches, 1, 3)
        g.addWidget(QLabel("再平衡观察阈值"), 1, 4)
        g.addWidget(self.sp_rebw, 1, 5)
        c.add_layout(g)
        c.add(QLabel("再平衡优先阈值"))
        c.add(self.sp_rebp)
        self.root.addWidget(c)

    def _build_params(self) -> None:
        c = Card("风险与统计参数")
        g = QGridLayout()
        self.sp_rf = QDoubleSpinBox()
        self.sp_rf.setRange(0, 20)
        self.sp_rf.setDecimals(2)
        self.sp_rf.setSuffix(" %")
        self.sp_cost = QDoubleSpinBox()
        self.sp_cost.setRange(0, 5)
        self.sp_cost.setDecimals(3)
        self.sp_cost.setSuffix(" %")
        self.sp_train = QSpinBox()
        self.sp_train.setRange(60, 1500)
        self.sp_neighbors = QSpinBox()
        self.sp_neighbors.setRange(10, 600)
        self.sp_cooldown = QSpinBox()
        self.sp_cooldown.setRange(1, 120)
        self.sp_minsamples = QSpinBox()
        self.sp_minsamples.setRange(5, 300)
        self.sp_capmult = QDoubleSpinBox()
        self.sp_capmult.setRange(1, 30)
        self.sp_capmult.setDecimals(1)
        self.sp_years = QSpinBox()
        self.sp_years.setRange(1, 15)

        g.addWidget(QLabel("无风险利率"), 0, 0)
        g.addWidget(self.sp_rf, 0, 1)
        g.addWidget(QLabel("交易成本"), 0, 2)
        g.addWidget(self.sp_cost, 0, 3)
        g.addWidget(QLabel("历史数据年数"), 0, 4)
        g.addWidget(self.sp_years, 0, 5)
        g.addWidget(QLabel("最小训练天数"), 1, 0)
        g.addWidget(self.sp_train, 1, 1)
        g.addWidget(QLabel("相似样本上限"), 1, 2)
        g.addWidget(self.sp_neighbors, 1, 3)
        g.addWidget(QLabel("样本最小间隔(日)"), 1, 4)
        g.addWidget(self.sp_cooldown, 1, 5)
        g.addWidget(QLabel("最少有效样本"), 2, 0)
        g.addWidget(self.sp_minsamples, 2, 1)
        g.addWidget(QLabel("样本距离上限倍数"), 2, 2)
        g.addWidget(self.sp_capmult, 2, 3)
        c.add_layout(g)

        g2 = QGridLayout()
        self.sp_swing = {}
        labels = {"stock": "股票ETF", "gold": "黄金ETF", "bond": "债券ETF"}
        for i, (k, lbl) in enumerate(labels.items()):
            sp = QDoubleSpinBox()
            sp.setRange(0.5, 10)
            sp.setDecimals(1)
            sp.setSuffix(" ×ATR")
            self.sp_swing[k] = sp
            g2.addWidget(QLabel(f"波段阈值 {lbl}"), i, 0)
            g2.addWidget(sp, i, 1)
        c.add(QLabel("波段顶底 ATR 阈值（不同资产不同波动率）"))
        c.add_layout(g2)
        self.root.addWidget(c)

    def _build_sources(self) -> None:
        c = Card("数据源与刷新")
        g = QGridLayout()
        self.ed_key = QLineEdit()
        self.ed_key.setEchoMode(QLineEdit.Password)
        self.ed_key.setPlaceholderText("同花顺扶摇 API Key（可用环境变量 THS_API_KEY 覆盖）")
        self.sp_timeout = QSpinBox()
        self.sp_timeout.setRange(3, 60)
        self.sp_timeout.setSuffix(" 秒")
        self.sp_retries = QSpinBox()
        self.sp_retries.setRange(1, 8)
        self.sp_ttl = QSpinBox()
        self.sp_ttl.setRange(10, 10080)
        self.sp_ttl.setSuffix(" 分钟")
        self.sp_refresh = QSpinBox()
        self.sp_refresh.setRange(1, 1440)
        self.sp_refresh.setSuffix(" 分钟")
        self.cb_yahoo = QCheckBox("启用 Yahoo（金价 / DXY / GVZ / BPGDM代理）")
        self.cb_fred = QCheckBox("启用官方利率数据（美国财政部/FRED）")

        g.addWidget(QLabel("同花顺 API Key"), 0, 0)
        g.addWidget(self.ed_key, 0, 1, 1, 3)
        g.addWidget(QLabel("请求超时"), 1, 0)
        g.addWidget(self.sp_timeout, 1, 1)
        g.addWidget(QLabel("失败重试"), 1, 2)
        g.addWidget(self.sp_retries, 1, 3)
        g.addWidget(QLabel("ETF缓存有效期"), 2, 0)
        g.addWidget(self.sp_ttl, 2, 1)
        g.addWidget(QLabel("自动刷新间隔"), 2, 2)
        g.addWidget(self.sp_refresh, 2, 3)
        c.add_layout(g)
        c.add(self.cb_yahoo)
        c.add(self.cb_fred)
        self.tbl_status = QTableWidget()
        self.tbl_status.setColumnCount(4)
        self.tbl_status.setHorizontalHeaderLabels(["数据源", "状态", "数据时间", "说明"])
        self.tbl_status.verticalHeader().setVisible(False)
        self.tbl_status.setMinimumHeight(200)
        c.add(QLabel("当前数据源状态（联网获取 / 本地缓存 / 异常 一目了然）"))
        c.add(self.tbl_status)

        # ---- 数据来源与有效性（每项参数：来源 / 网址 / 状态 / 数据日期 / 读取时间到秒）----
        from core.version import APP_VERSION, BUILD_STAMP
        c.add(QLabel(f"数据来源与有效性（版本 v{APP_VERSION}，构建 {BUILD_STAMP or '源码态'}）"
                     "——每项参数都要能核对：来源、网址、正常还是错误、"
                     "数据日期、读取时间精确到秒）"))
        self.lbl_prov_head = QLabel("—")
        c.add(self.lbl_prov_head)
        self.tbl_prov = QTableWidget()
        self.tbl_prov.setColumnCount(7)
        self.tbl_prov.setHorizontalHeaderLabels(
            ["参数", "数据来源", "状态", "数据日期", "读取时间(到秒)",
             "说明（含错误原因 / 取数方式）", "网址"])
        self.tbl_prov.verticalHeader().setVisible(False)
        self.tbl_prov.setMinimumHeight(280)
        self.tbl_prov.setEditTriggers(QTableWidget.NoEditTriggers)
        c.add(self.tbl_prov)
        rowp = QHBoxLayout()
        btn_shot = QPushButton("打开来源截图存证（逐源实时界面图）")
        btn_shot.clicked.connect(self._open_prov_shots)
        rowp.addWidget(btn_shot)
        rowp.addStretch(1)
        c.add_layout(rowp)
        self.root.addWidget(c)

    def _open_prov_shots(self) -> None:
        """打开来源截图存证（由 tests/shot_sources.py 生成）。

        用户要求"贴出来源的实时界面图"，这里给一个入口直接看；
        没有存证时提示如何生成（不联网、不自动跑浏览器，避免误触发）。
        """
        d = os.path.join(str(Path(__file__).resolve().parent.parent),
                         "__shots", "sources")
        if not os.path.isdir(d):
            QMessageBox.information(
                self, "暂无来源截图存证",
                "还没有生成过来源截图存证。\n\n"
                "可在项目目录执行：\n\n"
                "    python tests/shot_sources.py\n\n"
                "会自动用本机 Chrome/Edge 逐源截图，生成后此按钮即可打开。")
            return
        idx = os.path.join(d, "INDEX.md")
        target = idx if os.path.exists(idx) else d
        try:
            if sys.platform.startswith("win"):
                os.startfile(target)  # noqa: S606  (仅 Windows 打开本地文件/目录)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", target])
            else:
                subprocess.Popen(["xdg-open", target])
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "打开失败", str(exc))

    def _build_autostart(self) -> None:
        c = Card("Windows 自动启动（任务计划程序）")
        c.add(QLabel("使用 Windows 任务计划程序创建每日任务；不需要管理员权限，"
                     "也不会保存任何密码。"))
        self.ed_times = QLineEdit()
        self.ed_times.setPlaceholderText("例如：09:00,18:00（英文逗号分隔）")
        c.add(self.ed_times)
        row = QHBoxLayout()
        b_on = QPushButton("启用自动启动")
        b_on.clicked.connect(self._enable_autostart)
        b_off = QPushButton("关闭自动启动")
        b_off.clicked.connect(self._disable_autostart)
        b_q = QPushButton("查看当前任务")
        b_q.clicked.connect(self._query_autostart)
        row.addWidget(b_on)
        row.addWidget(b_off)
        row.addWidget(b_q)
        row.addStretch(1)
        c.add_layout(row)
        self.txt_autostart = QPlainTextEdit()
        self.txt_autostart.setReadOnly(True)
        self.txt_autostart.setMaximumHeight(120)
        c.add(self.txt_autostart)
        self.root.addWidget(c)

    def _build_maintenance(self) -> None:
        c = Card("维护：日志 / 缓存 / 历史数据 / 配置")
        row = QHBoxLayout()
        for label, fn in (("查看日志", self._view_log),
                          ("打开日志目录", lambda: self._open_dir(LOG_DIR)),
                          ("查看缓存", self._view_cache),
                          ("清空缓存", self._clear_cache),
                          ("查看历史数据", self._view_history),
                          ("导出历史数据", self._export_history),
                          ("备份配置", self._backup_cfg)):
            b = QPushButton(label)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch(1)
        c.add_layout(row)
        self.txt_maint = QPlainTextEdit()
        self.txt_maint.setReadOnly(True)
        self.txt_maint.setMinimumHeight(220)
        c.add(self.txt_maint)
        self.root.addWidget(c)

        save_row = QHBoxLayout()
        b_save = QPushButton("保存全部设置并重新加载数据")
        b_save.setObjectName("Primary")
        b_save.clicked.connect(self._save)
        b_reload = QPushButton("放弃修改（重新读取配置文件）")
        b_reload.clicked.connect(self._load)
        save_row.addWidget(b_save)
        save_row.addWidget(b_reload)
        save_row.addStretch(1)
        self.root.addLayout(save_row)
        self.root.addWidget(QLabel("修改会写回 config/config.json，保存后立即生效。"))

    # ==================================================================
    def _refresh_portfolio_combo(self) -> None:
        """把已存在的组合填进下拉（当前活动组合默认选中）。"""
        if not hasattr(self, "cmb_portfolio"):
            return
        cur = str(self.cfg.get("portfolio.id", "all_weather_01"))
        try:
            items = self.cfg.list_portfolios()
        except Exception:  # noqa: BLE001
            items = []
        self.cmb_portfolio.blockSignals(True)
        self.cmb_portfolio.clear()
        sel = 0
        for it in items:
            label = (f"{it.get('name')}（{it.get('id')} · "
                     f"{it.get('assets')} 只 · {it.get('source')}）")
            self.cmb_portfolio.addItem(label, str(it.get("id")))
            if str(it.get("id")) == cur:
                sel = self.cmb_portfolio.count() - 1
        if self.cmb_portfolio.count():
            self.cmb_portfolio.setCurrentIndex(sel)
        self.cmb_portfolio.blockSignals(False)

    def _switch_portfolio(self) -> None:
        """切换到下拉选中的组合：只改活动组合 ID，不动任何持仓数据。"""
        pid = self.cmb_portfolio.currentData()
        if not pid:
            return
        cur = str(self.cfg.get("portfolio.id", ""))
        if str(pid) == cur:
            QMessageBox.information(self, "提示", "这已经是当前组合。")
            return
        try:
            self.cfg.switch_portfolio(str(pid))
            self.cfg.save()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "切换失败", f"{type(exc).__name__}: {exc}")
            return
        self._load()
        self.config_changed.emit()
        QMessageBox.information(
            self, "已切换",
            f"当前组合已切换为：{self.cfg.portfolio_name}（{self.cfg.portfolio_id}）。"
            "\n持仓/现金/目标/历史都按该组合独立读取。")

    def _new_portfolio(self) -> None:
        """新建一个独立组合；可选是否复制当前持仓。"""
        pid, ok = QInputDialog.getText(
            self, "新建组合", "组合 ID（建议英文/数字，如 gold_only_01）：")
        if not ok:
            return
        pid = (pid or "").strip()
        if not pid:
            QMessageBox.warning(self, "无效", "组合 ID 不能为空。")
            return
        if pid in (self.cfg.portfolios or {}):
            QMessageBox.warning(self, "已存在", f"组合 {pid} 已存在，请改用其它 ID。")
            return
        name, ok2 = QInputDialog.getText(self, "新建组合", "组合名称（可留空，默认用 ID）：")
        if not ok2:
            return
        copy_cur = (QMessageBox.question(
            self, "是否复制当前持仓",
            "是否把【当前组合】的持仓复制到新组合？\n"
            "· 是 = 以当前持仓为起点（随后可改）；\n"
            "· 否 = 新组合从空白开始。",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes)
        try:
            self.cfg.create_portfolio(pid, (name or "").strip() or pid,
                                      copy_from_current=copy_cur, make_active=True)
            self.cfg.save()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "新建失败", f"{type(exc).__name__}: {exc}")
            return
        self._load()
        self.config_changed.emit()
        QMessageBox.information(
            self, "已切换",
            f"已创建并切换到组合 {pid}。\n"
            "请补充持仓 / 总资产后点『保存全部设置并重新加载数据』。")

    # ==================================================================
    def _load(self) -> None:
        self.cfg = get_config(refresh=True)
        c = self.cfg
        self.ed_pid.setText(str(c.get("portfolio.id", "all_weather_01")))
        self.ed_pname.setText(str(c.get("portfolio.name", "当前全天候组合")))
        self._refresh_portfolio_combo()
        self.sp_total.setValue(float(c.get("portfolio.total_assets", 0.0) or 0.0))
        cash = c.get("portfolio.cash", None)
        self.ed_cash.setText("" if cash in (None, "") else str(cash))
        t = c.get("portfolio.target_class", {})
        self.sp_bond.setValue(float(t.get("bond", 0.5)) * 100)
        self.sp_stock.setValue(float(t.get("stock", 0.45)) * 100)
        self.sp_gold.setValue(float(t.get("gold", 0.05)) * 100)
        self.sp_goldmax.setValue(float(c.get("portfolio.gold_max", 0.05)) * 100)
        self.sp_batches.setValue(int(c.get("portfolio.batch_count", 4)))
        self.sp_rebw.setValue(float(c.get("portfolio.rebalance_watch_pct", 3.0)))
        self.sp_rebp.setValue(float(c.get("portfolio.rebalance_priority_pct", 5.0)))

        self.sp_rf.setValue(float(c.get("risk.risk_free_rate", 0.02)) * 100)
        self.sp_cost.setValue(float(c.get("cost.trade_cost_pct", 0.20)))
        self.sp_years.setValue(int(c.get("app.history_years", 5)))
        self.sp_train.setValue(int(c.get("hist.min_train_days", 300)))
        self.sp_neighbors.setValue(int(c.get("hist.n_neighbors", 200)))
        self.sp_cooldown.setValue(int(c.get("hist.cooldown_days", 10)))
        self.sp_minsamples.setValue(int(c.get("hist.min_samples", 50)))
        self.sp_capmult.setValue(float(c.get("hist.distance_cap_mult", 3.0)))
        for k, sp in self.sp_swing.items():
            sp.setValue(float(c.get(f"swing.atr_multiplier.{k}", 2.0)))

        self.ed_key.setText(str(c.get("data_sources.ths_api_key", "")))
        self.sp_timeout.setValue(int(c.get("app.timeout_seconds", 10)))
        self.sp_retries.setValue(int(c.get("app.max_retries", 3)))
        self.sp_ttl.setValue(int(c.get("data_sources.cache_ttl_minutes.etf", 720)))
        self.sp_refresh.setValue(int(c.get("app.refresh_minutes", 60)))
        self.cb_yahoo.setChecked(bool(c.get("data_sources.yahoo_enabled", True)))
        self.cb_fred.setChecked(bool(c.get("data_sources.fred_enabled", True)))
        self.ed_times.setText(",".join(c.get("app.autostart_times", ["09:00", "18:00"])))

        assets = c.assets
        self.tbl.setRowCount(len(assets))
        for i, a in enumerate(assets):
            vals = [a.get("id"), a.get("name"), a.get("thscode"),
                    {"bond": "债券", "stock": "股票", "gold": "黄金"}.get(
                        a.get("asset_class"), a.get("asset_class")),
                    a.get("shares"), a.get("cost"),
                    round(float(a.get("target_weight") or 0) * 100, 2),
                    round(float(a.get("min_weight") or 0) * 100, 2),
                    round(float(a.get("max_weight") or 0) * 100, 2)]
            for j, v in enumerate(vals):
                it = QTableWidgetItem("" if v is None else str(v))
                if j in (0, 3):
                    it.setFlags(it.flags() & ~Qt.ItemIsEditable)
                self.tbl.setItem(i, j, it)
        self.tbl.resizeColumnsToContents()

    def set_snapshot(self, snap: dict) -> None:
        self.snap = snap
        st = snap.get("data_status") or []
        self.tbl_status.setRowCount(len(st))
        for i, s in enumerate(st):
            for j, v in enumerate([s.get("name"), s.get("label"),
                                   s.get("data_time") or "-", s.get("message") or ""]):
                it = QTableWidgetItem("" if v is None else str(v))
                self.tbl_status.setItem(i, j, it)
        self.tbl_status.resizeColumnsToContents()
        self._fill_provenance(snap)
        cons = ((snap.get("portfolio") or {}).get("plan") or {}).get("consistency") or {}
        w = (snap.get("portfolio") or {}).get("weights") or {}
        if cons:
            if cons.get("ok"):
                self.lbl_account_check.setText(
                    f"√ 数据一致性通过 · 总资产 {w.get('total_assets'):,.0f} 元"
                    f"（ETF {w.get('etf_value'):,.0f} + 现金 {w.get('cash_value'):,.0f}）"
                    f" · 口径：{w.get('source')}")
                self.lbl_account_check.setStyleSheet("")
            else:
                self.lbl_account_check.setText(
                    "× 数据一致性异常：\n" + "\n".join("· " + e for e in cons.get("errors", []))
                    + "\n（系统已禁止生成任何买入金额建议，请修正持仓/总资产后保存）")
                self.lbl_account_check.setStyleSheet(
                    f"color: {palette()['danger']};")
        self._load()

    def _fill_provenance(self, snap: dict) -> None:
        """填「数据来源与有效性」表：来源 / 网址 / 状态 / 数据日期 / 读取时间(到秒)。

        错误行标红并给出原因——用户要能一眼看出某项数据是"正常"还是"错误"。
        """
        prov = snap.get("provenance") or {}
        rows = prov.get("rows") or []
        if hasattr(self, "lbl_prov_head"):
            self.lbl_prov_head.setText(prov.get("headline") or "暂无来源信息")
        if not hasattr(self, "tbl_prov"):
            return
        pal = palette()
        self.tbl_prov.setRowCount(len(rows))
        for i, r in enumerate(rows):
            st = r.get("status")
            mark = r.get("status_mark") or {
                "OK": "✅ 正常（实时）", "CACHE": "○ 缓存（未过期·本次未联网）",
                "CACHE_FALLBACK": "⚠ 缓存（实时源失败）",
                "ERROR": "❌ 错误",
                "UNCONFIGURED": "○ 未配置"}.get(st, "— 未尝试")
            note = r.get("detail") or ""
            if (r.get("mode") and r.get("mode") != "本地计算"
                    and st in ("OK", "ERROR", "CACHE", "CACHE_FALLBACK")):
                note = (note + "｜" if note else "") + str(r["mode"])
            vals = [
                r.get("label") or "",
                r.get("source") or "",
                mark,
                r.get("data_date") or "—",
                r.get("updated_at") or "—",
                note,
            ]
            for j, v in enumerate(vals):
                it = QTableWidgetItem("" if v is None else str(v))
                if j == 2:
                    if st == "ERROR":
                        it.setForeground(QColor(pal["danger"]))
                        it.setToolTip(r.get("detail") or "取数失败")
                    elif st == "CACHE_FALLBACK":
                        # 实时源失败、退回旧缓存：用警示色，别让用户误以为实时。
                        it.setForeground(QColor(pal.get("warn") or pal["danger"]))
                        it.setToolTip("实时源本次不可达，当前显示的是本地缓存："
                                      + str(r.get("detail") or ""))
                    elif st == "CACHE":
                        # 命中未过期缓存（本次有意不联网，属正常策略）→ 中性色
                        it.setForeground(QColor(pal.get("muted") or pal["ok"]))
                        it.setToolTip("本次直接使用未过期的本地缓存（未联网）；"
                                      "点【立即刷新】可强制取实时。")
                    elif st == "OK":
                        it.setForeground(QColor(pal["ok"]))
                self.tbl_prov.setItem(i, j, it)
            url = r.get("url") or ""
            if url:
                lb = QLabel(f'<a href="{url}">打开核对</a>')
                lb.setOpenExternalLinks(True)
                lb.setToolTip(url)
                self.tbl_prov.setCellWidget(i, 6, lb)
            else:
                self.tbl_prov.setItem(i, 6, QTableWidgetItem("—"))
        self.tbl_prov.resizeColumnsToContents()

    def refresh_theme(self) -> None:
        pass

    # ==================================================================
    def _save(self) -> None:
        c = self.cfg
        try:
            # ---- 组合账户（隔离 + 资金口径）----
            old_pid = str(c.get("portfolio.id", "all_weather_01"))
            old_name = str(c.get("portfolio.name", old_pid))
            pid = self.ed_pid.text().strip() or old_pid
            pname = self.ed_pname.text().strip() or pid
            old_map = {str(a.get("id")): a for a in c.assets}
            if pid != old_pid:
                # 换了组合 ID：先把旧组合的持仓归档到它自己的位置，
                # 再为新组合建立独立条目，绝不共用同一份持仓数据。
                c.set(f"portfolios.{old_pid}",
                      {"name": old_name, "assets": list(c.assets)}, autosave=False)
                c.set(f"portfolios.{pid}", {"name": pname, "assets": []}, autosave=False)
            c.set("portfolio.id", pid, autosave=False)
            c.set("portfolio.name", pname, autosave=False)
            c.set("portfolio.total_assets", float(self.sp_total.value()), autosave=False)
            cash_txt = self.ed_cash.text().strip()
            c.set("portfolio.cash", _num(cash_txt, None) if cash_txt else None,
                  autosave=False)

            # 持仓表
            assets = []
            for i in range(self.tbl.rowCount()):
                def cell(j):
                    it = self.tbl.item(i, j)
                    return it.text().strip() if it else ""

                aid = cell(0)
                cls = {"债券": "bond", "股票": "stock", "黄金": "gold"}.get(cell(3), cell(3))
                old = old_map.get(str(aid)) or {}
                shares = _num(cell(4), 0.0)
                cost = _num(cell(5), None)
                tw = _num(cell(6), 0.0) / 100.0
                lo = _num(cell(7), 0.0) / 100.0
                hi = _num(cell(8), 100.0) / 100.0
                if cls == "gold":
                    hi = min(hi, float(self.sp_goldmax.value()) / 100.0)
                code = cell(2) or old.get("thscode", "")
                a = dict(old)
                a.update({
                    "id": aid, "name": cell(1), "thscode": code,
                    "ticker": code.split(".")[0] if "." in code else code,
                    "exchange": code.split(".")[1] if "." in code else a.get("exchange", ""),
                    "asset_class": cls, "shares": shares, "cost": cost,
                    "target_weight": tw, "min_weight": lo, "max_weight": hi,
                    "enabled": True,
                })
                assets.append(a)
            if assets:
                # 只写回【当前组合】自己的位置，绝不写入其他组合
                c.set_assets(assets)

            # 目标比例
            c.set("portfolio.target_class", {
                "bond": self.sp_bond.value() / 100.0,
                "stock": self.sp_stock.value() / 100.0,
                "gold": self.sp_gold.value() / 100.0,
            }, autosave=False)
            gold_max = self.sp_goldmax.value() / 100.0
            c.set("portfolio.gold_max", gold_max, autosave=False)
            c.set("portfolio.batch_count", self.sp_batches.value(), autosave=False)
            c.set("portfolio.rebalance_watch_pct", self.sp_rebw.value(), autosave=False)
            c.set("portfolio.rebalance_priority_pct", self.sp_rebp.value(), autosave=False)

            c.set("risk.risk_free_rate", self.sp_rf.value() / 100.0, autosave=False)
            c.set("cost.trade_cost_pct", self.sp_cost.value(), autosave=False)
            c.set("app.history_years", self.sp_years.value(), autosave=False)
            c.set("hist.min_train_days", self.sp_train.value(), autosave=False)
            c.set("hist.n_neighbors", self.sp_neighbors.value(), autosave=False)
            c.set("hist.cooldown_days", self.sp_cooldown.value(), autosave=False)
            c.set("hist.min_samples", self.sp_minsamples.value(), autosave=False)
            c.set("hist.distance_cap_mult", self.sp_capmult.value(), autosave=False)
            for k, sp in self.sp_swing.items():
                c.set(f"swing.atr_multiplier.{k}", sp.value(), autosave=False)

            c.set("data_sources.ths_api_key", self.ed_key.text().strip(), autosave=False)
            c.set("app.timeout_seconds", self.sp_timeout.value(), autosave=False)
            c.set("app.max_retries", self.sp_retries.value(), autosave=False)
            c.set("data_sources.cache_ttl_minutes.etf", self.sp_ttl.value(), autosave=False)
            c.set("app.refresh_minutes", self.sp_refresh.value(), autosave=False)
            c.set("data_sources.yahoo_enabled", self.cb_yahoo.isChecked(), autosave=False)
            c.set("data_sources.fred_enabled", self.cb_fred.isChecked(), autosave=False)
            times = [t.strip() for t in self.ed_times.text().split(",") if t.strip()]
            c.set("app.autostart_times", times or ["09:00", "18:00"], autosave=False)

            c.save()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "保存失败", f"{type(exc).__name__}: {exc}")
            return
        QMessageBox.information(self, "已保存", "配置已写入 config/config.json，正在重新加载数据。")
        self.config_changed.emit()

    # ==================================================================
    def _enable_autostart(self) -> None:
        times = [t.strip() for t in self.ed_times.text().split(",") if t.strip()]
        if not times:
            QMessageBox.information(self, "提示", "请先填写时间，例如 09:00,18:00")
            return
        ok, msg = autostart.enable(times)
        self.txt_autostart.setPlainText(msg)
        if not ok:
            QMessageBox.warning(self, "部分失败", msg)

    def _disable_autostart(self) -> None:
        ok, msg = autostart.disable()
        self.txt_autostart.setPlainText(msg)

    def _query_autostart(self) -> None:
        tasks = autostart.list_tasks()
        if not tasks:
            self.txt_autostart.setPlainText("当前没有本程序创建的计划任务。")
            return
        self.txt_autostart.setPlainText("\n".join(
            f"{t.get('name')}  状态={t.get('status', '-')}  "
            f"下次={t.get('next_run', '-')}\n    {t.get('command', '')}"
            for t in tasks))

    # ==================================================================
    def _view_log(self) -> None:
        p = Path(LOG_DIR) / "app.log"
        if not p.exists():
            self.txt_maint.setPlainText("暂无日志文件。")
            return
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception as exc:  # noqa: BLE001
            text = f"读取失败：{exc}"
        lines = text.splitlines()[-300:]
        self.txt_maint.setPlainText("\n".join(lines))

    def _open_dir(self, d) -> None:
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(d))  # noqa: S606
            else:
                subprocess.Popen(["xdg-open", str(d)])
        except Exception as exc:  # noqa: BLE001
            self.txt_maint.setPlainText(f"打开目录失败：{exc}")

    def _view_cache(self) -> None:
        from data_sources.http_util import DATA_CACHE

        items = DATA_CACHE.list_items()
        if not items:
            self.txt_maint.setPlainText("缓存为空。")
            return
        lines = [f"{'键':<22}{'数据时间':<14}{'抓取时间':<22}{'大小KB':>8}"]
        for it in items:
            lines.append(f"{str(it.get('key'))[:20]:<22}{str(it.get('data_time'))[:12]:<14}"
                         f"{str(it.get('fetched_at'))[:20]:<22}{it.get('size_kb'):>8}")
        lines.append("")
        lines.append("说明：缓存仅用于数据源失败时的降级显示，界面会明确标注『缓存数据』，"
                     "绝不把缓存伪装成最新数据。")
        self.txt_maint.setPlainText("\n".join(lines))

    def _clear_cache(self) -> None:
        r = QMessageBox.question(self, "确认", "确定清空本地缓存？下次将重新抓取全部数据。")
        if r != QMessageBox.Yes:
            return
        from data_sources.http_util import DATA_CACHE

        n = DATA_CACHE.clear()
        self.txt_maint.setPlainText(f"已清空 {n} 个缓存文件。")

    def _view_history(self) -> None:
        files = list_history_files()
        if not files:
            self.txt_maint.setPlainText("暂无历史数据文件（运行一次完整刷新后会自动生成）。")
            return
        lines = [f"{'文件':<28}{'行数':>8}{'大小KB':>10}  修改时间"]
        for f in files:
            lines.append(f"{f['name']:<28}{f['rows']:>8}{f['size_kb']:>10}  {f['modified']}")
        self.txt_maint.setPlainText("\n".join(lines))

    def _export_history(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "选择导出目录")
        if not d:
            return
        try:
            out = export_all(Path(d) / f"GoldETF-export-{datetime.now():%Y%m%d-%H%M%S}")
            self.txt_maint.setPlainText("已导出：\n" + "\n".join(str(p) for p in out))
        except Exception as exc:  # noqa: BLE001
            self.txt_maint.setPlainText(f"导出失败：{exc}")

    def _backup_cfg(self) -> None:
        try:
            p = self.cfg.backup()
            self.txt_maint.setPlainText(f"配置已备份到：{p}")
        except Exception as exc:  # noqa: BLE001
            self.txt_maint.setPlainText(f"备份失败：{exc}")


def _num(text: str, default):
    try:
        return float(text)
    except (TypeError, ValueError):
        return default
