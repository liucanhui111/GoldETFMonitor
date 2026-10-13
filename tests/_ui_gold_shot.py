"""离屏渲染：主窗口 + 切到「黄金ETF」页截图（加载系统字体）。"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, r"F:\etf02\GoldETFMonitor")

from PySide6.QtGui import QFont, QFontDatabase  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication(sys.argv)
for f in (r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\simhei.ttf",
          r"C:\Windows\Fonts\simsun.ttc"):
    if os.path.exists(f):
        QFontDatabase.addApplicationFont(f)
app.setFont(QFont("Microsoft YaHei", 9))

from core.config import get_config  # noqa: E402
from core.pipeline import run_full  # noqa: E402

cfg = get_config(refresh=True)
snap = run_full(cfg, progress=None, use_cache_first=True, with_macro=True, save=False)

from ui.theme import apply_theme  # noqa: E402

apply_theme(app)
from ui.main_window import MainWindow  # noqa: E402

win = MainWindow()
win.resize(1560, 1600)
win._on_done(snap)
win.stack.setCurrentIndex(2)
for _ in range(12):
    app.processEvents()

out_dir = r"F:\etf02\GoldETFMonitor\_uishot"
os.makedirs(out_dir, exist_ok=True)

page = win.page_gold
page.setMinimumHeight(1500)
app.processEvents()
p = os.path.join(out_dir, "gold_page.png")
page.grab().save(p)
print("saved", p, "size", page.size().width(), "x", page.size().height())

from PySide6.QtWidgets import QLabel  # noqa: E402

lines = [l.text().strip() for l in page.findChildren(QLabel) if l.text().strip()]
with open(os.path.join(out_dir, "gold_text.txt"), "w", encoding="utf-8") as fh:
    fh.write("\n".join(lines))
print("labels:", len(lines))
print("DONE")
