"""执行台账（Execution Ledger）——把「界面建议」变成「已执行的持仓变更」。

为什么需要它
------------
本程序**没有任何券商/成交接口**，它「知道持仓」的唯一入口是 config 的
`assets[].shares`。于是会出现一个让人困惑的现象：

    界面天天说「今天只做一件事：卖出黄金 1,300 份」，
    用户真的卖出后，如果不去设置页改份额，程序下次仍读到旧份额，
    于是**继续重复同一条建议**——因为它根本不知道你已经执行了。

本模块让用户点一次「我已按此执行」，就把建议**落到持仓上**，并留一条台账：

  · 按建议改动持仓份额 `assets[].shares` 与现金 `portfolio.cash`；
  · 写一条带日期/份额/金额的记录到 `data/execution_ledger.json`；
  · 界面据此显示「已于 X 日执行」或「尚未执行」。

安全约束（务必保持）
--------------------
  · **绝不自动下单、绝不联网**：只改本地 config 记录，由用户点击触发。
  · 改 config 之前先 `cfg.backup()`；份额不得为负、卖出不得超过持有。
  · 台账路径走 `project_root()/data`（冻结态 = exe 目录），绝不写进 `_internal`。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

LEDGER_NAME = "execution_ledger.json"
MIN_LOT = 100

ACTION_CN = {"REBALANCE": "再平衡卖出", "SELL": "卖出/减仓", "BUY": "买入"}


# --------------------------------------------------------------------------
# 路径与读写
# --------------------------------------------------------------------------
def _ledger_path() -> Path:
    from .config import project_root

    d = project_root() / "data"
    d.mkdir(parents=True, exist_ok=True)
    return d / LEDGER_NAME


def load_ledger() -> list[dict]:
    """读取全部台账条目（损坏时返回空表，绝不抛给界面）。"""
    p = _ledger_path()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    return data if isinstance(data, list) else []


def _append(entry: dict) -> None:
    entries = load_ledger()
    entries.append(entry)
    p = _ledger_path()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(entries, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(p)


def signature(action: str, asset_id: str, target_pct: float | None = None) -> str:
    """同一条建议的稳定标识：动作 | 资产 | 目标权重。"""
    t = "" if target_pct is None else f"{round(float(target_pct), 2)}"
    return f"{action}|{asset_id}|{t}"


def last_entry_for(sig: str) -> dict | None:
    for e in reversed(load_ledger()):
        if e.get("signature") == sig:
            return e
    return None


def last_execution() -> dict | None:
    entries = load_ledger()
    return entries[-1] if entries else None


# --------------------------------------------------------------------------
# 建议 → 台账状态
# --------------------------------------------------------------------------
def advice_signature(hl: dict, rebal_sim: dict | None = None) -> str | None:
    """从第一屏结论 (headline) 推导"当前这条建议"的签名；无建议返回 None。

    rebal_sim = 快照的 pk["rebal_sim"]（含 best 与 gold_id）。
    """
    key = (hl or {}).get("key")
    rs = rebal_sim or {}
    rb = rs.get("best") or {}
    if key == "BLUE_REBALANCE":
        if rb.get("target_pct") is not None and rb.get("sell_shares"):
            return signature("REBALANCE", str(rs.get("gold_id") or "gold"),
                             rb.get("target_pct"))
        return None
    if key in ("GREEN_BUY", "GREEN_PROBE"):
        aid = (hl or {}).get("asset_id") or (hl or {}).get("name")
        if aid:
            return signature("BUY", str(aid), None)
    return None


def status_text(hl: dict, rebal_sim: dict | None = None) -> dict:
    """给界面用：返回 {actionable, signature, executed, last, label}。

    label 是可直接显示的中文一句话。
    """
    sig = advice_signature(hl, rebal_sim)
    last = last_execution()
    if sig is None:
        return {"actionable": False, "signature": None, "executed": False,
                "last": last, "label": _last_line(last)}
    done = last_entry_for(sig)
    if done:
        label = (f"台账：这条方案已于 {done.get('date')} 按 {int(abs(done.get('shares_delta') or 0)):,} 份"
                 f"执行过（份额 {done.get('shares_before')}→{done.get('shares_after')}）。"
                 "若仍看到同一建议，请确认持仓是否已同步。")
    else:
        label = ("台账：这条建议**尚未执行**。执行后请点下方按钮，"
                 "程序会更新持仓并记住这次操作。")
    return {"actionable": True, "signature": sig, "executed": bool(done),
            "last": last, "label": label}


def _last_line(last: dict | None) -> str:
    if not last:
        return "台账：暂无执行记录。"
    return (f"台账：最近一次执行 {last.get('date')} "
            f"{ACTION_CN.get(last.get('action'), last.get('action'))} "
            f"{last.get('name')} {int(abs(last.get('shares_delta') or 0)):,} 份"
            f"（约 {float(last.get('amount') or 0):,.0f} 元）→ "
            f"份额 {last.get('shares_before')}→{last.get('shares_after')}")


# --------------------------------------------------------------------------
# 执行：把建议落到持仓
# --------------------------------------------------------------------------
def _find(assets: list[dict], asset_id) -> dict | None:
    for a in assets:
        if str(a.get("id")) == str(asset_id):
            return a
    return None


def _apply_cash(cfg, amount: float, *, add: bool) -> tuple[float | None, float | None]:
    """更新现金；返回 (before, after)。现金原为未填(None)时按成交额登记。"""
    before = cfg.declared_cash
    amt = float(amount or 0.0)
    if amt <= 0:
        return before, before
    if before is None:
        after = amt if add else -amt
    else:
        after = float(before) + (amt if add else -amt)
    cfg.set("portfolio.cash", round(after, 2), autosave=False)
    return before, round(after, 2)


def execute_rebalance(cfg, *, asset_id, name, sell_shares, price,
                      amount, target_pct, note: str = "") -> dict:
    """执行一次再平衡卖出：减份额 + 加现金 + 写台账。返回台账条目。"""
    sell_shares = int(sell_shares)
    if sell_shares <= 0:
        raise ValueError("卖出份数必须为正整数")
    assets = cfg.assets
    a = _find(assets, asset_id)
    if a is None:
        raise ValueError(f"当前组合里找不到资产 {asset_id}")
    have = int(float(a.get("shares") or 0))
    if sell_shares > have:
        raise ValueError(f"卖出 {sell_shares} 份 超过当前持有 {have} 份")
    try:
        cfg.backup()
    except Exception:  # noqa: BLE001
        pass
    a["shares"] = have - sell_shares
    cfg.set_assets(assets)
    cash_before, cash_after = _apply_cash(cfg, amount, add=True)
    cfg.save()
    ts = datetime.now().isoformat(timespec="seconds")
    entry = {
        "ts": ts, "date": ts[:10],
        "signature": signature("REBALANCE", asset_id, target_pct),
        "action": "REBALANCE", "asset_id": str(asset_id), "name": name,
        "shares_before": have, "shares_after": have - sell_shares,
        "shares_delta": -sell_shares, "price": price,
        "amount": round(float(amount or 0.0), 2), "target_pct": target_pct,
        "cash_before": cash_before, "cash_after": cash_after, "note": note,
    }
    _append(entry)
    return entry


def execute_buy(cfg, *, asset_id, name, buy_shares, price, amount,
                note: str = "") -> dict:
    """执行一次买入：加份额 + 减现金 + 写台账（研究级下界面不开放，仅供内部/测试）。"""
    buy_shares = int(buy_shares)
    if buy_shares <= 0:
        raise ValueError("买入份数必须为正整数")
    assets = cfg.assets
    a = _find(assets, asset_id)
    if a is None:
        raise ValueError(f"当前组合里找不到资产 {asset_id}")
    have = int(float(a.get("shares") or 0))
    amt = float(amount or 0.0)
    cash_before = cfg.declared_cash
    if cash_before is not None and amt > float(cash_before) + 1e-6:
        raise ValueError(f"现金不足：需要 {amt:,.2f} 元，可用 {float(cash_before):,.2f} 元")
    try:
        cfg.backup()
    except Exception:  # noqa: BLE001
        pass
    a["shares"] = have + buy_shares
    cfg.set_assets(assets)
    cash_before2, cash_after = _apply_cash(cfg, amt, add=False)
    cfg.save()
    ts = datetime.now().isoformat(timespec="seconds")
    entry = {
        "ts": ts, "date": ts[:10],
        "signature": signature("BUY", asset_id, None),
        "action": "BUY", "asset_id": str(asset_id), "name": name,
        "shares_before": have, "shares_after": have + buy_shares,
        "shares_delta": buy_shares, "price": price,
        "amount": round(amt, 2), "target_pct": None,
        "cash_before": cash_before2, "cash_after": cash_after, "note": note,
    }
    _append(entry)
    return entry


def record_event(action: str, *, note: str = "", asset_id=None, name=None,
                 extra: dict | None = None) -> dict:
    """记录一条**不改变持仓**的台账事件（SKIP / NOTE / SET 等）。

    用于「指令栏」里那些只留痕、不动份额的操作：忽略某条建议、写备注、
    修改现金/总资产/上限/目标配置。仅向台账追加一行，绝不触碰持仓。
    """
    ts = datetime.now().isoformat(timespec="seconds")
    entry = {
        "ts": ts, "date": ts[:10],
        "signature": signature(action, str(asset_id or ""), None),
        "action": action, "asset_id": str(asset_id) if asset_id else None,
        "name": name, "shares_before": None, "shares_after": None,
        "shares_delta": 0, "price": None, "amount": 0.0,
        "cash_before": None, "cash_after": None, "note": note,
    }
    if extra:
        entry.update(extra)
    _append(entry)
    return entry
