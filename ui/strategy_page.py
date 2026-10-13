"""策略有效性页（验收 §49/§50/§52）。

回答一个问题：模型到底有没有用？
- 四概率（基础 / 条件 / 样本外 / 相对基准提升）
- 系统可信等级（不是买入等级，硬门槛自动判定）
- 完整 STRATEGY_VALIDATION_REPORT.md（逐日滚动 OOS、校准、基准比较）
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QPushButton, QScrollArea,
                               QVBoxLayout, QWidget)

from .theme import palette
from .widgets import Card, Tile


def _root(cfg) -> Path:
    try:
        return Path(cfg.path).parent.parent
    except Exception:  # noqa: BLE001
        return Path.cwd()


def _fmt(v, nd=1, suffix="%"):
    return "暂无数据" if v is None else f"{float(v):.{nd}f}{suffix}"


class StrategyPage(QWidget):
    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self._worker = None

        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(10)

        head = QHBoxLayout()
        head.setSpacing(10)
        self.t_level = Tile("系统可信等级", "未评估",
                            "判定依据：数据完整性/样本外/校准/相对基础率")
        self.t_dec = Tile("决策主口径·独立事件OOS（60日）", "—",
                          "唯一决策概率：独立事件去重样本外")
        self.t_a = Tile("基础历史概率（不择时）", "—", "什么都不看的历史胜率（基准）")
        self.t_b = Tile("相似状态历史频率", "—", "研究参考，不参与决策")
        self.t_c = Tile("重叠滚动OOS频率", "—", "研究参考，不参与决策（窗口高度重叠）")
        for t in (self.t_level, self.t_dec, self.t_a, self.t_b, self.t_c):
            head.addWidget(t, 1)
        root.addLayout(head)

        self.t_up = Tile("相对基础率（决策口径 − 基础）", "—",
                         "只报方向与数量，不设达标线；有效性看样本外证据")
        self.t_cal = Tile("概率校准（vs 基础率基线）", "—",
                          "必须与 Naive Brier 基线比较（BSS>0 才算优于基线）")
        head2 = QHBoxLayout()
        head2.setSpacing(10)
        head2.addWidget(self.t_up, 1)
        head2.addWidget(self.t_cal, 1)
        root.addLayout(head2)

        self.lbl_verdict = QLabel("")
        self.lbl_verdict.setWordWrap(True)
        self.lbl_verdict.setStyleSheet(
            f"color: {palette().get('warn', '#c90')}; font-weight: 600;")
        root.addWidget(self.lbl_verdict)

        btns = QHBoxLayout()
        self.btn_run = QPushButton("运行策略验证（较慢，逐日滚动样本外）")
        self.btn_run.setObjectName("Primary")
        self.btn_run.clicked.connect(self._run_validation)
        btns.addWidget(self.btn_run)
        self.lbl_run = QLabel("")
        self.lbl_run.setObjectName("Muted")
        btns.addWidget(self.lbl_run, 1)
        root.addLayout(btns)

        card = Card("策略验证报告（STRATEGY_VALIDATION_REPORT.md）")
        self.txt = QLabel("")
        self.txt.setWordWrap(True)
        self.txt.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.txt.setStyleSheet("font-family: Consolas, monospace; font-size: 12px;")
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.txt)
        scroll.setFrameShape(QScrollArea.NoFrame)
        card.add(scroll)
        root.addWidget(card, 1)

        self.reload_report()

    # ------------------------------------------------------------------
    def set_snapshot(self, snap: dict) -> None:
        """从完整快照填四概率（廉价部分，不需要重跑验证）。"""
        p = (snap or {}).get("portfolio") or {}
        dec = p.get("decision") or {}
        base = (p.get("marginal") or {}).get("baseline") or {}
        prob = dec.get("prob") or {}
        # 决策主口径（独立事件去重样本外）
        dp = dec.get("dec_prob") or base.get("dec_prob") or {}
        dec_prob = dp.get("prob_used") or prob.get("prob_used")
        a = dp.get("base_rate") or prob.get("prob60_base") or base.get("prob60_base")
        b = dp.get("conditional_prob") or prob.get("p60") or base.get("prob60")
        c = dp.get("oos_overlap_prob") or prob.get("oos60") or base.get("prob60_oos")
        up = dp.get("lift_pp") if dp.get("lift_pp") is not None else base.get("prob60_uplift")
        if dec_prob is not None:
            self.t_dec.set(_fmt(dec_prob), 
                           f"独立事件 {dp.get('independent_events') or prob.get('independent_events') or '—'} 个"
                           "（唯一决策口径）", "accent")
        else:
            self.t_dec.set("暂无数据", "独立事件去重样本外不可用")
        self.t_a.set(_fmt(a), "样本 " + str(prob.get("samples60")
                                          or base.get("samples") or "—"))
        self.t_b.set(_fmt(b), "研究参考，不参与决策")
        self.t_c.set(_fmt(c), "研究参考，不参与决策")
        self.t_up.set(_fmt(up, 1, "pp") if up is not None else "暂无数据",
                      "≈0 或为负时：无证据证明模型有择时优势" if up is not None
                      and abs(float(up)) < 2 else
                      ("有正提升，仍需持续跟踪" if up is not None else ""))
        if up is not None and float(up) <= 0:
            self.t_up.set(_fmt(up, 1, "pp"),
                          "【当前模型没有证明额外择时优势】", "warn")
            self.lbl_verdict.setText(
                "⚠ 高胜率主要来自资产本身长期上涨特征，目前没有证明择时信号显著提高胜率。"
                "决策概率请按\"独立事件去重样本外\"理解，不要把研究参考数字当成择时能力。")
        elif up is not None:
            self.lbl_verdict.setText(
                f"决策口径相对基础率 {float(up):+.1f}pp；"
                "仍需持续跟踪其稳定性，任何概率都不构成收益保证。")

    # ------------------------------------------------------------------
    def reload_report(self) -> None:
        f = _root(self.cfg) / "STRATEGY_VALIDATION_REPORT.md"
        if f.exists():
            self.txt.setText(f.read_text(encoding="utf-8", errors="replace"))
            self._fill_level_from_report()
        else:
            self.txt.setText("尚未生成。点击上方按钮运行策略验证，"
                             "或执行：GoldETFMonitor-cli.exe --validate-strategy")
            self.t_level.set("未评估", "尚未运行策略验证")
            self.t_cal.set("—", "")

    def _fill_level_from_report(self) -> None:
        txt = self.txt.text()
        import re
        m = re.search(r"系统可信等级[：:]\s*\*\*?([^*\n]+)", txt)
        if m:
            level = m.group(1).strip()
            color = ("up" if "实盘" in level else
                     "warn" if ("研究" in level or "观察" in level) else "accent")
            self.t_level.set(level, "硬门槛自动判定（不是买入等级）", color)
        # 校准判定：优先 Brier Skill Score（与 Naive 基线比较），
        # 不再用 Brier<0.25 的旧口径。
        mbss = re.search(r"Brier Skill Score[^0-9\-+]{0,16}(-?[0-9.]+)", txt)
        if mbss:
            bss = float(mbss.group(1))
            self.t_cal.set(f"BSS {bss:+.4f}",
                           "优于基础率基线（BSS>0）" if bss > 0
                           else "未优于基础率基线——请勿依赖该概率",
                           "up" if bss > 0 else "warn")
        else:
            mb = re.search(r"Model Brier[^0-9]{0,24}([0-9.]+)", txt)
            if mb:
                self.t_cal.set(f"{float(mb.group(1)):.4f}",
                               "BSS 缺失，无法判定是否优于基础率基线", "muted")

    # ------------------------------------------------------------------
    def _run_validation(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            return
        from .worker import StrategyValidateWorker

        self.btn_run.setEnabled(False)
        self.lbl_run.setText("运行中…（逐日滚动样本外预测，可能需要数分钟）")
        self._worker = StrategyValidateWorker(self.cfg)
        self._worker.progress.connect(self.lbl_run.setText)
        self._worker.done.connect(self._on_done)
        self._worker.failed.connect(self._on_failed)
        self._worker.start()

    def _on_done(self, _res) -> None:
        self.btn_run.setEnabled(True)
        self.lbl_run.setText("完成")
        self.reload_report()

    def _on_failed(self, msg: str) -> None:
        self.btn_run.setEnabled(True)
        self.lbl_run.setText("失败：" + msg.splitlines()[-1][:120])

    def refresh_theme(self) -> None:  # noqa: D102 — 与其他页保持一致
        self.reload_report()
