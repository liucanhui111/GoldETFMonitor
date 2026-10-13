"""离屏渲染：主窗口 + 切到「黄金与宏观」tab 截图（加载系统字体）。"""
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
win.resize(1560, 1500)
win._on_done(snap)
app.processEvents()

out_dir = r"F:\etf02\GoldETFMonitor\_uishot"
os.makedirs(out_dir, exist_ok=True)

tabs = win.page_portfolio.tabs
print("标签页清单:", [tabs.tabText(i) for i in range(tabs.count())])

win.stack.setCurrentIndex(0)
for i in range(tabs.count()):
    name = tabs.tabText(i)
    if "宏观" in name:
        tabs.setCurrentIndex(i)
        for _ in range(10):
            app.processEvents()
        w = tabs.widget(i)
        w.setMinimumHeight(1400)
        app.processEvents()
        p = os.path.join(out_dir, f"macro_widget_{i}.png")
        w.grab().save(p)
        print("saved", p, "->", name, "size", w.size().width(), "x", w.size().height())
        # 同步导出该页全部可见文本，便于核对
        from PySide6.QtWidgets import QLabel
        lines = [l.text().strip() for l in w.findChildren(QLabel) if l.text().strip()]
        txt_p = os.path.join(out_dir, f"macro_text_{i}.txt")
        with open(txt_p, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines))
        print("text ->", txt_p, len(lines), "labels")

print("DONE")
