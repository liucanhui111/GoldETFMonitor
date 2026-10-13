"""DATA_FRESHNESS_GATE —— 数据新鲜度闸门。

背景（用户明确指出的问题）
--------------------------
报告里曾出现：ETF 最新交易日明显落后于宏观数据日期，却仍给出"今天"的交易建议。
本闸门在任何实际交易建议**之前**强制检查每一类数据的 data_date：

    ETF 收盘 / ETF 实时 / 黄金 / DXY / 实际利率 / GVZ / BPGDM

规则
----
1. 关键行情超过允许新鲜度 → 直接 **WAIT / DATA_STALE**，禁止输出 BUY；
2. **不得用旧收盘冒充实时**：没有真正实时行情时，必须明确写
   "基于最近交易日收盘数据，只能给下一交易日计划，不能称实时买卖建议"；
3. 代理值（BPGDM 代理 / Fear&Greed 本地代理）不参与"新鲜度合格"的判定——
   它们是研究参考，不是决策输入；
4. 缺失 data_date → 判 UNKNOWN（不视为新鲜）。
"""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

# 各类数据允许的最大"日历日"陈旧度（含周末/长假缓冲）
DEFAULT_LIMITS_DAYS: dict[str, int] = {
    "etf_close": 7,
    "etf_realtime": 1,
    "gold": 7,
    "dxy": 7,
    "real_rate": 7,
    "gvz": 7,
    "bpgdm": 7,
}

# 这些是"交易决策的关键行情"：任一 STALE/UNKNOWN 即禁止 BUY
CRITICAL_KEYS = ("etf_close",)

# 缓存文件 → 数据类别
_CACHE_FILES = {
    "gold": "gold_price.json",
    "dxy": "usd_index.json",
    "real_rate": "real_yield_10y.json",
    "gvz": "gvz_index.json",
    "bpgdm": "bpgdm.json",
}

_LABELS = {
    "etf_close": "ETF 收盘行情",
    "etf_realtime": "ETF 实时行情",
    "gold": "国际金价",
    "dxy": "美元指数 DXY",
    "real_rate": "美国10Y实际利率",
    "gvz": "黄金波动率 GVZ",
    "bpgdm": "金矿股广度 BPGDM",
}


def _to_date(s) -> date | None:
    if not s:
        return None
    t = str(s).strip()[:10]
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
        try:
            return datetime.strptime(t.replace("/", "-") if fmt != "%Y/%m/%d" else t,
                                     fmt).date()
        except ValueError:
            continue
    return None


def _cache_date(cache_dir: Path, name: str) -> str | None:
    p = cache_dir / name
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d.get("data_time")
    except Exception:  # noqa: BLE001
        return None


def collect_dates(cfg, snap: dict | None = None,
                  now: date | None = None) -> dict:
    """汇总各类数据的 data_date 与年龄（日历日）。

    优先取运行期快照，其次回落到本地缓存文件。缺失 → None。
    """
    from .config import project_root

    now = now or date.today()
    cache_dir = Path(project_root()) / "data" / "cache"
    dates: dict[str, str | None] = {}

    # ETF 收盘：优先 snap.data_status 里的 etf 项；否则读任一 etf_*.json
    etf_close = None
    if snap:
        for s in (snap.get("data_status") or []):
            if isinstance(s, dict) and str(s.get("name", "")).startswith("ETF"):
                etf_close = s.get("data_time") or etf_close
    if not etf_close:
        try:
            cands = sorted(cache_dir.glob("etf_*.json"))
            etf_close = _cache_date(cache_dir, cands[0].name) if cands else None
        except Exception:  # noqa: BLE001
            etf_close = None
    dates["etf_close"] = etf_close

    # ETF 实时：本系统无独立实时源时如实为 None（不可用旧收盘冒充）
    rt = None
    if snap:
        rt = (snap.get("realtime") or {}).get("data_date")
    dates["etf_realtime"] = rt

    for key, fname in _CACHE_FILES.items():
        d = None
        if snap:
            d = ((snap.get("macro") or {}).get(key + "_data_date"))
        if not d:
            d = _cache_date(cache_dir, fname)
        dates[key] = d

    out: dict = {}
    for key, ds in dates.items():
        dt = _to_date(ds)
        age = (now - dt).days if dt else None
        out[key] = {"data_date": ds, "age_days": age, "label": _LABELS.get(key, key)}
    return out


