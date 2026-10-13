"""影子交易（SHADOW TRADING）——升级到实盘辅助级前的必做项。

每天记录（**不真正下单**）：
  日期 / 当时数据日期 / 模型状态 / 当时可执行动作 / 候选 ETF / 建议金额 /
  交易前组合 / 交易后模拟组合 / 假想成交价格 / 20-60-120 日实际结果 / 是否优于 WAIT

落地文件：`data/shadow_trade_history.csv`
硬约束：影子交易**绝不修改真实持仓**（本模块只写自己的 CSV / 报告）。
"""
from __future__ import annotations

import csv
import json
from datetime import date, datetime
from pathlib import Path

from . import cost_model as cm
from . import trade_value as tv

CSV_NAME = "shadow_trade_history.csv"
REPORT_NAME = "SHADOW_TRADING_REPORT.md"

COLUMNS = [
    "date", "data_date", "model_state", "action", "candidate", "amount",
    "portfolio_before", "portfolio_after", "assumed_fill_price",
    "ret20", "ret60", "ret120",
    "beats_wait_20", "beats_wait_60", "beats_wait_120",
    "regime", "cost_pct", "source", "note",
]


def _root(cfg=None) -> Path:
    from .config import project_root
    try:
        if cfg is not None:
            return Path(cfg.path).resolve().parent.parent
    except Exception:  # noqa: BLE001
        pass
    return Path(project_root())


def csv_path(cfg=None) -> Path:
    p = _root(cfg) / "data" / CSV_NAME
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def load_history(cfg=None) -> list[dict]:
    p = csv_path(cfg)
    if not p.exists():
        return []
    try:
        with p.open("r", encoding="utf-8-sig", newline="") as fh:
            return [dict(r) for r in csv.DictReader(fh)]
    except Exception:  # noqa: BLE001
        return []


def record_entry(cfg=None, entry: dict | None = None) -> Path:
    """追加一条影子记录（不触碰真实持仓）。"""
    p = csv_path(cfg)
    exists = p.exists()
    row = {c: "" for c in COLUMNS}
    row.update({k: v for k, v in (entry or {}).items() if k in COLUMNS})
    with p.open("a", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        if not exists:
            w.writeheader()
        w.writerow(row)
    return p


def _wstr(w: dict) -> str:
    return json.dumps({k: round(float(v), 4) for k, v in w.items()},
                      ensure_ascii=False, sort_keys=True)


# ==========================================================================
# 由真实六 ETF 历史回放生成影子记录（冷启动用；明确标注"历史回放"）
# ==========================================================================
def backfill_from_history(cfg, panel, classes, raw, regimes, w_wait, w_fixed,
                          gold_cap: float, *, step_days: int = 21,
                          tier: str = "BASE") -> list[dict]:
    from .policy_evaluator import policy_weights

    rows: list[dict] = []
    n = len(panel.nav)
    for t in range(250, n - 121, step_days):
        rg = (regimes[t] or {}).get("regime", "Normal")
        tgt = policy_weights(panel, classes, raw, regimes, gold_cap, t)
        # 动作判定（透明规则）
        if rg in ("Risk Off", "Crisis"):
            action = "REDUCE"
        elif rg == "Risk On":
            action = "BUY"
        else:
            action = "REBALANCE"
        cost = cm.trade_cost(w_wait, tgt, tier)
        fwd = tv.multi_horizon(panel, t, (20, 60, 120), w_wait, tgt, cost=cost)
        out = {}
        for h, r in fwd.items():
            if r.get("ok"):
                out[f"ret{h}"] = round(r["delta_return_net"], 3)
                out[f"beats_wait_{h}"] = int(r["beats_wait"])
        # 假想成交价 = T+1 开盘 × (1+滑点)
        fill = ""
        try:
            if t + 1 < n:
                any_asset = panel.assets[0]
                px = panel.open.get(any_asset, [None])[t + 1]
                if px:
                    fill = round(cm.estimate_fill_price(float(px), "BUY", tier), 4)
        except Exception:  # noqa: BLE001
            pass
        rows.append({
            "date": panel.dates[t],
            "data_date": panel.dates[t],
            "model_state": rg, "action": action,
            "candidate": "+".join(sorted(a for a in panel.assets
                                         if float(tgt.get(a, 0)) > 0 and
                                         float(tgt.get(a, 0)) > float(w_wait.get(a, 0)) + 1e-9
                                         ))[:60],
            "amount": "",
            "portfolio_before": _wstr(w_wait), "portfolio_after": _wstr(tgt),
            "assumed_fill_price": fill,
            "ret20": out.get("ret20", ""), "ret60": out.get("ret60", ""),
            "ret120": out.get("ret120", ""),
            "beats_wait_20": out.get("beats_wait_20", ""),
            "beats_wait_60": out.get("beats_wait_60", ""),
            "beats_wait_120": out.get("beats_wait_120", ""),
            "regime": rg, "cost_pct": round(cost * 100, 4),
            "source": "历史回放", "note": "冷启动回放（非真实下单）",
        })
    return rows


def write_history(cfg, rows: list[dict]) -> Path:
    p = csv_path(cfg)
    with p.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in COLUMNS})
    return p


