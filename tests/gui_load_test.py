"""端到端 GUI 加载测试（离屏）：走真实的 LoadWorker → pipeline → 三页填充路径。

用途：复现用户"打开 EXE 后一直显示正在加载"的场景，量化首次加载耗时。
运行：QT_QPA_PLATFORM=offscreen python tests/gui_load_test.py [--macro] [--cache]
"""
from __future__ import annotations

import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.dirname(_ROOT))

WANT_MACRO = "--no-macro" not in sys.argv
# 与程序默认一致：优先使用缓存（秒开）。加 --no-cache 可测"强制全量联网"的冷启动。
CACHE_FIRST = "--no-cache" not in sys.argv


def main() -> int:
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    from ui.main_window import MainWindow

    app = QApplication(sys.argv)
    win = MainWindow()
    win.cb_macro.setChecked(WANT_MACRO)
    win.cb_cache.setChecked(CACHE_FIRST)
    win.show()

    t0 = time.time()
    state = {"done": False, "msgs": 0}

    def on_progress(msg: str) -> None:
        state["msgs"] += 1
        print(f"  [{time.time() - t0:5.1f}s] 进度: {msg}", flush=True)

    def poll() -> None:
        if win.worker and not getattr(win, "_hooked", False):
            win._hooked = True
            win.worker.progress.connect(on_progress)
        if win.snap is not None:
            state["done"] = True
            el = time.time() - t0
            snap = win.snap
            ok = snap.get("ok")
            print(f"[结果] ok={ok}  总耗时={el:.1f}s  进度消息={state['msgs']} 条")
            print(f"[结果] 数据源: {len(snap.get('data_status') or [])} 项，"
                  f"异常 {sum(1 for s in (snap.get('data_status') or []) if not s.get('ok'))} 项")
            if ok:
                p = snap["portfolio"]
                print(f"[结果] 首页已可显示: 风险={p['risk']['score']} "
                      f"Quality={p['quality']['score']} "
                      f"信号={p['entry']['grade']}/{p['decision']['action']}")
            for w in (snap.get("warnings") or [])[:4]:
                print(f"[提示] {w}")
            app.quit()
            return
        if time.time() - t0 > 300:
            print("[失败] 超过 300 秒仍未完成加载")
            app.quit()
            return

    timer = QTimer()
    timer.timeout.connect(poll)
    timer.start(120)
    QTimer.singleShot(150, lambda: win.reload())
    app.exec()
    return 0 if state["done"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
