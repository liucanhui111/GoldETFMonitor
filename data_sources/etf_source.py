"""ETF 历史行情数据源。

主源：同花顺扶摇金融数据（/api/fund/market/historical，前复权，单次≤5 自然年）
备源：腾讯证券（web.ifzq.gtimg.cn，前复权）
三备：新浪财经

统一输出结构（DatePoint 列表）：
    {
      "code": "159937", "name": "黄金ETF博时", "source": "ths",
      "dates": ["2021-11-01", ...],
      "open": [...], "high": [...], "low": [...], "close": [...],
      "volume": [...], "turnover": [...],
      "data_time": "2026-09-30", "from_cache": False
    }
"""
from __future__ import annotations

import threading
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .http_util import (
    STATUS,
    DiskCache,
    FetchResult,
    fetch_with_cache,
    http_get_json,
    http_get_text,
)

CN_TZ = timezone(timedelta(hours=8))

# --------------------------------------------------------------------------
# 同花顺扶摇「单飞」锁（实测必需）
# --------------------------------------------------------------------------
# 背景（实测，2026-10-08）：管道默认 3 路并发预取 6 只 ETF（app.fetch_workers=3）。
# 同花顺扶摇对**并发突发**限流很重：
#   * 单进程内串行请求：每只 6~10 秒，6 只全部成功（1212/803/1079 根）；
#   * 3 路并发请求：单次响应被拖到 30~50 秒，超过取数硬超时(timeout+1.5s)，
#     被判定为「连接级失败」→ 连续 2 次即触发 **主机熔断**（15 分钟），
#     其余标的全部秒级快速失败 → 静默回落到腾讯证券（历史更短，只有 640 天）。
# 结果是：**主源明明可用，却因为并发被限流而丢掉长历史**，"真实六 ETF 共同区间"
# 从 803 天缩到 640 天——这是取数方式缺陷，不是数据源不可用。
# 修法：把扶摇的 HTTP 请求在**进程内串行化**（只允许一个在飞），并把它的超时放宽
# 到足以覆盖单只 6~10 秒的正常耗时，避免「慢但能成功」被判超时进而熔断。
_THS_LOCK = threading.Lock()


def ms_to_date(ms: int) -> str:
    """同花顺毫秒时间戳 → 交易日（Asia/Shanghai）。"""
    return datetime.fromtimestamp(ms / 1000, tz=CN_TZ).strftime("%Y-%m-%d")


def date_to_ms(d: str) -> int:
    dt = datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=CN_TZ)
    return int(dt.timestamp() * 1000)


