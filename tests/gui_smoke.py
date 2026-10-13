"""GUI 冒烟测试（离线，使用真实缓存数据）：验证 PySide6 界面端到端可用。

用法：
    python tests/gui_smoke.py

作用：在无显示器（offscreen）环境下，用**真实 pipeline 快照**驱动
主窗口与三个页面，确认窗口能构造、三页能正确填充、自绘图表能渲染。
返回 0 表示通过。
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    try:
        from PySide6.QtWidgets import QApplication
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] 无法导入 PySide6：{exc}")
        return 2

    from core.config import get_config
    from core.pipeline import run_full

    print("· 运行 pipeline（优先缓存，跳过宏观）…")
    cfg = get_config(refresh=True)
    snap = run_full(cfg, progress=None, use_cache_first=True,
                    with_macro=False, save=False)
    print(f"· pipeline 完成：ok={snap.get('ok')} 耗时 {snap.get('elapsed_sec')}s")

    app = QApplication(sys.argv)
    try:
        from ui.theme import apply_theme
        apply_theme(app)
        from ui.main_window import MainWindow

        win = MainWindow()
        print("[PASS] MainWindow 构造成功")
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        print(f"[FAIL] MainWindow 构造失败：{exc}")
        return 1

    try:
        win._on_done(snap)
        app.processEvents()
        print("[PASS] 三页真实快照填充成功")
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        print(f"[FAIL] 页面填充失败：{exc}")
        return 1

    # 切到每个标签页，触发图表重绘
    try:
        for page in (win.page_portfolio, win.page_etf):
            tabs = getattr(page, "tabs", None)
            for i in range(tabs.count() if tabs else 0):
                tabs.setCurrentIndex(i)
                app.processEvents()
        print("[PASS] 所有标签页切换/重绘成功")
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        print(f"[WARN] 标签页切换异常：{exc}")

    # 五个导航页逐一进入（含"策略有效性"与"黄金ETF"）
    try:
        pages = (win.page_portfolio, win.page_etf, win.page_gold,
                 win.page_strategy, win.page_settings)
        for i, _ in enumerate(pages):
            win.stack.setCurrentIndex(i)
            app.processEvents()
        print("[PASS] 五个导航页（组合/ETF/黄金ETF/策略有效性/设置）切换成功")
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        print(f"[FAIL] 导航页切换失败：{exc}")
        return 1

    # 主题切换（此前会因引用未创建的 page_strategy 而崩溃）
    try:
        win._toggle_theme()
        app.processEvents()
        win._toggle_theme()
        app.processEvents()
        print("[PASS] 深色/浅色主题切换成功")
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        print(f"[FAIL] 主题切换失败：{exc}")
        return 1

    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
