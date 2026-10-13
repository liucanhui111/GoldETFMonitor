"""主窗口：左侧导航 + 右侧页面 + 顶部工具条。"""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QComboBox, QFrame, QHBoxLayout,
                               QLabel, QMainWindow, QMessageBox, QProgressBar, QPushButton,
                               QSizePolicy, QStackedWidget, QToolBar, QVBoxLayout, QWidget)

from core.config import get_config

from .etf_page import EtfPage
from .gold_page import GoldPage
from .portfolio_page import PortfolioPage
from .settings_page import SettingsPage
from .strategy_page import StrategyPage
from .theme import palette, set_theme
from .worker import LoadWorker
from core.version import APP_VERSION, BUILD_STAMP


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.cfg = get_config(refresh=True)
        self.snap: dict | None = None
        self.worker: LoadWorker | None = None

        self.setWindowTitle(
            f"个人全天候 ETF 量化资产配置与择时系统 v{APP_VERSION}"
            f"（构建 {BUILD_STAMP or '源码态'}）")
        self.resize(1400, 920)

        self._build_toolbar()
        self._build_body()
        self._build_status()

        self.timer = QTimer(self)
        self.timer.timeout.connect(lambda: self.reload(silent=True))
        self._apply_interval()

        QTimer.singleShot(150, self.reload)

    # ------------------------------------------------------------------
    def _build_toolbar(self) -> None:
        tb = QToolBar()
        tb.setMovable(False)
        self.addToolBar(tb)

        title = QLabel("  全天候 ETF 组合系统  ")
        title.setStyleSheet("font-size:14px; font-weight:700;")
        tb.addWidget(title)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(spacer)

        self.cb_simple = QCheckBox("新手模式")
        self.cb_simple.setToolTip(
            "新手模式（默认）：首页只显示『今天怎么做 + 今天投入多少 + 买哪只 + "
            "组合60日概率 + 为什么 + 下一次条件』，其余参数全部收进专业模式。\n"
            "取消勾选进入专业模式，可看到全部指标。")
        self.cb_simple.setChecked(bool(self.cfg.get("app.simple_mode", True)))
        self.cb_simple.stateChanged.connect(self._on_simple_mode)
        tb.addWidget(self.cb_simple)

        self.cb_cache = QCheckBox("优先使用缓存（秒开）")
        self.cb_cache.setChecked(True)
        self.cb_cache.setToolTip(
            "勾选：缓存未过期就直接用，启动只要几秒（日线数据当天更新一次足够）。\n"
            "取消勾选：每次都重新联网下载全部数据，网络慢时可能要等 1 分钟。\n"
            "『立即刷新』按钮始终强制联网更新，不看缓存。")
        self.cb_macro = QCheckBox("加载宏观/黄金数据")
        self.cb_macro.setChecked(True)
        tb.addWidget(self.cb_cache)
        tb.addWidget(self.cb_macro)

        tb.addWidget(QLabel("  自动刷新  "))
        self.cmb_interval = QComboBox()
        self.cmb_interval.addItems(["关闭", "15 分钟", "30 分钟", "60 分钟", "120 分钟"])
        self.cmb_interval.setCurrentIndex(3)
        self.cmb_interval.currentIndexChanged.connect(self._apply_interval)
        tb.addWidget(self.cmb_interval)

        self.btn_refresh = QPushButton("立即刷新")
        self.btn_refresh.setObjectName("Primary")
        self.btn_refresh.setToolTip("强制联网更新全部数据（忽略缓存）")
        self.btn_refresh.clicked.connect(lambda: self.reload(force=True))
        tb.addWidget(self.btn_refresh)

        tb.addWidget(QLabel("  "))
        self.btn_theme = QPushButton("深色")
        self.btn_theme.clicked.connect(self._toggle_theme)
        tb.addWidget(self.btn_theme)

    def _on_simple_mode(self, state) -> None:
        page = getattr(self, "page_portfolio", None)
        if page is not None:
            page.set_simple(bool(state))
        try:
            self.cfg.set("app.simple_mode", bool(state))
        except Exception:
            pass

    def _toggle_theme(self) -> None:
        dark = not (palette().get("bg") == "#14171c")
        set_theme(dark)
        from .theme import apply_theme
        from PySide6.QtWidgets import QApplication
        apply_theme(QApplication.instance())
        self.btn_theme.setText("浅色" if dark else "深色")
        for page in (self.page_portfolio, self.page_etf, self.page_gold,
                     self.page_strategy, self.page_settings):
            page.refresh_theme()
        self._refresh_status()

    def _build_body(self) -> None:
        central = QWidget()
        root = QHBoxLayout(central)
        root.setContentsMargins(10, 10, 10, 8)
        root.setSpacing(10)

        # 左侧导航
        nav = QFrame()
        nav.setObjectName("Card")
        nav.setFixedWidth(148)
        nl = QVBoxLayout(nav)
        nl.setContentsMargins(8, 10, 8, 10)
        nl.setSpacing(6)
        self.nav_group = QButtonGroup(self)
        for i, (label, tip) in enumerate([("组合总览", "组合层决策与统计"),
                                          ("ETF 明细", "单只 ETF 全部指标"),
                                          ("黄金ETF", "159937 黄金专项监控"),
                                          ("策略有效性", "四概率体系与模型可信度"),
                                          ("设置", "持仓/参数/数据源/启动")]):
            b = QPushButton(label)
            b.setObjectName("Nav")
            b.setCheckable(True)
            b.setChecked(i == 0)
            b.setToolTip(tip)
            self.nav_group.addButton(b, i)
            nl.addWidget(b)
        nl.addStretch(1)
        self.lbl_nav_status = QLabel("—")
        self.lbl_nav_status.setObjectName("Muted")
        self.lbl_nav_status.setWordWrap(True)
        nl.addWidget(self.lbl_nav_status)

        self.stack = QStackedWidget()
        self.page_portfolio = PortfolioPage(self.cfg)
        self.page_etf = EtfPage(self.cfg)
        self.page_gold = GoldPage(self.cfg)
        self.page_strategy = StrategyPage(self.cfg)
        self.page_settings = SettingsPage(self.cfg)
        self.page_settings.config_changed.connect(lambda: self.reload())
        # 执行台账：用户在结论卡点「我已按此执行」后重算（持仓已变，建议随之更新）
        self.page_portfolio.config_changed.connect(lambda: self.reload())
        for p in (self.page_portfolio, self.page_etf, self.page_gold,
                  self.page_strategy, self.page_settings):
            self.stack.addWidget(p)
        self.nav_group.idClicked.connect(self.stack.setCurrentIndex)

        root.addWidget(nav)
        root.addWidget(self.stack, 1)
        self.setCentralWidget(central)

        # 应用启动时的模式（默认新手模式）：结论优先，参数收进专业模式
        self.page_portfolio.set_simple(self.cb_simple.isChecked())

    def _build_status(self) -> None:
        sb = self.statusBar()
        self.pbar = QProgressBar()
        self.pbar.setRange(0, 0)
        self.pbar.setFixedWidth(140)
        self.pbar.setVisible(False)
        self.lbl_msg = QLabel("就绪")
        self.lbl_src = QLabel("")
        sb.addWidget(self.lbl_msg, 1)
        sb.addPermanentWidget(self.pbar)
        sb.addPermanentWidget(self.lbl_src)

    # ------------------------------------------------------------------
    def _apply_interval(self) -> None:
        idx = self.cmb_interval.currentIndex()
        minutes = {0: 0, 1: 15, 2: 30, 3: 60, 4: 120}.get(idx, 60)
        if minutes <= 0:
            self.timer.stop()
        else:
            self.timer.start(minutes * 60_000)

    def reload(self, silent: bool = False, force: bool = False) -> None:
        if self.worker and self.worker.isRunning():
            return
        cfg = get_config(refresh=True)
        self.cfg = cfg
        self.btn_refresh.setEnabled(False)
        self.pbar.setVisible(True)
        self.lbl_msg.setText("正在获取数据…")
        if not silent:
            self.page_portfolio.set_loading(True)
            self.page_portfolio.set_progress("正在准备数据…")
            self.page_gold.set_loading(True)

        # force=True（『立即刷新』）时忽略缓存，强制联网；自动刷新/启动走缓存优先
        cache_first = self.cb_cache.isChecked() and not force
        self.worker = LoadWorker(cfg,
                                 cache_first=cache_first,
                                 with_macro=self.cb_macro.isChecked())
        self.worker.progress.connect(self._on_progress)
        self.worker.done.connect(self._on_done)
        self.worker.failed.connect(self._on_failed)
        self.worker.start()

    def _on_progress(self, msg: str) -> None:
        self.lbl_msg.setText(msg)
        self.page_portfolio.set_progress(msg)

    def _on_done(self, snap: dict) -> None:
        self.snap = snap
        self.pbar.setVisible(False)
        self.btn_refresh.setEnabled(True)
        self.page_portfolio.set_loading(False)
        self.page_portfolio.set_snapshot(snap)
        self.page_etf.set_snapshot(snap)
        self.page_gold.set_snapshot(snap)
        self.page_strategy.set_snapshot(snap)
        self.page_settings.set_snapshot(snap)
        if snap.get("ok"):
            p = snap["portfolio"]
            self.lbl_msg.setText(
                f"更新完成 · {snap['ts']:%H:%M:%S} · 市场状态 {p['risk']['state']} · "
                f"信号 {p['entry']['grade']} / {p['decision']['action']} · "
                f"耗时 {snap.get('elapsed_sec')}s")
        else:
            self.lbl_msg.setText("更新完成，但数据不足：" +
                                 "；".join(snap.get("warnings", [])[:2]))
        self._refresh_status()
        self._apply_interval()

    def _on_failed(self, text: str) -> None:
        self.pbar.setVisible(False)
        self.btn_refresh.setEnabled(True)
        self.page_portfolio.set_loading(False)
        self.page_gold.set_loading(False)
        self.lbl_msg.setText("加载失败")
        QMessageBox.critical(self, "加载失败",
                             "数据加载过程出现异常：\n\n" + text[:1500])

    def _refresh_status(self) -> None:
        st = (self.snap or {}).get("data_status") or []
        bad = [s for s in st if not s.get("ok")]
        cached = [s for s in st if s.get("ok") and s.get("from_cache")]
        parts = [f"数据源 {len(st) - len(bad)}/{len(st)} 正常"]
        if cached:
            parts.append(f"{len(cached)} 项使用缓存")
        if bad:
            parts.append(f"{len(bad)} 项异常")
        self.lbl_src.setText(" · ".join(parts) if st else "")
        self.lbl_nav_status.setText(
            "\n".join(parts) if st else "尚未加载数据")
        pal = palette()
        self.lbl_src.setStyleSheet(
            f"color: {pal['danger'] if bad else pal['muted']}; padding-right:8px;")

    def closeEvent(self, ev):  # noqa: N802
        if self.worker and self.worker.isRunning():
            self.worker.terminate()
            self.worker.wait(1500)
        super().closeEvent(ev)