# --------------------------------------------------------------------------
# 1) 同花顺扶摇（主源）
# --------------------------------------------------------------------------
class THSSource:
    name = "同花顺扶摇"

    def __init__(self, api_key: str, base: str = "https://fuyao.aicubes.cn",
                 timeout: int = 10, retries: int = 3) -> None:
        self.api_key = api_key
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.retries = retries

    def available(self) -> bool:
        return bool(self.api_key)

    def _headers(self) -> dict[str, str]:
        return {"X-api-key": self.api_key}

    def _get(self, path: str) -> tuple[Any, str]:
        # 进程内串行化：扶摇并发突发会被限流（见文件顶部 _THS_LOCK 说明）。
        with _THS_LOCK:
            return http_get_json(
                self.base + path,
                headers=self._headers(),
                timeout=self.timeout,
                retries=self.retries,
            )

    # -------- 日线 --------
    def history(self, thscode: str, years: int = 5) -> list[dict] | None:
        """返回原始 item 列表；超过 5 年自动分段请求。"""
        today = date.today()
        segs: list[tuple[date, date]] = []
        end = today
        remaining = years
        while remaining > 0:
            span = min(5, remaining)
            start = end - timedelta(days=int(span * 365.25))
            segs.append((start, end))
            end = start - timedelta(days=1)
            remaining -= span

        rows: dict[int, dict] = {}
        err = ""
        for start, stop in segs:
            path = (
                f"/api/fund/market/historical?thscode={thscode}"
                f"&interval=1d&start={date_to_ms(start.isoformat())}"
                f"&end={date_to_ms(stop.isoformat())}"
            )
            payload, e = self._get(path)
            if payload is None:
                err = e
                continue
            code = payload.get("code")
            if code != 0:
                err = f"code={code} {payload.get('message')}"
                continue
            for it in (payload.get("data") or {}).get("item") or []:
                rows[int(it["date_ms"])] = it
        if not rows:
            raise RuntimeError(f"同花顺历史行情不可用：{err or '空数据'}")
        return [rows[k] for k in sorted(rows)]

    # -------- 名称检索 --------
    def search(self, keyword: str, asset_type: str = "fund-etf",
               limit: int = 10) -> list[dict]:
        from urllib.parse import quote
        path = (
            f"/api/meta/tickers/search?q={quote(keyword)}"
            f"&asset_type={asset_type}&limit={limit}"
        )
        payload, err = self._get(path)
        if payload is None or payload.get("code") != 0:
            return []
        return (payload.get("data") or {}).get("item") or []

    # -------- 交易日历 --------
    def trading_days(self, start: str, end: str) -> list[str]:
        payload, _ = self._get(
            f"/api/a-share/calendar/trading-days?start={start}&end={end}"
        )
        if payload is None or payload.get("code") != 0:
            return []
        return [
            it["date"]
            for it in (payload.get("data") or {}).get("item") or []
            if it.get("date")
        ]

    def normalize(self, code: str, name: str, items: list[dict]) -> dict:
        return {
            "code": code,
            "name": name,
            "source": self.name,
            "dates": [ms_to_date(int(i["date_ms"])) for i in items],
            "open": [_f(i.get("open_price")) for i in items],
            "high": [_f(i.get("high_price")) for i in items],
            "low": [_f(i.get("low_price")) for i in items],
            "close": [_f(i.get("close_price")) for i in items],
            "volume": [_f(i.get("volume")) for i in items],
            "turnover": [_f(i.get("turnover")) for i in items],
            "data_time": ms_to_date(int(items[-1]["date_ms"])),
            "adjust": "forward",
        }


# --------------------------------------------------------------------------
# 2) 腾讯证券（备源）
# --------------------------------------------------------------------------
class TencentSource:
    name = "腾讯证券"

    def __init__(self, timeout: int = 10, retries: int = 3) -> None:
        self.timeout = timeout
        self.retries = retries

    def history(self, thscode: str, years: int = 5) -> dict | None:
        sym = _ths_to_tencent(thscode)
        bars = int(years * 250) + 20
        url = (
            "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
            f"?param={sym},day,,,{bars},qfq"
        )
        payload, err = http_get_json(url, timeout=self.timeout, retries=self.retries)
        if payload is None:
            raise RuntimeError(f"腾讯行情不可用：{err}")
        node = (payload.get("data") or {}).get(sym) or {}
        rows = node.get("qfqday") or node.get("day") or []
        if not rows:
            raise RuntimeError("腾讯行情返回空")
        return {
            "code": thscode.split(".")[0],
            "name": (node.get("qt") or {}).get(sym, [None, ""])[1] if node.get("qt") else "",
            "source": self.name,
            "dates": [r[0] for r in rows],
            "open": [_f(r[1]) for r in rows],
            "close": [_f(r[2]) for r in rows],
            "high": [_f(r[3]) for r in rows],
            "low": [_f(r[4]) for r in rows],
            "volume": [_f(r[5]) for r in rows],
            "turnover": [None] * len(rows),
            "data_time": rows[-1][0],
            "adjust": "forward",
        }

    def quote_name(self, thscode: str) -> str:
        sym = _ths_to_tencent(thscode)
        text, _ = http_get_text(
            f"https://qt.gtimg.cn/q={sym}", encoding="gbk",
            timeout=self.timeout, retries=1,
        )
        if not text or "~" not in text:
            return ""
        return text.split("~")[1] if len(text.split("~")) > 1 else ""


