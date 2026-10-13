"""后台数据加载线程（避免界面卡死）。"""
from __future__ import annotations

import traceback

from PySide6.QtCore import QThread, Signal


class LoadWorker(QThread):
    progress = Signal(str)
    done = Signal(dict)
    failed = Signal(str)

    def __init__(self, cfg, *, cache_first: bool = False, with_macro: bool = True,
                 save: bool = True, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.cache_first = cache_first
        self.with_macro = with_macro
        self.save = save

    def run(self):  # noqa: D102
        try:
            from core.pipeline import run_full

            snap = run_full(self.cfg, progress=self.progress.emit,
                            use_cache_first=self.cache_first,
                            with_macro=self.with_macro, save=self.save)
            self.done.emit(snap)
        except Exception:  # noqa: BLE001
            self.failed.emit(traceback.format_exc())


class BacktestWorker(QThread):
    progress = Signal(str)
    done = Signal(dict)
    failed = Signal(str)

    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.cfg = cfg

    def run(self):  # noqa: D102
        try:
            from core.backtest import run_backtest

            self.done.emit(run_backtest(self.cfg, progress=self.progress.emit))
        except Exception:  # noqa: BLE001
            self.failed.emit(traceback.format_exc())


class StrategyValidateWorker(QThread):
    """策略有效性验证（逐日滚动样本外，较慢）。"""
    progress = Signal(str)
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.cfg = cfg

    def run(self):  # noqa: D102
        try:
            from core.strategy_validation import run_and_write

            res, path = run_and_write(self.cfg, progress=self.progress.emit)
            self.done.emit({"res": res, "path": str(path)})
        except Exception:  # noqa: BLE001
            self.failed.emit(traceback.format_exc())
