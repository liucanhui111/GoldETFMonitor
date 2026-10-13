"""黄金 ETF 页：把旧版『159937 黄金ETF量化风险监控』的核心视图集成进主系统。

数据全部取自本项目**同一次快照**（snap），不新增数据源、不重复下载、不伪造。
旧版概念 → 新版真实数据的对应关系：

    旧版「当前状态」        → portfolio.gold_control（黄金仓位控制 + 建议）
    旧版「为什么」          → gold_control.advice + 底部雷达 + 宏观评分 + 均线位置 + 风控标记
    旧版「综合风险评分」    → portfolio.risk（组合口径 0-100，越高风险越大）+ 分项
    旧版「核心数据」        → 159937 snapshot（价格/持仓/成本/市值/盈亏）+ 黄金仓位
    旧版「黄金底部雷达」    → macro.gold_radar（8 条件，真实数据，缺失即标 UNKNOWN）
    旧版「技术面与关键价位」→ 159937 均线 / MACD / RSI / ATR / 高低点 + 关键价位表
    旧版「8 指标监控明细」  → macro.gold_macro_score.components（8 指标加权，含权重重归一）

刻意不含走势图（本轮约定：只做卡片 + 表格）。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QGridLayout, QHBoxLayout, QLabel, QScrollArea,
                               QVBoxLayout, QWidget)

from .theme import palette
from .widgets import Card, DataTable, Tile, hline, set_badge

# 指标键 → 层级（与旧版「趋势层 / 宏观层 / 情绪层 / 广度层」一致）
LAYER_OF = {
    "trend": "趋势层", "etf159937": "趋势层",
    "usd": "宏观层", "real": "宏观层", "nominal": "宏观层", "fed": "宏观层",
    "gvz": "情绪层", "bpgdm": "广度层",
}


def _f(v, nd: int = 2, suffix: str = "", sign: bool = False) -> str:
    if v is None:
        return "暂无数据"
    try:
        s = f"{float(v):+.{nd}f}" if sign else f"{float(v):.{nd}f}"
    except (TypeError, ValueError):
        return "暂无数据"
    return f"{s}{suffix}"


def _money(v) -> str:
    if v is None:
        return "暂无数据"
    try:
        return f"{float(v):,.0f} 元"
    except (TypeError, ValueError):
        return "暂无数据"


class GoldPage(QWidget):
    """159937 黄金专项监控页（卡片 + 表格，无图表）。"""

    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.snap: dict | None = None
        self._row: dict | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        outer.addWidget(self.scroll)
        container = QWidget()
        self.scroll.setWidget(container)
        self.root = QVBoxLayout(container)
        self.root.setContentsMargins(0, 0, 4, 4)
        self.root.setSpacing(10)

        self.lbl_head = QLabel("黄金专项监控（159937）　暂无数据")
        self.lbl_head.setObjectName("SectionTitle")
        self.lbl_head.setWordWrap(True)
        self.root.addWidget(self.lbl_head)

        self.root.addWidget(self._row_status())
        self.root.addWidget(self._row_core())
        self.root.addWidget(self._row_radar())
        self.root.addWidget(self._row_ind8())
        self.root.addStretch(1)

    # ---------------------------------------------------------------- 行 1
    def _row_status(self) -> QWidget:
        wrap = QWidget()
        h = QHBoxLayout(wrap)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(10)

        left = Card("当前状态")
        left.setMinimumWidth(520)
        self.badge = QLabel("暂无数据")
        self.badge.setWordWrap(True)
        left.add(self.badge)
        self.lbl_badge_sub = QLabel("")
        self.lbl_badge_sub.setObjectName("Muted")
        self.lbl_badge_sub.setWordWrap(True)
        left.add(self.lbl_badge_sub)
        left.add(hline())

        lbl_why = QLabel("为什么")
        lbl_why.setObjectName("CardTitle")
        left.add(lbl_why)
        self.lbl_reasons = QLabel("暂无数据")
        self.lbl_reasons.setWordWrap(True)
        self.lbl_reasons.setTextFormat(Qt.RichText)
        left.add(self.lbl_reasons)
        left.add(hline())

        lbl_watch = QLabel("目前最重要的观察")
        lbl_watch.setObjectName("CardTitle")
        left.add(lbl_watch)
        self.lbl_watch = QLabel("暂无数据")
        self.lbl_watch.setWordWrap(True)
        left.add(self.lbl_watch)

        self.lbl_disclaimer = QLabel("")
        self.lbl_disclaimer.setWordWrap(True)
        self.lbl_disclaimer.setVisible(False)
        left.add(self.lbl_disclaimer)
        h.addWidget(left, 3)

        right = Card("综合风险评分（0-100，分数越高＝下行风险越大）")
        right.setMinimumWidth(400)
        top = QHBoxLayout()
        self.lbl_risk = QLabel("—")
        self.lbl_risk.setObjectName("BigValue")
        self.lbl_risk_state = QLabel("")
        top.addWidget(self.lbl_risk)
        top.addSpacing(8)
        top.addWidget(self.lbl_risk_state)
        top.addStretch(1)
        top_w = QWidget()
        top_w.setLayout(top)
        right.add(top_w)
        right.add(hline())

        lbl_parts = QLabel("风险分项（组合口径）")
        lbl_parts.setObjectName("CardTitle")
        right.add(lbl_parts)
        self.grid_risk = QGridLayout()
        self.grid_risk.setHorizontalSpacing(14)
        self.grid_risk.setVerticalSpacing(4)
        gw = QWidget()
        gw.setLayout(self.grid_risk)
        right.add(gw)
        right.add(hline())

        self.lbl_macro = QLabel("黄金宏观评分：暂无数据")
        self.lbl_macro.setWordWrap(True)
        right.add(self.lbl_macro)
        h.addWidget(right, 2)
        return wrap

    # ---------------------------------------------------------------- 行 2
    def _row_core(self) -> QWidget:
        card = Card("核心数据")
        grid = QGridLayout()
        grid.setSpacing(9)
        specs = ["当前价格", "持仓数量", "持仓成本", "当前市值",
                 "浮动盈亏", "收益率", "黄金仓位", "行情时间"]
        self.tiles: dict[str, Tile] = {}
        for i, name in enumerate(specs):
            t = Tile(name, "暂无数据")
            self.tiles[name] = t
            grid.addWidget(t, i // 4, i % 4)
        cw = QWidget()
        cw.setLayout(grid)
        card.add(cw)
        self.lbl_cost_note = QLabel("")
        self.lbl_cost_note.setObjectName("Muted")
        self.lbl_cost_note.setWordWrap(True)
        card.add(self.lbl_cost_note)
        return card

    # ---------------------------------------------------------------- 行 3
    def _row_radar(self) -> QWidget:
        wrap = QWidget()
        h = QHBoxLayout(wrap)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(10)

        left = Card("黄金底部雷达（8 个条件）")
        left.setMinimumWidth(560)
        head = QHBoxLayout()
        self.lbl_radar_count = QLabel("底部信号：0/8")
        self.lbl_radar_count.setObjectName("MidValue")
        self.lbl_radar_state = QLabel("—")
        self.lbl_radar_state.setStyleSheet("font-size:13px; font-weight:700;")
        head.addWidget(self.lbl_radar_count)
        head.addSpacing(10)
        head.addWidget(self.lbl_radar_state)
        head.addStretch(1)
        hw = QWidget()
        hw.setLayout(head)
        left.add(hw)
        self.tbl_radar = DataTable()
        self.tbl_radar.setMinimumHeight(300)
        left.add(self.tbl_radar)
        self.lbl_radar_note = QLabel(
            "以上为量化观察信号，不代表未来必然上涨，不构成投资建议；"
            "即使雷达满格，黄金仓位也绝不能超过 5%。")
        self.lbl_radar_note.setWordWrap(True)
        self.lbl_radar_note.setObjectName("Muted")
        left.add(self.lbl_radar_note)
        h.addWidget(left, 3)

        right = Card("159937 技术面与关键价位")
        right.setMinimumWidth(430)
        self.lbl_tech = QLabel("暂无数据")
        self.lbl_tech.setWordWrap(True)
        self.lbl_tech.setTextFormat(Qt.RichText)
        right.add(self.lbl_tech)
        self.tbl_key = DataTable()
        self.tbl_key.setMinimumHeight(260)
        right.add(self.tbl_key)
        h.addWidget(right, 2)
        return wrap

    # ---------------------------------------------------------------- 行 4
    def _row_ind8(self) -> QWidget:
        card = Card("8 指标监控明细（趋势 + 宏观 + 情绪 + 市场广度）")
        self.tbl_ind8 = DataTable()
        self.tbl_ind8.setMinimumHeight(280)
        card.add(self.tbl_ind8)
        self.lbl_ind8_note = QLabel(
            "评分 0-100，50=中性；权重为系统既定权重，某项数据缺失时该项不参与加权"
            "（权重重归一），绝不伪造。")
        self.lbl_ind8_note.setObjectName("Muted")
        self.lbl_ind8_note.setWordWrap(True)
        card.add(self.lbl_ind8_note)
        return card

    # ---------------------------------------------------------------- 数据
    def set_snapshot(self, snap: dict) -> None:
        self.snap = snap
        self._fill()

    def set_loading(self, on: bool) -> None:
        if self.snap:
            return                      # 已有数据时不覆盖（刷新期间保留上一轮结果）
        if on:
            self.lbl_head.setText("黄金专项监控（159937）　正在获取数据…")
        else:
            self.lbl_head.setText("黄金专项监控（159937）　暂无数据")

    def refresh_theme(self) -> None:
        if self.snap:
            self._fill()

    @staticmethod
    def _gold_row(snap: dict) -> dict | None:
        for r in (snap.get("rows") or []):
            if (r.get("asset") or {}).get("asset_class") == "gold" and r.get("snapshot"):
                return r
        return None

    # ---------------------------------------------------------------- 渲染
    def _fill(self) -> None:
        snap = self.snap or {}
        if not snap.get("ok"):
            self.lbl_head.setText("黄金专项监控（159937）　暂无数据（本轮未成功加载）")
            self._fill_status({}, {}, {})
            self._fill_core(None, {})
            self._fill_radar({})
            self._fill_tech(None)
            self._fill_ind8({})
            return

        p = snap.get("portfolio") or {}
        macro = snap.get("macro") or {}
        row = self._gold_row(snap)
        self._row = row
        s = (row or {}).get("snapshot") or {}

        self.lbl_head.setText(
            f"{s.get('name') or '黄金ETF'}（{s.get('thscode') or '159937'}）　"
            f"数据日：{s.get('data_time') or '—'}　K线：{s.get('bars') or '—'}　"
            "类别：黄金")

        self._fill_status(p.get("gold_control") or {}, p.get("risk") or {},
                          macro.get("gold_macro_score") or {}, row, macro)
        self._fill_core(row, p.get("weights") or {})
        self._fill_radar(macro.get("gold_radar") or {})
        self._fill_tech(row)
        self._fill_ind8(macro.get("gold_macro_score") or {})

    # ---- 行 1
    def _fill_status(self, gc: dict, risk: dict, ms: dict,
                     row: dict | None = None, macro: dict | None = None) -> None:
        row = row or {}
        macro = macro or {}
        s = row.get("snapshot") or {}
        sc = row.get("scores") or {}
        flags = sc.get("flags") or {}

        # ---- 状态徽标 ----
        cur = gc.get("current_pct")
        cap = gc.get("cap_pct")
        over = bool(gc.get("over_cap"))
        radar_level = gc.get("radar_level") or 0
        if over:
            state, color = "🔴 已超配 · 暂停加仓", "danger"
        elif cur is not None and cap is not None and cur >= cap - 0.005:
            state, color = "🟠 已达上限 · 只持有", "warn"
        elif radar_level >= 2:
            state, color = "🟡 底部信号 · 受 5% 上限约束", "warn"
        elif cur is not None:
            state, color = "🟢 正常参与（对冲/危机保护）", "ok"
        else:
            state, color = "⚪ 数据不足", "muted"
        set_badge(self.badge, state, color, big=True)

        self.lbl_badge_sub.setText(
            f"黄金仓位 {_f(cur, 2, '%')} ｜ 战略上限 {_f(cap, 2, '%')} ｜ "
            f"底部雷达 {gc.get('radar') or '—'} ｜ 宏观评分 "
            f"{_f(gc.get('macro_score'), 1)}({gc.get('macro_grade') or '—'})")

        # ---- 为什么 ----
        reasons: list[str] = []
        if gc.get("advice"):
            reasons.append(gc["advice"])
        radar = macro.get("gold_radar") or {}
        if radar.get("total"):
            extra = (f"，其中 {len(radar.get('missing') or [])} 项数据缺失"
                     if radar.get("missing") else "")
            reasons.append(f"黄金底部雷达 {radar.get('hit')}/{radar.get('total')} —— "
                           f"{radar.get('status') or '—'}{extra}")
        if ms.get("available"):
            reasons.append(f"黄金宏观评分 {_f(ms.get('score'), 1)}/100（"
                           f"{ms.get('grade_label') or '—'}）；最强项 "
                           f"{(ms.get('drivers') or ['—'])[0]}，最弱项 "
                           f"{(ms.get('drags') or ['—'])[0]}")
        price = s.get("price")
        ma = s.get("ma") or {}
        if price is not None and ma.get("ma20"):
            rel = "站上" if price >= ma["ma20"] else "跌破"
            reasons.append(f"现价 {_f(price, 3)} {rel} MA20（{_f(ma.get('ma20'), 3)}）"
                           + (f"，{'站上' if price >= ma.get('ma60', 0) else '跌破'} "
                              f"MA60（{_f(ma.get('ma60'), 3)}）" if ma.get("ma60") else ""))
        if flags.get("no_chase"):
            reasons.append("风控：高位强趋势，禁止追涨")
        if flags.get("not_stopped_falling"):
            reasons.append("风控：尚未止跌（创新低 / MA20 向下 / 底部未确认），禁止抄底")
        if flags.get("deep_oversold"):
            reasons.append("风控：深度超跌（1年/2年低位 + 大跌 + 超卖）")
        if risk.get("score") is not None:
            reasons.append(f"组合风险 {_f(risk.get('score'), 1)} → 市场状态 "
                           f"{risk.get('state') or '—'}")
        if not reasons:
            reasons = ["黄金数据未加载或不足，暂无法给出判断（请在工具栏勾选『加载宏观/黄金数据』）"]
        self.lbl_reasons.setText(
            "<br>".join(f"{i + 1}. {t}" for i, t in enumerate(reasons)))

        # ---- 观察 ----
        if over:
            watch = (f"黄金已超过 {_f(cap, 2, '%')} 战略上限，需先减配回到上限以内，"
                     "才谈得上重新加仓。")
        elif cur is not None and cap is not None and cur >= cap - 0.005:
            watch = (f"黄金已达 {_f(cap, 2, '%')} 上限，只持有、不再增加；"
                     "等待其他资产出现更好的边际机会。")
        else:
            parts = []
            if ma.get("ma20") is not None:
                parts.append(f"能否站上 MA20（{_f(ma.get('ma20'), 3)}）")
            if radar.get("total"):
                parts.append(f"底部雷达能否进入『底部观察区』（当前 {radar.get('hit')}/{radar.get('total')}）")
            watch = "；".join(parts) + "。" if parts else \
                "等待宏观/黄金数据加载后再判断。"
        self.lbl_watch.setText(watch)

        disc = ("以上为量化观察信号，不构成投资建议。黄金永远 ≤5%，"
                "不连接证券账户、不自动下单。")
        self.lbl_disclaimer.setText(disc)
        self.lbl_disclaimer.setVisible(True)

        # ---- 风险分项 ----
        while self.grid_risk.count():
            it = self.grid_risk.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        score = risk.get("score")
        pal = palette()
        self.lbl_risk.setText("—" if score is None else f"{score:.1f}")
        c = pal["up"] if (score or 0) >= 50 else (pal["down"] if score is not None else pal["muted"])
        if score is not None and 30 <= score < 50:
            c = pal["warn"]
        self.lbl_risk.setStyleSheet(f"color:{c};")
        self.lbl_risk_state.setText(risk.get("state") or "—")
        comps = risk.get("components") or []
        if not comps:
            lab = QLabel("暂无数据")
            lab.setObjectName("Muted")
            self.grid_risk.addWidget(lab, 0, 0)
        for i, cp in enumerate(comps):
            name = QLabel(f"{cp.get('name')}（权重 {_f((cp.get('weight') or 0) * 100, 0, '%')}）")
            name.setObjectName("Muted")
            val = QLabel(_f(cp.get("value"), 1))
            val.setObjectName("MidValue")
            self.grid_risk.addWidget(name, (i // 2) * 2, i % 2)
            self.grid_risk.addWidget(val, (i // 2) * 2 + 1, i % 2)

        if ms.get("available"):
            self.lbl_macro.setText(
                f"黄金宏观评分：{_f(ms.get('score'), 1)}/100　"
                f"{ms.get('grade_label') or '—'}（50=中性；只描述环境，不构成仓位建议）")
        else:
            self.lbl_macro.setText(
                "黄金宏观评分：暂无数据（需在工具栏勾选『加载宏观/黄金数据』）")

    # ---- 行 2
    def _fill_core(self, row: dict | None, weights: dict) -> None:
        s = (row or {}).get("snapshot") or {}
        by_class = weights.get("by_class") or {}
        gold_pct = (by_class.get("gold") or 0) * 100 if by_class else None
        pnl = s.get("pnl")
        pnl_pct = s.get("pnl_pct")
        up, down = "up", "down"

        self.tiles["当前价格"].set(_f(s.get("price"), 4),
                                   f"日涨跌 {_f(s.get('change_pct'), 2, '%', True)}",
                                   up if (s.get("change_pct") or 0) >= 0 else down)
        self.tiles["持仓数量"].set(f"{s.get('shares', 0):,.0f} 份" if row else "暂无数据",
                                   "")
        self.tiles["持仓成本"].set(_f(s.get("cost"), 4), "元/份")
        self.tiles["当前市值"].set(_money(s.get("market_value")), "")
        self.tiles["浮动盈亏"].set(_money(pnl), "红=盈利 / 绿=亏损",
                                   up if (pnl or 0) >= 0 else down)
        self.tiles["收益率"].set(_f(pnl_pct, 2, "%", True), "",
                                 up if (pnl_pct or 0) >= 0 else down)
        self.tiles["黄金仓位"].set(_f(gold_pct, 2, "%"),
                                   f"上限 {_f(float(self.cfg.get('portfolio.gold_max', 0.05)) * 100, 1, '%')}")
        self.tiles["行情时间"].set(s.get("data_time") or "—",
                                   "最近交易日收盘" if s.get("data_time") else "")

        if row:
            src = ((row.get("raw") or {}).get("source") or "—")
            self.lbl_cost_note.setText(
                f"成本基准：{_f(s.get('cost'), 4)} 元/份 × {s.get('shares', 0):,.0f} 份 = "
                f"{_money((s.get('cost') or 0) * (s.get('shares') or 0))} ｜ "
                f"数据源：{src} ｜ 价格时间：{s.get('data_time') or '—'}")
        else:
            self.lbl_cost_note.setText("持仓与行情暂无数据（无法换算份数与金额，不伪造）。")

    # ---- 行 3 左
    def _fill_radar(self, radar: dict) -> None:
        conds = radar.get("conditions") or []
        if not conds:
            self.lbl_radar_count.setText("底部信号：暂无数据")
            self.lbl_radar_state.setText("—")
            self.tbl_radar.set_rows(["#", "条件", "是否满足", "说明"], [])
            self.lbl_radar_note.setText(
                "宏观/黄金数据未加载（请在工具栏勾选『加载宏观/黄金数据』），"
                "本轮无法判定底部雷达。")
            return
        self.lbl_radar_count.setText(
            f"底部信号：{radar.get('hit')}/{radar.get('total')}")
        self.lbl_radar_state.setText(radar.get("status") or "—")
        color_key = {0: "muted", 1: "muted", 2: "warn", 3: "danger"}.get(
            radar.get("level") or 0, "muted")
        self.lbl_radar_state.setStyleSheet(
            f"font-size:13px; font-weight:700; color:{palette().get(color_key)};")

        rows = []
        for cd in conds:
            ok = cd.get("ok")
            mark = "✔ 满足" if ok is True else ("✘ 未满足" if ok is False else "？ 数据缺失")
            rows.append([cd.get("no"), cd.get("name"), mark, cd.get("detail") or ""])
        self.tbl_radar.set_rows(["#", "条件", "是否满足", "说明"], rows,
                                stretch_cols=[1, 3])
        note = radar.get("note") or ""
        self.lbl_radar_note.setText(
            "以上为量化观察信号，不代表未来必然上涨，不构成投资建议。"
            + (("　" + note) if note else ""))

    # ---- 行 3 右
    def _fill_tech(self, row: dict | None) -> None:
        s = (row or {}).get("snapshot") or {}
        if not row:
            self.lbl_tech.setText("159937 技术数据暂不可用（本轮未取到该 ETF 行情）")
            self.tbl_key.set_rows(["价位", "含义", "距现价", "实际均线"], [])
            return
        ma = s.get("ma") or {}
        macd = s.get("macd") or {}
        hl = s.get("high_low") or {}
        price = s.get("price")

        def _hl(key):
            d = hl.get(key) or {}
            return d.get("high"), d.get("low")

        h60, l60 = _hl("60d") if hl.get("60d") else (None, None)
        h52, l52 = _hl("52w") if hl.get("52w") else (None, None)

        above = [w for w in (5, 10, 20, 30, 50, 60, 120, 200)
                 if ma.get(f"ma{w}") and price and price >= ma[f"ma{w}"]]
        below = [w for w in (5, 10, 20, 30, 50, 60, 120, 200)
                 if ma.get(f"ma{w}") and price and price < ma[f"ma{w}"]]
        ma_txt = " ｜ ".join(f"<b>MA{k}</b> {_f(v, 3)}"
                             for k, v in (("5", ma.get("ma5")), ("10", ma.get("ma10")),
                                          ("20", ma.get("ma20")), ("30", ma.get("ma30")),
                                          ("60", ma.get("ma60")), ("120", ma.get("ma120")),
                                          ("200", ma.get("ma200"))) if v is not None)
        parts = [f"均线：{ma_txt or '暂无数据'}"]
        parts.append(f"RSI14 <b>{_f(s.get('rsi14'), 1)}</b> ｜ "
                     f"MACD DIF {_f(macd.get('dif'), 4)} / DEA {_f(macd.get('dea'), 4)} ｜ "
                     f"ATR14 {_f(s.get('atr14'), 4)}（{_f(s.get('atr_pct'), 2)}%）")
        parts.append(f"60日高/低 <b>{_f(h60, 3)}</b> / <b>{_f(l60, 3)}</b> ｜ "
                     f"52周高/低 {_f(h52, 3)} / {_f(l52, 3)}")
        parts.append(f"20日涨跌 {_f(s.get('ret20'), 2, '%', True)} ｜ "
                     f"1年相对价位 {_f((s.get('relpos') or {}).get('1y'), 1)} ｜ "
                     f"1年当前回撤 {_f(((s.get('dd') or {}).get('1y') or {}).get('current_drawdown_pct'), 2, '%')}")
        parts.append("站上的均线：<b>%s</b> ｜ 未站上的均线：<b>%s</b>"
                     % ("、".join(f"MA{w}" for w in above) or "无",
                        "、".join(f"MA{w}" for w in below) or "无"))
        self.lbl_tech.setText("<br>".join(parts))

        cost = s.get("cost")
        rows: list[list] = []
        for w, role in ((10, "短期支撑/压力"), (20, "短中期支撑/压力"),
                        (30, "中期支撑/压力"), (60, "中期趋势线"),
                        (120, "长期趋势线"), (200, "超长期趋势线")):
            v = ma.get(f"ma{w}")
            if v is None:
                continue
            d = (price / v - 1) * 100 if price else None
            rows.append([_f(v, 3), f"MA{w} {role}",
                         _f(d, 2, "%", True), f"MA{w} = {_f(v, 3)}"])
        if l60 is not None:
            d = (price / l60 - 1) * 100 if price else None
            rows.append([_f(l60, 3), "60日低点", _f(d, 2, "%", True), f"60日低点 {_f(l60, 3)}"])
        if h60 is not None:
            d = (price / h60 - 1) * 100 if price else None
            rows.append([_f(h60, 3), "60日高点", _f(d, 2, "%", True), f"60日高点 {_f(h60, 3)}"])
        if cost:
            d = (price / cost - 1) * 100 if price else None
            rows.append([_f(cost, 3), "用户持仓成本", _f(d, 2, "%", True), "用户持仓成本"])
        self.tbl_key.set_rows(["价位", "含义", "距现价", "实际均线"], rows,
                              color_cols={2}, stretch_cols=[1, 3])

    # ---- 行 4
    def _fill_ind8(self, ms: dict) -> None:
        comps = ms.get("components") or []
        if not comps:
            self.tbl_ind8.set_rows(
                ["层", "指标", "评分", "权重", "贡献", "说明"],
                [["—", "暂无数据（未加载宏观/黄金数据）", "", "", "", ""]])
            return
        usable = [c for c in comps if c.get("available") and c.get("score") is not None]
        wsum = sum(float(c.get("weight") or 0) for c in usable) or 1.0
        rows = []
        for c in comps:
            w = float(c.get("weight") or 0)
            sc = c.get("score")
            contrib = (sc * w / wsum) if (sc is not None and c.get("available")) else None
            rows.append([
                LAYER_OF.get(c.get("key"), "—"),
                c.get("name"),
                _f(sc, 1) if c.get("available") else "暂无数据",
                _f(w * 100, 0, "%"),
                _f(contrib, 2) if contrib is not None else "—",
                c.get("reason") or "",
            ])
        self.tbl_ind8.set_rows(
            ["层", "指标", "评分", "权重", "贡献", "说明"], rows,
            color_cols={2, 4}, stretch_cols=[1, 5])
        weights_txt = "、".join(f"{c.get('name')}={_f((c.get('weight') or 0) * 100, 0)}%"
                              for c in comps if c.get("available"))
        self.lbl_ind8_note.setText(
            "评分 0-100，50=中性；某项数据缺失时该项不参与加权（权重重归一）。"
            f"本轮参与加权：{weights_txt or '无'}。"
            "评分只描述环境，不构成仓位建议（黄金永远 ≤5%）。")