def check_freshness(cfg, snap: dict | None = None, *,
                    now: date | None = None,
                    limits: dict | None = None) -> dict:
    """执行新鲜度闸门，返回是否放行 BUY 与逐项明细。"""
    lim = dict(DEFAULT_LIMITS_DAYS)
    if cfg is not None:
        try:
            user = cfg.get("freshness.limits_days", {}) or {}
            for k, v in user.items():
                if k in lim and isinstance(v, (int, float)):
                    lim[k] = int(v)
        except Exception:  # noqa: BLE001
            pass
    if limits:
        lim.update({k: int(v) for k, v in limits.items()})

    items = collect_dates(cfg, snap, now=now)
    rows: list[dict] = []
    stale: list[str] = []
    unknown: list[str] = []
    for key, info in items.items():
        age = info["age_days"]
        allowed = lim.get(key, 7)
        if age is None:
            status = "UNKNOWN"
            if key != "etf_realtime":      # 实时缺失属正常，单项不计 UNKNOWN 阻断
                unknown.append(key)
        elif age > allowed:
            status = "STALE"
            stale.append(key)
        else:
            status = "OK"
        rows.append({
            "key": key, "label": info["label"], "data_date": info["data_date"],
            "age_days": age, "allowed_days": allowed, "status": status,
        })

    critical_stale = [k for k in CRITICAL_KEYS if (k in stale or k in unknown)]
    has_realtime = items.get("etf_realtime", {}).get("data_date") is not None

    if critical_stale:
        action = "DATA_STALE"
        ok = False
        reason = ("关键行情不新鲜/缺失（" + "、".join(
            _LABELS.get(k, k) for k in critical_stale) + "）→ 禁止 BUY，只能 WAIT")
    elif stale:
        action = "WAIT"
        ok = False
        reason = ("部分数据陈旧（" + "、".join(_LABELS.get(k, k) for k in stale)
                  + "）→ 建议 WAIT，不新增风险")
    else:
        action = "PASS"
        ok = True
        reason = "全部关键数据在允许新鲜度内"

    # 实时性声明（不得用旧收盘冒充实时）
    if has_realtime:
        realtime_note = "存在 ETF 实时行情，可作为当日参考；但仍以 T 日收盘产生信号、T+1 执行。"
        quote_basis = "实时行情"
    else:
        realtime_note = ("无独立实时行情：本建议基于【最近交易日收盘数据】，"
                         "只能给下一交易日计划，不能称实时买卖建议。")
        quote_basis = "最近交易日收盘"

    return {
        "ok": ok,
        "action": action,
        "reason": reason,
        "rows": rows,
        "stale": stale,
        "unknown": unknown,
        "critical_stale": critical_stale,
        "can_buy": bool(ok),
        "has_realtime": has_realtime,
        "quote_basis": quote_basis,
        "realtime_note": realtime_note,
        "checked_at": (now or date.today()).isoformat(),
    }


def render_freshness_block(res: dict) -> list[str]:
    L = ["| 数据类别 | 数据日期 | 陈旧(天) | 允许 | 状态 |", "|---|---|---|---|---|"]
    for r in res.get("rows", []):
        L.append(f"| {r['label']} | {r['data_date'] or '—'} | "
                 f"{r['age_days'] if r['age_days'] is not None else '—'} | "
                 f"{r['allowed_days']} | {r['status']} |")
    return L


def banner_text(res: dict) -> str:
    """首屏「数据陈旧」横幅文案；新鲜时返回空串。

    返回空串 ⇒ 无需提示（闸门 PASS）；否则给出
    「数据日期 + 陈旧天数 + 非实时声明」的一句话，供 GUI 首屏直接展示。
    这是 FRESHNESS GATE 结果回流首屏的出口——「为什么禁止买入」必须在第一屏可见。
    """
    res = res or {}
    bad = [r for r in (res.get("rows") or [])
           if r.get("status") in ("STALE", "UNKNOWN")]
    if not bad:
        return ""
    parts = []
    for r in bad:
        d = r.get("data_date") or "无"
        age = r.get("age_days")
        parts.append(f"{r.get('label')} {d}"
                     + (f"（已 {age} 天）" if age is not None else "（缺失）"))
    head = ("⚠ 关键行情陈旧 → 已禁止买入"
            if res.get("action") == "DATA_STALE"
            else "⚠ 部分数据陈旧 → 建议观望、不加风险")
    return (head + "：" + "；".join(parts)
            + f"。本次基于【{res.get('quote_basis') or '最近交易日收盘'}】，"
              "不构成实时买卖建议。")
