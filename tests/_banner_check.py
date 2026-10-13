import os, sys, datetime
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
from PySide6.QtWidgets import QApplication
from ui.theme import apply_theme
from ui.portfolio_page import PortfolioPage
from core.config import get_config

app = QApplication(sys.argv)
apply_theme(app)
cfg = get_config(refresh=False)
pg = PortfolioPage(cfg)

stale = {"ok": True, "ts": datetime.datetime.now(), "elapsed_sec": 1,
         "rows": [], "macro": {},
         "freshness": {"ok": False, "action": "DATA_STALE",
                       "quote_basis": "最近交易日收盘",
                       "rows": [{"key": "etf_close", "label": "A股ETF收盘",
                                 "data_date": "2026-09-30", "age_days": 7,
                                 "allowed_days": 3, "status": "STALE"}]}}
pg.snap = stale
pg._render_stale_banner()
print("STALE -> hidden:", pg.v_stale.isHidden())
print("STALE -> text:", pg.v_stale.text())

fresh = {"ok": True, "freshness": {"ok": True, "action": "PASS",
         "rows": [{"key": "etf_close", "label": "A股ETF收盘",
                   "data_date": "2026-10-06", "age_days": 1,
                   "allowed_days": 3, "status": "OK"}]}}
pg.snap = fresh
pg._render_stale_banner()
print("PASS  -> hidden:", pg.v_stale.isHidden())
