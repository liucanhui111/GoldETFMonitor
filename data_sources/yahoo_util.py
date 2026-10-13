"""Yahoo Finance Chart 接口封装。

用于获取无法通过 A 股渠道拿到的国际品种：
美元指数(DX-Y.NYB)、COMEX 黄金(GC=F)、美债收益率(^TNX) 等。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .http_util import http_get_json

BASE = "https://query1.finance.yahoo.com/v8/finance/chart/"


def fetch_chart(
    symbol: str,
    range_: str = "5y",
    interval: str = "1d",
    timeout: int = 10,
    retries: int = 3,
) -> dict | None:
    """返回 {dates, close, high, low, open, volume, data_time}。"""
    from urllib.parse import quote

    url = f"{BASE}{quote(symbol)}?range={range_}&interval={interval}"
    payload, err = http_get_json(url, timeout=timeout, retries=retries)
    if payload is None:
        raise RuntimeError(f"Yahoo {symbol} 请求失败：{err}")
    chart = payload.get("chart") or {}
    if chart.get("error"):
        raise RuntimeError(f"Yahoo {symbol} 错误：{chart['error']}")
    result = (chart.get("result") or [None])[0]
    if not result:
        raise RuntimeError(f"Yahoo {symbol} 无结果")

    ts = result.get("timestamp") or []
    quote_block = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    adj = ((result.get("indicators") or {}).get("adjclose") or [{}])
    closes = (adj[0].get("adjclose") if adj and adj[0].get("adjclose") else None) or quote_block.get("close") or []

    dates, close = [], []
    opens, highs, lows, vols = [], [], [], []
    for i, t in enumerate(ts):
        c = closes[i] if i < len(closes) else None
        if c is None:
            continue
        dates.append(datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d"))
        close.append(float(c))
        opens.append(_nn(quote_block.get("open"), i))
        highs.append(_nn(quote_block.get("high"), i))
        lows.append(_nn(quote_block.get("low"), i))
        vols.append(_nn(quote_block.get("volume"), i))
    if not dates:
        raise RuntimeError(f"Yahoo {symbol} 无有效收盘价")

    return {
        "symbol": symbol,
        "name": ((result.get("meta") or {}).get("shortName") or symbol),
        "dates": dates,
        "close": close,
        "open": opens,
        "high": highs,
        "low": lows,
        "volume": vols,
        "data_time": dates[-1],
        "currency": (result.get("meta") or {}).get("currency"),
    }


def _nn(seq: Any, i: int) -> float | None:
    try:
        v = seq[i] if seq and i < len(seq) else None
        return None if v is None else float(v)
    except Exception:  # noqa: BLE001
        return None