# --------------------------------------------------------------------------
# 3) 新浪财经（三备，仅取最近 ~1000 根）
# --------------------------------------------------------------------------
class SinaSource:
    name = "新浪财经"

    def history(self, thscode: str, years: int = 5) -> dict | None:
        ticker, ex = thscode.split(".")
        sym = ("sh" if ex.upper() == "SH" else "sz") + ticker
        url = (
            "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
            f"CN_MarketData.getKLineData?symbol={sym}&scale=240&ma=no&datalen=1300"
        )
        payload, err = http_get_json(url, timeout=10, retries=2)
        if not payload:
            raise RuntimeError(f"新浪行情不可用：{err}")
        dates = [r["day"][:10] for r in payload]
        return {
            "code": ticker,
            "name": "",
            "source": self.name,
            "dates": dates,
            "open": [_f(r.get("open")) for r in payload],
            "close": [_f(r.get("close")) for r in payload],
            "high": [_f(r.get("high")) for r in payload],
            "low": [_f(r.get("low")) for r in payload],
            "volume": [_f(r.get("volume")) for r in payload],
            "turnover": [None] * len(payload),
            "data_time": dates[-1] if dates else None,
            "adjust": "forward",
        }


# --------------------------------------------------------------------------
# 门面
# --------------------------------------------------------------------------
class ETFDataSource:
    """按 primary → fallback → 缓存 的顺序获取 ETF 日线。"""

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        ds = cfg.data
        # 扶摇单只正常耗时 6~10 秒；给足余量（默认 20s），避免"慢但能成功"被
        # 硬超时（timeout+1.5s）误判为连接失败，进而把主机熔断、静默回落短历史备源。
        try:
            ths_to = int(cfg.get("data_sources.ths_timeout_seconds", 20) or 20)
        except Exception:  # noqa: BLE001
            ths_to = 20
        self.ths = THSSource(
            cfg.ths_api_key(),
            ds.get("data_sources", {}).get("ths_base", "https://fuyao.aicubes.cn"),
            timeout=max(ths_to, int(cfg.get("app.timeout_seconds", 10) or 10)),
            retries=int(cfg.get("app.max_retries", 3)),
        )
        self.tencent = TencentSource(
            timeout=int(cfg.get("app.timeout_seconds", 10)),
            retries=int(cfg.get("app.max_retries", 3)),
        )
        self.sina = SinaSource()
        self.years = int(cfg.get("app.history_years", 5))
        from .http_util import DATA_CACHE

        self.cache = DATA_CACHE

    def fetch(self, asset: dict, use_cache_first: bool = False) -> dict | None:
        """返回标准化历史数据；失败返回 None（调用方显示"暂无数据"）。"""
        thscode = asset["thscode"]
        ticker = str(asset.get("ticker") or thscode.split(".")[0])
        label = asset.get("name") or ticker
        key = f"etf_{ticker}"

        def loader() -> dict:
            last_err = ""
            for src in (self.ths, self.tencent, self.sina):
                try:
                    if isinstance(src, THSSource):
                        if not src.available():
                            last_err = "缺少同花顺 API Key"
                            continue
                        items = src.history(thscode, self.years)
                        if not items:
                            last_err = "空数据"
                            continue
                        out = src.normalize(ticker, label, items)
                    else:
                        out = src.history(thscode, self.years)
                    if out and out.get("dates"):
                        STATUS.report(
                            f"ETF行情({ticker})",
                            True,
                            f"来源：{out['source']}",
                            False,
                            1,
                            out.get("data_time"),
                        )
                        return out
                except Exception as exc:  # noqa: BLE001
                    last_err = f"{src.name}: {type(exc).__name__}"
                    continue
            raise RuntimeError(last_err or "所有ETF数据源均失败")

        res: FetchResult = fetch_with_cache(
            f"ETF行情({ticker})",
            key,
            loader,
            self.cache,
            ttl_minutes=float(self.cfg.get("data_sources.cache_ttl_minutes.etf", 720)),
            use_cache_first=use_cache_first,
        )
        if not res.ok or not res.value:
            return None
        data = dict(res.value)
        data["from_cache"] = res.from_cache
        data["cache_age_minutes"] = res.cache_age_minutes
        data["name"] = label
        return data

    # 名称 → 代码（设置页"自动识别"用）
    def resolve_by_name(self, keyword: str) -> list[dict]:
        if not self.ths.available():
            return []
        try:
            return self.ths.search(keyword)
        except Exception:  # noqa: BLE001
            return []


def _f(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _ths_to_tencent(thscode: str) -> str:
    ticker, ex = thscode.split(".")
    return ("sh" if ex.upper() == "SH" else "sz") + ticker
