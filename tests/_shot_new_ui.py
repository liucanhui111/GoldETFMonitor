"""离屏截取本轮新 UI：①首屏「数据陈旧」横幅 ②设置页「多组合」行。

运行：
    QT_QPA_PLATFORM=offscreen python tests/_shot_new_ui.py
输出到 __shots/。
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

OUT = os.path.join(os.path.dirname(_HERE), "__shots")


def main() -> int:
    from PySide6.QtWidgets import QApplication

    from core.config import get_config
    from core.pipeline import run_full
    from ui.main_window import MainWindow
    from ui.theme import apply_theme

    os.makedirs(OUT, exist_ok=True)
    app = QApplication(sys.argv)
    apply_theme(app)
    cfg = get_config(refresh=True)
    snap = run_full(cfg, progress=None, use_cache_first=True, with_macro=False,
                    save=False)
    win = MainWindow()
    win.resize(1240, 980)
    win.show()

    def grab(name: str) -> None:
        for _ in range(8):
            app.processEvents()
            win.repaint()
        path = os.path.join(OUT, name)
        win.grab().save(path)
        print(f"已导出 {path}")

    # ① 首屏「数据陈旧」横幅：注入合成 stale freshness（数据含锁对象→浅拷贝替换键）
    s = dict(snap)
    s["freshness"] = {
        "ok": False, "action": "DATA_STALE", "quote_basis": "最近交易日收盘",
        "rows": [{"key": "etf_close", "label": "A股ETF收盘",
                  "data_date": "2026-09-30", "age_days": 12,
                  "allowed_days": 7, "status": "STALE"},
                 {"key": "gold", "label": "黄金", "data_date": "2026-09-28",
                  "age_days": 14, "allowed_days": 7, "status": "STALE"}]}
    win.page_portfolio.set_snapshot(s)
    win.resize(1240, 1080)
    grab("shot_stale_banner.png")

    # ② 设置页「多组合」行
    win.page_settings.set_snapshot(snap)
    win.stack.setCurrentIndex(4)
    win.resize(1240, 1080)
    grab("shot_settings_portfolios.png")

    win.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
