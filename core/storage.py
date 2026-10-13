"""本地历史数据落盘（按组合账户隔离）。

规则（对应需求"同一天不能重复无限追加"）：
    · 每个文件以 (date, key) 为唯一键，写入时先删除同键旧行再追加；
    · 写文件前先写 .tmp 再原子替换，避免中断导致半截文件；
    · 数据缺失一律写空字符串，绝不写 0 或随机值；
    · **按 portfolio_id 分目录**（data/<portfolio_id>/），不同组合的历史数据
      物理隔离，绝不互相覆盖或混读。
"""
from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

from .config import DATA_DIR

# 当前活动组合（由 pipeline 在每次运行前设置）
_PORTFOLIO_ID = "all_weather_01"


def set_portfolio(portfolio_id: str) -> None:
    global _PORTFOLIO_ID
    _PORTFOLIO_ID = str(portfolio_id or "all_weather_01").strip() or "all_weather_01"


def current_portfolio() -> str:
    return _PORTFOLIO_ID


def portfolio_dir(portfolio_id: str | None = None) -> Path:
    d = DATA_DIR / (portfolio_id or _PORTFOLIO_ID)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _path(name: str) -> Path:
    return portfolio_dir() / name


def _write_rows(name: str, rows: list[dict], keys: tuple[str, ...]) -> int:
    """按 keys 去重后写回（保留顺序）。"""
    if not rows:
        return 0
    p = _path(name)
    existing: list[dict] = []
    if p.exists():
        try:
            with p.open("r", encoding="utf-8-sig", newline="") as f:
                existing = list(csv.DictReader(f))
        except Exception:
            existing = []
    new_keys = {tuple(str(r.get(k, "")) for k in keys) for r in rows}
    kept = [r for r in existing
            if tuple(str(r.get(k, "")) for k in keys) not in new_keys]
    merged = kept + rows
    fields: list[str] = []
    for r in merged:
        for k in r:
            if k not in fields:
                fields.append(k)
    tmp = p.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in merged:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in fields})
    tmp.replace(p)
    return len(rows)


def save_etf_history(rows: list[dict]) -> int:
    """每只 ETF 当日的一条收盘快照。"""
    return _write_rows("etf_history.csv", rows, ("date", "id"))


def save_macro_history(rows: list[dict]) -> int:
    return _write_rows("macro_history.csv", rows, ("date", "item"))


def save_indicator_history(rows: list[dict]) -> int:
    return _write_rows("indicator_history.csv", rows, ("date", "id"))


def save_portfolio_history(rows: list[dict]) -> int:
    return _write_rows("portfolio_history.csv", rows, ("date", "portfolio_id"))


def save_signal_history(rows: list[dict]) -> int:
    return _write_rows("signal_history.csv", rows, ("date", "portfolio_id"))


def save_rebalance_history(rows: list[dict]) -> int:
    return _write_rows("rebalance_history.csv", rows, ("date", "id"))


def save_portfolio_simulation(rows: list[dict]) -> int:
    """【组合模拟历史】候选 ETF × 金额网格 的逐条模拟结果（需求第五十六条）。

    每行 = 某只 ETF 的某一档买入金额 在"整个组合"上模拟出来的结果，
    用于事后复盘"当时模拟出的概率/质量 vs 后来真实发生的情况"。
    """
    return _write_rows("portfolio_simulation.csv", rows,
                       ("date", "portfolio_id", "asset_id", "amount"))


def save_rebalance_simulation(rows: list[dict]) -> int:
    """【再平衡模拟历史】黄金降到各档位（10/8/5%…）后组合质量的变化。"""
    return _write_rows("rebalance_simulation.csv", rows,
                       ("date", "portfolio_id", "gold_id", "target_pct"))


def export_all(dst_dir: Path | None = None) -> list[Path]:
    """导出全部历史数据（设置页"历史数据导出"用）。"""
    import shutil

    dst = Path(dst_dir) if dst_dir else DATA_DIR / f"export-{datetime.now():%Y%m%d-%H%M%S}"
    dst.mkdir(parents=True, exist_ok=True)
    out: list[Path] = []
    for f in sorted(DATA_DIR.rglob("*.csv")):
        rel = f.relative_to(DATA_DIR)
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, target)
        out.append(target)
    return out


def list_history_files() -> list[dict]:
    out = []
    for f in sorted(DATA_DIR.rglob("*.csv")):
        st = f.stat()
        lines = 0
        try:
            with f.open("r", encoding="utf-8-sig") as fh:
                lines = max(0, sum(1 for _ in fh) - 1)
        except Exception:
            pass
        try:
            name = str(f.relative_to(DATA_DIR)).replace("\\", "/")
        except ValueError:
            name = f.name
        out.append({
            "name": name,
            "path": str(f),
            "size_kb": round(st.st_size / 1024, 1),
            "rows": lines,
            "modified": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
        })
    return out
