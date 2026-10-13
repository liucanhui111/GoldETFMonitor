"""美元指数数据源（DXY，ICE 美元指数）。

主源：Yahoo Finance DX-Y.NYB
输出美元强弱、均线位置、20/60 日变化，用于黄金环境判断。
"""
from __future__ import annotations

from .http_util import DATA_CACHE, STATUS, fetch_with_cache
from .yahoo_util import fetch_chart

SYMBOLS = ["DX-Y.NYB", "DX=F"]


class USDSource:
    name = "美元指数"

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.timeout = int(cfg.get("app.timeout_seconds", 10))
        self.retries = int(cfg.get("app.max_retries", 3))

    def fetch(self, use_cache_first: bool = False) -> dict | None:
        def loader() -> dict:
            last = ""
            for sym in SYMBOLS:
                try:
                    data = fetch_chart(sym, "5y", "1d", self.timeout, self.retries)
                    data["source"] = f"Yahoo {sym}"
                    STATUS.report("美元指数", True, f"来源 {sym}", False, 1, data.get("data_time"))
                    return data
                except Exception as exc:  # noqa: BLE001
                    last = f"{sym}: {exc}"
            raise RuntimeError(last or "无可用美元指数源")

        res = fetch_with_cache(
            "美元指数", "usd_index", loader, DATA_CACHE,
            ttl_minutes=float(self.cfg.get("data_sources.cache_ttl_minutes.macro", 1440)),
            use_cache_first=use_cache_first,
        )
        data = res.value if res.ok else None
        if data:
            data["from_cache"] = res.from_cache
            data["cache_age_minutes"] = res.cache_age_minutes
        return data


def usd_metrics(data: dict | None) -> dict:
    if not data or not data.get("close"):
        return {"available": False, "reason": "暂无数据"}
    c = data["close"]
    n = len(c)
    out = {"available": True, "source": data.get("source"),
           "data_time": data.get("data_time"), "last": c[-1]}

    def ma(w):
        return sum(c[-w:]) / w if n >= w else None

    for w in (5, 20, 60, 120, 200):
        out[f"ma{w}"] = ma(w)
    for w in (20, 60):
        out[f"ret{w}_pct"] = (c[-1] / c[-1 - w] - 1) * 100 if n > w + 1 else None
    # 20 日变化方向（趋势）
    if n > 21:
        r20 = out["ret20_pct"]
        out["direction"] = "转弱" if r20 is not None and r20 < -1 else (
            "转强" if r20 is not None and r20 > 1 else "震荡")
        out["below_ma20"] = c[-1] < (ma(20) or c[-1])
        out["below_ma60"] = c[-1] < (ma(60) or c[-1])
        out["weak_confirmed"] = bool(out["below_ma20"] and out["below_ma60"] and (r20 or 0) < 0)
    else:
        out["direction"] = None
        out["weak_confirmed"] = None
    return out
