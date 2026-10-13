"""通用界面控件：卡片、指标块、折线图、表格、相关性热力图。

图表用 QPainter 自绘，不依赖 QtCharts，打包后体积小且不会有插件缺失问题。
"""
from __future__ import annotations

import math

from PySide6.QtCore import Qt, QRectF, QPointF
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QBrush, QPolygonF
from PySide6.QtWidgets import (QFrame, QGridLayout, QHBoxLayout, QHeaderView, QLabel,
                               QSizePolicy, QTableWidget, QTableWidgetItem,
                               QVBoxLayout, QWidget)

from .theme import palette


def color_of(key: str) -> QColor:
    return QColor(palette().get(key, "#888888"))


class Card(QFrame):
    """带标题的卡片容器。"""

    def __init__(self, title: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("Card")
        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(14, 12, 14, 12)
        self._lay.setSpacing(9)
        self.title: QLabel | None = None
        if title:
            self.title = QLabel(title)
            self.title.setObjectName("CardTitle")
            self._lay.addWidget(self.title)
        self.body = QWidget()
        self.body_lay = QVBoxLayout(self.body)
        self.body_lay.setContentsMargins(0, 0, 0, 0)
        self.body_lay.setSpacing(8)
        self._lay.addWidget(self.body)

    def add(self, w: QWidget):
        self.body_lay.addWidget(w)
        return w

    def add_layout(self, lay):
        self.body_lay.addLayout(lay)
        return lay


class Tile(QFrame):
    """单个指标块：标题 + 大数值 + 副说明。"""

    def __init__(self, label: str, value: str = "暂无数据", sub: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("Card")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(2)
        self.lbl = QLabel(label)
        self.lbl.setObjectName("Muted")
        self.val = QLabel(value)
        self.val.setObjectName("MidValue")
        self.sub = QLabel(sub)
        self.sub.setObjectName("Muted")
        self.sub.setWordWrap(True)
        lay.addWidget(self.lbl)
        lay.addWidget(self.val)
        lay.addWidget(self.sub)

    def set(self, value: str, sub: str = "", color: str | None = None):
        self.val.setText(value)
        self.sub.setText(sub)
        if color:
            self.val.setStyleSheet(f"color: {palette().get(color, color)};")
        else:
            self.val.setStyleSheet("")


def kpi_grid(items: list[tuple[str, str, str, str | None]], cols: int = 4) -> QWidget:
    """items: (label, value, sub, color_key)"""
    w = QWidget()
    g = QGridLayout(w)
    g.setContentsMargins(0, 0, 0, 0)
    g.setSpacing(9)
    for i, (label, value, sub, col) in enumerate(items):
        t = Tile(label, value, sub)
        if col:
            t.set(value, sub, col)
        g.addWidget(t, i // cols, i % cols)
    return w


def badge(text: str, color_key: str) -> QLabel:
    """状态徽标。注意：Qt 的 8 位十六进制是 #AARRGGBB（alpha 在前），
    所以透明度必须写成 rgba()，否则 {c}22 会被当成近不透明的深色。"""
    lbl = QLabel(text)
    c = palette().get(color_key, "#888888")
    q = QColor(c)
    lbl.setStyleSheet(
        f"background: rgba({q.red()},{q.green()},{q.blue()},0.13); color:{c};"
        f"border:1px solid rgba({q.red()},{q.green()},{q.blue()},0.45);"
        "border-radius:9px; padding:2px 10px; font-weight:600;")
    lbl.setAlignment(Qt.AlignCenter)
    return lbl


def set_badge(lbl: QLabel, text: str, color_key: str, *, big: bool = False) -> None:
    """更新已有徽标的文字与配色（供需要重复刷新的页面使用）。"""
    c = palette().get(color_key, "#888888")
    q = QColor(c)
    size = "font-size:15px; padding:7px 14px; font-weight:700;" if big \
        else "font-size:12px; padding:2px 10px; font-weight:600;"
    lbl.setText(text)
    lbl.setStyleSheet(
        f"background: rgba({q.red()},{q.green()},{q.blue()},0.13); color:{c};"
        f"border:1px solid rgba({q.red()},{q.green()},{q.blue()},0.45);"
        f"border-radius:9px; {size}")
    lbl.setAlignment(Qt.AlignCenter)


def colored_item(text: str, value=None, *, pct: bool = False, bold: bool = False,
                 nd: int = 2) -> QTableWidgetItem:
    """按中国习惯配色：正=红，负=绿。"""
    it = QTableWidgetItem(text)
    it.setTextAlignment(Qt.AlignCenter)
    if value is not None:
        try:
            v = float(value)
            c = palette()["up"] if v > 0 else (palette()["down"] if v < 0 else palette()["text"])
            it.setForeground(QColor(c))
        except (TypeError, ValueError):
            pass
    if bold:
        f = QFont()
        f.setBold(True)
        it.setFont(f)
    return it


class DataTable(QTableWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setEditTriggers(QTableWidget.NoEditTriggers)
        self.setAlternatingRowColors(False)
        self.setSelectionBehavior(QTableWidget.SelectRows)
        self.verticalHeader().setVisible(False)
        self.setShowGrid(True)
        self.horizontalHeader().setStretchLastSection(True)

    def set_rows(self, headers: list[str], rows: list[list], *,
                 color_cols: set[int] | None = None, bold_cols: set[int] | None = None,
                 stretch_cols: list[int] | None = None, row_height: int = 26):
        self.clear()
        self.setColumnCount(len(headers))
        self.setHorizontalHeaderLabels(headers)
        self.setRowCount(len(rows))
        color_cols = color_cols or set()
        bold_cols = bold_cols or set()
        for r, row in enumerate(rows):
            for c, cell in enumerate(row):
                if isinstance(cell, QTableWidgetItem):
                    self.setItem(r, c, cell)
                    continue
                text = "" if cell is None else str(cell)
                if c in color_cols:
                    try:
                        v = float(str(cell).replace("%", "").replace("+", ""))
                    except (TypeError, ValueError):
                        v = None
                    self.setItem(r, c, colored_item(text, v, bold=c in bold_cols))
                else:
                    it = QTableWidgetItem(text)
                    it.setTextAlignment(Qt.AlignCenter if c else Qt.AlignLeft | Qt.AlignVCenter)
                    if c in bold_cols:
                        f = QFont()
                        f.setBold(True)
                        it.setFont(f)
                    self.setItem(r, c, it)
        for r in range(self.rowCount()):
            self.setRowHeight(r, row_height)
        hh = self.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.Interactive)
        if stretch_cols:
            for c in stretch_cols:
                hh.setSectionResizeMode(c, QHeaderView.Stretch)
        self.resizeColumnsToContents()


class LineChart(QWidget):
    """多序列折线图（自绘）。"""

    def __init__(self, title: str = "", height: int = 220, parent=None):
        super().__init__(parent)
        self.title = title
        self._series: list[dict] = []
        self._x: list[str] = []
        self._zero = False
        self._y_fmt = ".2f"
        self._hint = "暂无数据"
        self.setMinimumHeight(height)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def set_series(self, series: list[dict], x: list[str] | None = None, *,
                   zero_line: bool = False, y_fmt: str = ".2f",
                   hint: str = "暂无数据"):
        self._series = [s for s in (series or []) if s.get("values")]
        self._x = list(x or [])
        self._zero = zero_line
        self._y_fmt = y_fmt
        self._hint = hint
        self.update()

    def clear(self):
        self._series = []
        self._x = []
        self.update()

    def paintEvent(self, _ev):  # noqa: N802
        p = palette()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        w, h = self.width(), self.height()
        painter.fillRect(0, 0, w, h, QColor(p["card"]))

        top = 8
        if self.title:
            painter.setPen(QColor(p["text"]))
            f = QFont()
            f.setBold(True)
            f.setPointSize(9)
            painter.setFont(f)
            painter.drawText(10, 18, self.title)
            top = 26

        vals = [v for s in self._series for v in s["values"] if v is not None]
        if not vals:
            painter.setPen(QColor(p["muted"]))
            painter.drawText(self.rect(), Qt.AlignCenter, self._hint)
            painter.end()
            return

        lo, hi = min(vals), max(vals)
        if self._zero:
            lo, hi = min(lo, 0.0), max(hi, 0.0)
        if hi - lo < 1e-12:
            hi, lo = hi + 1, lo - 1
        pad = (hi - lo) * 0.08
        lo, hi = lo - pad, hi + pad

        left, right, bottom = 58, 12, 22
        plot = QRectF(left, top + 6, max(10, w - left - right), max(10, h - top - bottom - 6))

        # 网格 + Y 轴刻度
        painter.setPen(QPen(QColor(p["grid"]), 1))
        labels = 4
        for i in range(labels + 1):
            y = plot.top() + plot.height() * i / labels
            painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            v = hi - (hi - lo) * i / labels
            painter.setPen(QColor(p["muted"]))
            painter.drawText(QRectF(0, y - 8, left - 6, 16),
                             Qt.AlignRight | Qt.AlignVCenter, f"{v:{self._y_fmt}}")
            painter.setPen(QPen(QColor(p["grid"]), 1))

        if self._zero and lo < 0 < hi:
            y0 = plot.bottom() - (0 - lo) / (hi - lo) * plot.height()
            painter.setPen(QPen(QColor(p["muted"]), 1, Qt.DashLine))
            painter.drawLine(QPointF(plot.left(), y0), QPointF(plot.right(), y0))

        # 序列
        series_colors = p["series"]
        for si, s in enumerate(self._series):
            color = QColor(s.get("color") or series_colors[si % len(series_colors)])
            pen = QPen(color, float(s.get("width", 1.6)))
            if s.get("dashed"):
                pen.setStyle(Qt.DashLine)
            painter.setPen(pen)
            vals_s = s["values"]
            n = len(vals_s)
            if n < 2:
                continue
            pts: list[QPointF] = []
            # 断点：None 处断开，避免连线造成"假数据"
            segs: list[list[QPointF]] = []
            cur: list[QPointF] = []
            for i, v in enumerate(vals_s):
                if v is None:
                    if len(cur) > 1:
                        segs.append(cur)
                    cur = []
                    continue
                x = plot.left() + plot.width() * (i / (n - 1))
                y = plot.bottom() - (float(v) - lo) / (hi - lo) * plot.height()
                cur.append(QPointF(x, y))
                pts.append(cur[-1])
            if len(cur) > 1:
                segs.append(cur)

            if s.get("fill") and segs:
                area = QPolygonF(segs[0] + [QPointF(segs[-1][-1].x(), plot.bottom()),
                                           QPointF(segs[0][0].x(), plot.bottom())])
                c = QColor(color)
                c.setAlpha(38)
                painter.setBrush(QBrush(c))
                painter.setPen(Qt.NoPen)
                painter.drawPolygon(area)
                painter.setPen(pen)
            for seg in segs:
                painter.drawPolyline(QPolygonF(seg))
            if s.get("dots") and pts:
                painter.setBrush(QBrush(color))
                for pt in pts[-40:]:
                    painter.drawEllipse(pt, 1.6, 1.6)

        # 图例
        painter.setPen(QColor(p["muted"]))
        lx = plot.left() + 2
        ly = plot.top() - 8
        for si, s in enumerate(self._series):
            color = QColor(s.get("color") or series_colors[si % len(series_colors)])
            name = str(s.get("name", ""))
            last = next((v for v in reversed(s["values"]) if v is not None), None)
            txt = f"{name} {last:{self._y_fmt}}" if last is not None else name
            fm = painter.fontMetrics()
            tw = fm.horizontalAdvance(txt) + 18
            if lx + tw > plot.right():
                break
            painter.setPen(QPen(color, 2))
            painter.drawLine(QPointF(lx, ly + 5), QPointF(lx + 12, ly + 5))
            painter.setPen(QColor(p["muted"]))
            painter.drawText(QPointF(lx + 16, ly + 9), txt)
            lx += tw + 10

        # X 轴日期
        if self._x:
            painter.setPen(QColor(p["muted"]))
            for frac, align in ((0.0, Qt.AlignLeft), (0.5, Qt.AlignHCenter), (1.0, Qt.AlignRight)):
                idx = int((len(self._x) - 1) * frac)
                painter.drawText(
                    QRectF(plot.left() + plot.width() * frac - 45, plot.bottom() + 3, 90, 16),
                    align, str(self._x[idx])[:10])
        painter.end()


class BarChart(QWidget):
    """水平/垂直柱状图（用于风险贡献、年度收益等）。"""

    def __init__(self, title: str = "", height: int = 200, parent=None):
        super().__init__(parent)
        self.title = title
        self._items: list[tuple[str, float, float | None]] = []
        self._fmt = ".1f"
        self._suffix = "%"
        self._zero = True
        self.setMinimumHeight(height)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def set_items(self, items: list[tuple[str, float, float | None]], *,
                  y_fmt: str = ".1f", suffix: str = "%", zero_line: bool = True):
        self._items = items
        self._fmt, self._suffix, self._zero = y_fmt, suffix, zero_line
        self.update()

    def paintEvent(self, _ev):  # noqa: N802
        p = palette()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        w, h = self.width(), self.height()
        painter.fillRect(0, 0, w, h, QColor(p["card"]))
        top = 8
        if self.title:
            f = QFont()
            f.setBold(True)
            f.setPointSize(9)
            painter.setFont(f)
            painter.setPen(QColor(p["text"]))
            painter.drawText(10, 18, self.title)
            top = 26
        if not self._items:
            painter.setPen(QColor(p["muted"]))
            painter.drawText(self.rect(), Qt.AlignCenter, "暂无数据")
            painter.end()
            return

        vals = [v for _, v, _ in self._items if v is not None]
        vals += [v for _, _, v in self._items if v is not None]
        lo, hi = min(vals + [0.0]), max(vals + [0.0])
        if hi - lo < 1e-9:
            hi, lo = 1, -1
        left, right, bottom = 104, 54, 8
        plot_h = h - top - bottom
        n = len(self._items)
        row_h = plot_h / n
        bar_h = min(16.0, row_h * 0.6)
        x0 = left
        scale = (w - left - right) / (hi - lo)
        zx = x0 + (0 - lo) * scale

        painter.setPen(QPen(QColor(p["grid"]), 1))
        painter.drawLine(QPointF(zx, top), QPointF(zx, h - bottom))

        for i, (name, v, ref) in enumerate(self._items):
            cy = top + row_h * i + row_h / 2
            painter.setPen(QColor(p["text"]))
            painter.drawText(QRectF(0, cy - 9, left - 8, 18),
                             Qt.AlignRight | Qt.AlignVCenter, str(name)[:12])
            if v is not None:
                c = color_of("up") if v >= 0 else color_of("down")
                x = x0 + (min(0.0, v) - lo) * scale
                bw = abs(v) * scale
                painter.fillRect(QRectF(x, cy - bar_h / 2, max(1.0, bw), bar_h), c)
                painter.setPen(QColor(p["text"]))
                painter.drawText(QRectF(zx + (bw if v >= 0 else -bw) + (4 if v >= 0 else -54),
                                        cy - 9, 50, 18),
                                 Qt.AlignLeft if v >= 0 else Qt.AlignRight,
                                 f"{v:{self._fmt}}{self._suffix}")
            if ref is not None:
                rx = x0 + (ref - lo) * scale
                painter.setPen(QPen(color_of("muted"), 1, Qt.DashLine))
                painter.drawLine(QPointF(rx, cy - bar_h), QPointF(rx, cy + bar_h))
        painter.end()


class HeatmapTable(DataTable):
    """相关性矩阵热力图（用表格底色表现强度）。"""

    def set_matrix(self, labels: list[str], matrix: list[list[float | None]]):
        self.clear()
        self.setColumnCount(len(labels) + 1)
        self.setHorizontalHeaderLabels([""] + [l[:8] for l in labels])
        self.setRowCount(len(labels))
        self.setVerticalHeaderLabels([l[:10] for l in labels])
        for i in range(len(labels)):
            it = QTableWidgetItem(str(labels[i])[:10])
            it.setTextAlignment(Qt.AlignCenter)
            self.setItem(i, 0, it)
            for j in range(len(labels)):
                v = matrix[i][j] if i < len(matrix) and j < len(matrix[i]) else None
                if v is None:
                    cell = QTableWidgetItem("暂无数据")
                else:
                    cell = QTableWidgetItem(f"{v:.2f}")
                    cell.setBackground(QColor(_heat(v)))
                    if abs(v) > 0.55:
                        cell.setForeground(QColor("#ffffff"))
                cell.setTextAlignment(Qt.AlignCenter)
                self.setItem(i, j + 1, cell)
        self.resizeColumnsToContents()
        self.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        for r in range(self.rowCount()):
            self.setRowHeight(r, 24)


def _heat(v: float) -> str:
    """相关系数 → 背景色（绿=低/分散，红=高/集中）。"""
    v = max(-1.0, min(1.0, float(v)))
    if v >= 0:
        r = int(255 - 90 * v)
        g = int(255 - 150 * v)
        b = int(255 - 160 * v)
    else:
        r = int(255 + 60 * v)
        g = int(255 + 20 * v)
        b = int(255 - 40 * v)
    return f"#{max(0,min(255,r)):02x}{max(0,min(255,g)):02x}{max(0,min(255,b)):02x}"


def hline() -> QFrame:
    f = QFrame()
    f.setFrameShape(QFrame.HLine)
    f.setStyleSheet(f"color: {palette()['border']};")
    return f


def hbox(*widgets, spacing: int = 9) -> QWidget:
    w = QWidget()
    lay = QHBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(spacing)
    for x in widgets:
        if x is None:
            continue
        if isinstance(x, QWidget):
            lay.addWidget(x)
        else:
            lay.addLayout(x)
    return w
