"""界面主题与配色。

遵循中国股票市场惯例：涨=红，跌=绿。
浅色主题为主，自动适配系统深色（可被设置页强制切换）。
"""
from __future__ import annotations

LIGHT = {
    "bg": "#f2f4f7",
    "card": "#ffffff",
    "card_alt": "#fafbfc",
    "text": "#1f2328",
    "muted": "#6b7280",
    "border": "#e1e5ea",
    "accent": "#2563eb",
    "accent_soft": "#e8f0fe",
    "up": "#d92c20",      # 涨 / 正收益（红）
    "down": "#12985a",    # 跌 / 负收益（绿）
    "warn": "#b45309",
    "danger": "#c0261b",
    "ok": "#0f766e",
    "grid": "#eef0f3",
    "series": ["#2563eb", "#d92c20", "#0f766e", "#b45309", "#7c3aed",
               "#0891b2", "#c0261b", "#4d7c0f"],
}

DARK = {
    "bg": "#14171c",
    "card": "#1c2027",
    "card_alt": "#21262e",
    "text": "#e8eaed",
    "muted": "#9aa4b2",
    "border": "#2c333d",
    "accent": "#5b9cf8",
    "accent_soft": "#1e2a3d",
    "up": "#ff6b5e",
    "down": "#3ecf8e",
    "warn": "#f0b357",
    "danger": "#ff6b5e",
    "ok": "#3ecf8e",
    "grid": "#262c35",
    "series": ["#5b9cf8", "#ff6b5e", "#3ecf8e", "#f0b357", "#b18cff",
               "#4bc7e0", "#ff8a80", "#a3d977"],
}

_current = dict(LIGHT)


def palette() -> dict:
    return _current


def set_theme(dark: bool) -> dict:
    _current.clear()
    _current.update(DARK if dark else LIGHT)
    return _current


def is_dark() -> bool:
    return _current is DARK or _current.get("bg") == DARK["bg"]


def apply_theme(app) -> None:
    from PySide6.QtGui import QFont

    p = palette()
    app.setStyleSheet(f"""
    QWidget {{
        background: {p['bg']};
        color: {p['text']};
        font-family: "Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", sans-serif;
        font-size: 12px;
    }}
    /* 标签必须透明，否则会把卡片/结论卡的底色盖成页面背景色 */
    QLabel {{ background: transparent; }}
    QFrame#Card {{
        background: {p['card']};
        border: 1px solid {p['border']};
        border-radius: 10px;
    }}
    QLabel#CardTitle {{ font-size: 13px; font-weight: 600; }}
    QLabel#SectionTitle {{ font-size: 15px; font-weight: 600; }}
    QLabel#Muted {{ color: {p['muted']}; }}
    QLabel#BigValue {{ font-size: 26px; font-weight: 700; }}
    QLabel#MidValue {{ font-size: 17px; font-weight: 600; }}
    QLabel#HugeValue {{ font-size: 34px; font-weight: 800; }}
    QLabel#SubValue {{ font-size: 19px; font-weight: 700; }}
    QFrame#VerdictCard {{
        background: {p['accent_soft']};
        border: 2px solid {p['accent']};
        border-radius: 12px;
    }}
    QPushButton {{
        background: {p['card']};
        border: 1px solid {p['border']};
        border-radius: 7px;
        padding: 6px 14px;
    }}
    QPushButton:hover {{ border-color: {p['accent']}; color: {p['accent']}; }}
    QPushButton:disabled {{ color: {p['muted']}; }}
    QPushButton#Primary {{
        background: {p['accent']}; color: #ffffff; border: none;
    }}
    QPushButton#Primary:hover {{ background: {p['accent']}; color: #ffffff; opacity: .9; }}
    QPushButton#Nav {{
        text-align: left; padding: 9px 14px; border: none; border-radius: 8px;
        background: transparent;
    }}
    QPushButton#Nav:checked {{
        background: {p['accent_soft']}; color: {p['accent']}; font-weight: 600;
    }}
    QTabWidget::pane {{ border: 1px solid {p['border']}; border-radius: 8px; }}
    QTabBar::tab {{
        padding: 7px 14px; margin-right: 2px; border-radius: 6px;
        background: {p['card_alt']}; color: {p['muted']};
    }}
    QTabBar::tab:selected {{ background: {p['accent_soft']}; color: {p['accent']}; }}
    QTableWidget, QTableView {{
        background: {p['card']}; border: 1px solid {p['border']};
        gridline-color: {p['grid']}; border-radius: 8px;
        selection-background-color: {p['accent_soft']};
        selection-color: {p['text']};
    }}
    QHeaderView::section {{
        background: {p['card_alt']}; border: none;
        border-bottom: 1px solid {p['border']}; padding: 6px 8px; font-weight: 600;
    }}
    QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit, QTextEdit {{
        background: {p['card']}; border: 1px solid {p['border']};
        border-radius: 6px; padding: 5px 8px;
    }}
    QScrollArea {{ border: none; }}
    QStatusBar {{ background: {p['card']}; border-top: 1px solid {p['border']}; }}
    QToolBar {{ background: {p['card']}; border-bottom: 1px solid {p['border']}; spacing: 6px; }}
    QProgressBar {{
        border: none; background: {p['card_alt']}; border-radius: 4px;
        text-align: center; color: {p['muted']}; height: 6px;
    }}
    QProgressBar::chunk {{ background: {p['accent']}; border-radius: 4px; }}
    """)
    app.setFont(QFont("Microsoft YaHei UI", 9))
