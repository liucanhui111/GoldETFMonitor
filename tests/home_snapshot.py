"""首页（组合总览）文本内容快照：用于人工核对第二阶段需求是否落实。"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))


def main() -> int:
    from PySide6.QtWidgets import QApplication, QLabel

    from core.config import get_config
    from core.pipeline import run_full
    from ui.portfolio_page import PortfolioPage

    app = QApplication(sys.argv)
    cfg = get_config(refresh=True)
    snap = run_full(cfg, progress=None, use_cache_first=True, with_macro=False,
                    save=False)
    page = PortfolioPage(cfg)
    page.set_snapshot(snap)
    print("=" * 60)
    print("专业模式首页可见文本（按布局顺序）")
    print("=" * 60)
    for lbl in page.findChildren(QLabel):
        t = lbl.text().strip()
        if not t:
            continue
        if lbl.objectName() in ("SectionTitle", "CardTitle") or len(t) > 8:
            print(t)
    print("\n" + "=" * 60)
    print("新手模式首页文本")
    print("=" * 60)
    page.set_simple(True)
    for lbl in page.findChildren(QLabel):
        t = lbl.text().strip()
        if t:
            print(t)
    print("SNAPSHOT_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
