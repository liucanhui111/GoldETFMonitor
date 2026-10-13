"""黄金情绪指标（Fear & Greed）。

说明（重要，避免任何"假数据"）：
* 官方 Gold Fear & Greed 指数没有稳定的免费公开接口。若用户在
  「设置 → 数据源」填写 `fear_greed_url`，程序会解析该地址并显示官方值。
* 未配置或抓取失败时，官方值显示"暂无数据"，**不会伪造**。
* 同时程序提供"黄金情绪代理分"——完全由真实市场数据（金价动量、RSI、
  波动率、美元、实际利率）按公开公式计算，界面会明确标注为"本地计算代理"。
"""
from __future__ import annotations

import json
from datetime import date

from .http_util import DATA_CACHE, STATUS, fetch_with_cache, http_get_json


class FearGreedSource:
    name = "Gold Fear & Greed"

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.url = str(cfg.get("data_sources.fear_greed_url", "") or "").strip()
        self.timeout = int(cfg.get("app.timeout_seconds", 10))

    # ---------------- 官方值（可选） ----------------
    def fetch_official(self, use_cache_first: bool = False) -> dict | None:
        if not self.url:
            STATUS.report(self.name, False, "未配置官方数据源 URL（显示暂无数据）")
            return None

        def loader() -> dict:
            payload, err = http_get_json(self.url, timeout=self.timeout, retries=2)
            if payload is None:
                raise RuntimeError(err)
            score, ts = _dig_score(payload)
            if score is None:
                raise RuntimeError("响应中找不到情绪分数字段")
            return {"score": score, "data_time": ts or date.today().isoformat(),
                    "raw": payload, "source": "官方(用户配置)"}

        res = fetch_with_cache(
            self.name, "gold_fear_greed", loader, DATA_CACHE,
            ttl_minutes=float(self.cfg.get("data_sources.cache_ttl_minutes.sentiment", 1440)),
            use_cache_first=use_cache_first,
        )
        if not res.ok:
            return None
        d = dict(res.value)
        d["from_cache"] = res.from_cache
        return d

    # ---------------- 区域判定 ----------------
    @staticmethod
    def zone(score: float | None) -> str:
        if score is None:
            return "暂无数据"
        if score <= 20:
            return "极度恐慌"
        if score <= 40:
            return "恐慌"
        if score <= 60:
            return "中性"
        if score <= 80:
            return "贪婪"
        return "极度贪婪"


def _dig_score(payload) -> tuple[float | None, str | None]:
    """在任意 JSON 里尽力寻找 0-100 的情绪分数字段。"""
    if isinstance(payload, (int, float)):
        return float(payload), None
    if isinstance(payload, dict):
        for k in ("score", "value", "fear_greed", "fearAndGreed", "index"):
            v = payload.get(k)
            if isinstance(v, (int, float)) and 0 <= float(v) <= 100:
                return float(v), payload.get("date") or payload.get("timestamp")
        # 递归一层
        for v in payload.values():
            got = _dig_score(v)
            if got[0] is not None:
                return got
    if isinstance(payload, list) and payload:
        return _dig_score(payload[0])
    if isinstance(payload, str):
        try:
            return _dig_score(json.loads(payload))
        except Exception:  # noqa: BLE001
            pass
    return None, None


# --------------------------------------------------------------------------
# 黄金情绪代理分（本地计算，公式公开）
# --------------------------------------------------------------------------
def gold_sentiment_proxy(gold: dict | None, usd: dict | None,
                         real_rate: dict | None) -> dict:
    """0-100，越高越贪婪。所有输入均为真实市场数据。"""
    gm = _gold_inputs(gold)
    um = _usd_inputs(usd)
    rm = _rate_inputs(real_rate)
    if gm is None:
        return {"available": False, "reason": "金价数据缺失，无法计算代理情绪"}

    parts: list[tuple[str, float, float]] = []  # (名称, 0-100分, 权重)

    dev = gm["dev_ma200_pct"]
    parts.append(("金价相对200日均线", _clip(50 + dev * 4.0), 0.22))

    parts.append(("20日动量", _clip(50 + gm["ret20_pct"] * 2.0), 0.18))
    parts.append(("RSI14", _clip(gm["rsi14"]), 0.18))
    parts.append(("距52周高点回撤", _clip(100 + gm["dd_from_high_pct"] * 2.0), 0.12))
    parts.append(("20日波动率(反向)", _clip(100 - gm["vol20_pct"] * 3.5), 0.12))

    if um is not None:
        parts.append(("美元指数20日变化(反向)", _clip(50 - um * 2.5), 0.10))
    if rm is not None:
        parts.append(("10年期实际利率20日变化(反向)", _clip(50 - rm * 40.0), 0.08))

    total_w = sum(w for _, _, w in parts)
    score = sum(s * w for _, s, w in parts) / total_w
    return {
        "available": True,
        "score": round(score, 1),
        "label": "黄金情绪代理分（本地计算）",
        "formula": "金价动量 + RSI + 距离高点 + 波动率 + 美元 + 实际利率 加权合成",
        "components": [{"name": n, "score": round(s, 1), "weight": w} for n, s, w in parts],
    }


def _gold_inputs(gold: dict | None):
    if not gold or not gold.get("close"):
        return None
    c = gold["close"]
    n = len(c)
    if n < 30:
        return None
    ma200 = sum(c[-200:]) / min(200, n)
    dev = (c[-1] / ma200 - 1) * 100
    ret20 = (c[-1] / c[-21] - 1) * 100 if n > 21 else 0.0
    gains, losses = [], []
    for i in range(n - 14, n):
        d = c[i] - c[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    ag = sum(gains) / 14
    al = sum(losses) / 14
    rsi = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    hi = max(c[-252:]) if n >= 20 else max(c)
    dd = (c[-1] / hi - 1) * 100
    rs = [c[i] / c[i - 1] - 1 for i in range(n - 20, n)] if n > 21 else [0.0]
    mean = sum(rs) / len(rs)
    var = sum((x - mean) ** 2 for x in rs) / max(1, len(rs) - 1)
    vol = (var ** 0.5) * (252 ** 0.5) * 100
    return {"dev_ma200_pct": dev, "ret20_pct": ret20, "rsi14": rsi,
            "dd_from_high_pct": dd, "vol20_pct": vol}


def _usd_inputs(usd: dict | None):
    if not usd or not usd.get("close"):
        return None
    c = usd["close"]
    if len(c) <= 21:
        return None
    return (c[-1] / c[-21] - 1) * 100


def _rate_inputs(rate: dict | None):
    if not rate or not rate.get("close"):
        return None
    c = rate["close"]
    if len(c) <= 21:
        return None
    return c[-1] - c[-21]


def _clip(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, float(v)))
