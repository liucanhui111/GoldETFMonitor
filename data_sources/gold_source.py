"""国际金价数据源。

主源：Yahoo Finance —— COMEX 黄金期货连续合约 GC=F（美元/盎司）
备用：Yahoo —— 上海金/现货替代 XAUUSD=X（若可用）

输出通过 fetch 返回统一结构；失败返回 None，界面显示"暂无数据"。
"""
from __future__ import annotations

from .http_util import DATA_CACHE, STATUS, fetch_with_cache
from .yahoo_util import fetch_chart

CANDIDATES = ["GC=F", "XAUUSD=X"]


class GoldSource:
    name = "国际金价"

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.timeout = int(cfg.get("app.timeout_seconds", 10))
        self.retries = int(cfg.get("app.max_retries", 3))

    def fetch(self, use_cache_first: bool = False) -> dict | None:
        def loader() -> dict:
            last = ""
            for sym in CANDIDATES:
                try:
                    data = fetch_chart(sym, "5y", "1d", self.timeout, self.retries)
                    data["source"] = f"Yahoo {sym}"
                    STATUS.report(
                        "国际金价", True, f"来源 {sym}", False, 1, data.get("data_time")
                    )
                    return data
                except Exception as exc:  # noqa: BLE001
                    last = f"{sym}: {exc}"
            raise RuntimeError(last or "无可用金价源")

        res = fetch_with_cache(
            "国际金价", "gold_price", loader, DATA_CACHE,
            ttl_minutes=float(self.cfg.get("data_sources.cache_ttl_minutes.macro", 1440)),
            use_cache_first=use_cache_first,
        )
        data = res.value if res.ok else None
        if data:
            data["from_cache"] = res.from_cache
            data["cache_age_minutes"] = res.cache_age_minutes
        return data


# --------------------------------------------------------------------------
# 指标计算（全部基于真实序列，公开公式）
# --------------------------------------------------------------------------
def gold_metrics(data: dict | None) -> dict:
    """金价趋势指标：MA、距离高点、20日/60日涨跌、年化波动。"""
    if not data or not data.get("close"):
        return {"available": False, "reason": "暂无数据"}
    close = data["close"]
    n = len(close)
    out: dict = {"available": True, "source": data.get("source"),
                 "data_time": data.get("data_time"), "symbol": data.get("symbol")}

    def ma(w: int):
        if n < w:
            return None
        return sum(close[-w:]) / w

    for w in (5, 20, 60, 120, 200):
        out[f"ma{w}"] = ma(w)
    out["last"] = close[-1]

    def ret(w: int):
        return (close[-1] / close[-1 - w] - 1) * 100 if n > w + 1 else None

    out["ret20_pct"] = ret(20)
    out["ret60_pct"] = ret(60)
    out["ret120_pct"] = ret(120)
    out["high_252"] = max(close[-252:]) if n >= 20 else max(close)
    out["low_252"] = min(close[-252:]) if n >= 20 else min(close)
    out["dd_from_high_pct"] = (close[-1] / out["high_252"] - 1) * 100

    # 20日年化波动率
    if n > 21:
        rs = [close[i] / close[i - 1] - 1 for i in range(n - 20, n)]
        mean = sum(rs) / len(rs)
        var = sum((x - mean) ** 2 for x in rs) / max(1, len(rs) - 1)
        out["vol20_annual_pct"] = (var ** 0.5) * (252 ** 0.5) * 100
    else:
        out["vol20_annual_pct"] = None

    # 是否停止创新低（近20日最低是否出现在最后5日内）
    if n >= 25:
        lows20 = close[-20:]
        min_idx = lows20.index(min(lows20))
        out["low_position_in_20d"] = min_idx  # 0=20日前, 19=最新
        out["stopped_new_low"] = min_idx < 15
    else:
        out["low_position_in_20d"] = None
        out["stopped_new_low"] = None
    return out