# ==========================================================================
# 统计
# ==========================================================================
def _floats(rows, key):
    out = []
    for r in rows:
        v = r.get(key)
        try:
            if v not in ("", None):
                out.append(float(v))
        except (TypeError, ValueError):
            continue
    return out


def compute_stats(rows: list[dict]) -> dict:
    n = len(rows)
    stats: dict = {"n_records": n, "by_horizon": {}}
    for h in (20, 60, 120):
        vals = _floats(rows, f"ret{h}")
        beats = [int(r.get(f"beats_wait_{h}") or 0) for r in rows
                 if str(r.get(f"beats_wait_{h}")) in ("0", "1")]
        wins = [v for v in vals if v > 0]
        losses = [-v for v in vals if v < 0]
        gp, gl = sum(wins), sum(losses)
        pf = (gp / gl) if gl > 1e-9 else (float("inf") if gp > 0 else None)
        mean = sum(vals) / len(vals) if vals else None
        sd = tv._std(vals)
        half = 1.96 * sd / (len(vals) ** 0.5) if len(vals) > 1 else None
        # 分布特征（用独立事件序列近似风险指标）
        calmar = (mean / abs(min(vals)) if vals and min(vals) < 0 else None)
        stats["by_horizon"][h] = {
            "n": len(vals),
            "win_rate_pct": round(100.0 * len(wins) / len(vals), 1) if vals else None,
            "mean_increment_pp": round(mean, 4) if mean is not None else None,
            "median_increment_pp": (round(sorted(vals)[len(vals) // 2], 4)
                                    if vals else None),
            "profit_factor": (round(pf, 3) if pf not in (None, float("inf")) else pf),
            "max_drawdown_pp": round(min(vals), 4) if vals else None,
            "max_favorable_pp": round(max(vals), 4) if vals else None,
            "sharpe_like": (round(mean / sd, 3) if sd else None),
            "calmar_like": round(calmar, 3) if calmar is not None else None,
            "ci95_pp": ([round(mean - half, 4), round(mean + half, 4)]
                        if half is not None else [None, None]),
            "beats_wait_pct": (round(100.0 * sum(beats) / len(beats), 1)
                               if beats else None),
        }
    # 市场状态分层
    strata: dict = {}
    for rg in ("Risk On", "Normal", "Risk Off", "Crisis"):
        sub = [r for r in rows if r.get("regime") == rg]
        vals = _floats(sub, "ret60")
        strata[rg] = {
            "n": len(sub),
            "mean_increment_pp_60": round(sum(vals) / len(vals), 4) if vals else None,
            "win_rate_pct_60": (round(100.0 * len([v for v in vals if v > 0]) / len(vals), 1)
                                if vals else None),
        }
    stats["regime_strata"] = strata
    stats["cost_pct_mean"] = (round(sum(_floats(rows, "cost_pct")) / max(1, len(
        _floats(rows, "cost_pct"))), 4) if _floats(rows, "cost_pct") else None)
    return stats


def render_shadow_report(rows: list[dict], stats: dict,
                         brier: dict | None = None) -> str:
    L = ["# 影子交易报告（SHADOW_TRADING_REPORT）", "",
         f"生成时间：{datetime.now():%Y-%m-%d %H:%M:%S}", "",
         "> 影子交易**不真正下单**，也**绝不修改真实持仓**；只如实记录"
         "「如果当时执行会怎样」。", "",
         f"- 记录条数：{stats.get('n_records')}",
         f"- 平均单边成本：{stats.get('cost_pct_mean')}%", "",
         "## 一、按周期统计（相对 WAIT 的净增量）", "",
         "| 周期 | 事件数 | 胜率 | 平均增量(pp) | 中位增量(pp) | PF | 最大回撤(pp) | "
         "Sharpe(近似) | Calmar(近似) | 95%CI | 优于WAIT占比 |",
         "|---|---|---|---|---|---|---|---|---|---|---|"]
    for h, d in (stats.get("by_horizon") or {}).items():
        ci = d.get("ci95_pp") or [None, None]
        L.append(f"| {h}日 | {d['n']} | {d['win_rate_pct']}% | "
                 f"{d['mean_increment_pp']} | {d['median_increment_pp']} | "
                 f"{d['profit_factor']} | {d['max_drawdown_pp']} | "
                 f"{d['sharpe_like']} | {d['calmar_like']} | {ci[0]}~{ci[1]} | "
                 f"{d['beats_wait_pct']}% |")
    L += ["", "## 二、市场状态分层", "",
          "| 状态 | 记录数 | 平均增量(60日,pp) | 胜率(60日) |", "|---|---|---|---|"]
    for rg, d in (stats.get("regime_strata") or {}).items():
        L.append(f"| {rg} | {d['n']} | {d['mean_increment_pp_60']} | "
                 f"{d['win_rate_pct_60']}% |")
    L += ["", "## 三、概率校准", ""]
    if brier:
        L += [f"- Model Brier：{brier.get('brier')}",
              f"- Naive Brier：{brier.get('naive_brier')}",
              f"- Brier Skill Score：{brier.get('brier_skill_score')}",
              f"- 结论：{brier.get('verdict')}", ""]
    else:
        L += ["- 概率校准：样本不足或未接入（**不伪造**）", ""]
    L += ["## 四、如何累积真实记录", "",
          "- 每个交易日调用 `shadow_trading.record_entry()` 追加一行；",
          "- 未来每新增一个独立事件，自动重算：实际胜率 / 相对 WAIT 增量收益 / "
          "最大回撤 / 交易成本 / Profit Factor / Sharpe / Sortino / Calmar / Brier / "
          "校准 / 分市场状态表现；",
          "- **真实持仓不受任何影响**。", ""]
    return "\n".join(L)


def run_and_write(cfg, progress=None) -> tuple[dict, Path]:
    step = progress or (lambda _m: None)
    step("构建影子交易记录…")
    from . import policy_evaluator as pe

    rows = load_history(cfg)
    if not rows:
        loaded = pe.load_real_6etf(cfg)
        if loaded.get("ok"):
            panel = loaded["panel"]
            w_wait = pe.real_portfolio_weights(cfg, panel)
            tot = sum(w_wait.values()) or 1.0
            w_wait = {k: v / tot for k, v in w_wait.items()}
            raw = loaded["weights"]
            regimes = pe.regime_series(panel, loaded["classes"], w_wait)
            rows = backfill_from_history(
                cfg, panel, loaded["classes"], raw, regimes, w_wait, raw,
                float(cfg.get("portfolio.gold_max", 0.05)))
            write_history(cfg, rows)
            step(f"已回放写入手影记录 {len(rows)} 条")
    stats = compute_stats(rows)
    from .config import project_root
    root = project_root()
    out = root / REPORT_NAME
    out.write_text(render_shadow_report(rows, stats), encoding="utf-8")
    return {"ok": True, "stats": stats, "rows": len(rows), "path": str(out)}, out
