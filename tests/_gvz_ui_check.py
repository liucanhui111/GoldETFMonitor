"""临时探针：带宏观快照，验证 GVZ/BPGDM 新口径在界面表格里正确渲染。

运行：QT_QPA_PLATFORM=offscreen python tests/_gvz_ui_check.py
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))


def _table_text(tbl) -> str:
    out = []
    for r in range(tbl.rowCount()):
        for c in range(tbl.columnCount()):
            it = tbl.item(r, c)
            if it is not None:
                out.append(it.text())
    return " | ".join(out)


def main() -> int:
    from PySide6.QtWidgets import QApplication

    from core.config import get_config
    from core.pipeline import run_full
    from ui.etf_page import EtfPage
    from ui.portfolio_page import PortfolioPage

    app = QApplication(sys.argv)
    cfg = get_config(refresh=True)
    snap = run_full(cfg, progress=None, use_cache_first=True, with_macro=True,
                    save=False)
    macro = snap.get("macro") or {}
    if not macro:
        print("SKIP: 宏观数据不可用（网络受限）")
        return 0

    # ---- ETF 页（黄金资产）----
    page = EtfPage(cfg)
    page.set_snapshot(snap)
    gold_idx = None
    for i, r in enumerate(page.rows):
        if r["asset"].get("asset_class") == "gold":
            gold_idx = i
            break
    if gold_idx is None:
        print("FAIL: 未找到黄金资产")
        return 1
    page.list.setCurrentRow(gold_idx)
    txt = _table_text(page.tbl_gold)
    need = ["BPGDM 真实值", "BPGDM 代理值【非真实BPGDM】", "BPGDM 研究区间",
            "黄金波动率指数 GVZ", "系统研究分档"]
    miss = [n for n in need if n not in txt]
    print("ETF页黄金表命中:", {n: (n in txt) for n in need})
    print("  GVZ 行:", [s for s in txt.split(" | ") if "GVZ" in s][:4])
    print("  BPGDM 行:", [s for s in txt.split(" | ") if "BPGDM" in s][:8])
    bad = bool(miss)

    # ---- 组合页（宏观表）----
    pp = PortfolioPage(cfg)
    pp.set_snapshot(snap)
    ptxt = _table_text(pp.tbl_macro)
    need2 = ["BPGDM 真实值（StockCharts/StockQ）", "BPGDM 代理值【非真实BPGDM】",
             "BPGDM 研究区间 / P&F", "系统研究分档"]
    miss2 = [n for n in need2 if n not in ptxt]
    print("组合页宏观表命中:", {n: (n in ptxt) for n in need2})
    print("  BPGDM 行:", [s for s in ptxt.split(" | ") if "BPGDM" in s][:8])
    bad = bad or bool(miss2)
    if miss or miss2:
        print(f"FAIL: 缺失 {miss + miss2}")
        return 1
    print("GVZ_UI_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
