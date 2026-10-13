"""ETF 明细页（第二阶段）离屏测试：验证买入质量/胜率/期望/盈亏比/是否适合买 + 边际分析。

运行：QT_QPA_PLATFORM=offscreen python tests/etf_page_test.py
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))


def main() -> int:
    from PySide6.QtWidgets import QApplication

    from core.config import get_config
    from core.pipeline import run_full
    from ui.etf_page import EtfPage

    app = QApplication(sys.argv)
    cfg = get_config(refresh=True)
    snap = run_full(cfg, progress=None, use_cache_first=True, with_macro=False,
                    save=False)
    page = EtfPage(cfg)
    page.set_snapshot(snap)
    n = len(page.rows)
    print(f"ETF 行数: {n}")
    bad = []
    for i in range(n):
        page.list.setCurrentRow(i)
        r = page.rows[i]
        name = r["asset"].get("name")
        decide = page.lbl_decide.text()
        marg = page.lbl_marginal_head.text()
        tiles = page.grid_decide.count()
        rows = page.tbl_marginal.rowCount()
        print(f"[{i}] {name}")
        print(f"    {decide}")
        print(f"    边际分析: {marg[:68]}")
        print(f"    tiles={tiles}  marginal_rows={rows}")
        if not decide.startswith("【当前是否适合买】") or tiles != 6:
            bad.append(name)
    print("ETF_PAGE_OK" if not bad else f"ETF_PAGE_FAIL: {bad}")
    page.deleteLater()
    app.quit()
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
