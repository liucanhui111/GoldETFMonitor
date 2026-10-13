"""黄金波动率指数 GVZ（Cboe Gold ETF Volatility Index）。

一、真实定义（依据 Cboe 官方）
  * 由 Cboe 发布：基于 GLD（SPDR 黄金 ETF）期权价格、按与 VIX 相同的算法
    计算的「未来 30 天预期波动率」，以**年化百分比**表示（2008-06-03 起）。
  * **只测波动幅度，不测方向**：GVZ 上行在黄金牛市里同样常见
    （2026-01 黄金创历史新高时 GVZ 反而冲到 46），不能当作单纯的利空信号。
  * GVZ **不是**黄金涨跌指标、**不是**超买超卖指标、**没有**官方买卖阈值。

  ⇒ 因此本系统**禁止**出现「GVZ<20 = CBOE 官方买点」「GVZ>30 = 官方卖点」
    这类表述。文中的 20 / 30 等数字一律标注为【系统研究参数】，
    仅为本项目自定义的研究分档，**不是官方参数**。

二、数据源优先级（取不到就如实 UNKNOWN，绝不填 0 / 随机值 / 旧值 / 代理值）
  1. CBOE 官方延迟行情 API（公开、无 Key）
     https://cdn.cboe.com/api/global/delayed_quotes/volatility/historical/_GVZ.json
  2. Yahoo Finance ^GVZ（range=10y，约 2500 根日线）
  3. FRED GVZCLS（Cboe 官方序列镜像，2008 至今全历史）
  4. MacroMicro GVZ（第三方聚合，尽力尝试）
  5. Moomoo .GVZ-US（第三方聚合，尽力尝试）
  6. TradingView CBOE:GVZ（第三方聚合，尽力尝试）

  每一次取值都必须保存：source / data_date / raw value；
  第三方聚合源取到的值必须通过合理性校验（3.0~150.0），否则视为失败。
  主口径要求至少 30 根历史（分位/趋势/z-score 都依赖序列长度）：
  Moomoo / TradingView 只提供最新快照、无历史序列，故仅作交叉验证参考，
  不能进入主口径；取不到就如实 UNKNOWN，绝不用旧值或代理值冒充实时值。

三、指标字段（gvz_metrics 输出）
  gvz, gvz_change_1d, gvz_ret_5, gvz_ret_10, gvz_ret_20,
  gvz_ma20, gvz_bias20, gvz_52w_high, gvz_52w_low, gvz_52w_percentile,
  gvz_zscore, gvz_spike, gvz_trend, gvz_source, gvz_data_date
  其中：
    · gvz_spike            —— 短期波动率是否突然快速抬升（研究参数阈值）
    · gvz_52w_percentile   —— 当前 GVZ 在近一年分布中的位置（优先使用，
                              不只看单一固定绝对值）
    · gvz_source/gvz_data_date —— 来源与数据日期（实时值 vs 缓存值一目了然）
"""
from __future__ import annotations

import json
import re
import statistics

from .http_util import DATA_CACHE, STATUS, fetch_with_cache, http_get_json, http_get_text
from .yahoo_util import fetch_chart

CBOE_URL = ("https://cdn.cboe.com/api/global/delayed_quotes/volatility/"
            "historical/_GVZ.json")
FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=GVZCLS"
MACROMICRO_URL = "https://en.macromicro.me/charts/data/2055"
MOOMOO_URL = "https://www.moomoo.com/quote/GVZ-US"
TRADINGVIEW_URL = ("https://scanner.tradingview.com/symbol"
                   "?symbol=CBOE%3AGVZ&fields=close,change")
CANDIDATES = ["^GVZ", "GVZ"]
YAHOO_RANGE = "10y"   # 2y 太短（分位/极值不稳），10y 约 2500 根日线

# --------------------------------------------------------------------------
# 研究分档（★系统研究参数★ —— 非 Cboe 官方阈值，Cboe 不发布 GVZ 买卖阈值）
#   值 < 上界 即落入该档；index 越大 = 波动越高
# --------------------------------------------------------------------------
GVZ_BANDS = [
    (10.0, "极端平静"),
    (15.0, "异常安静"),
    (20.0, "正常基准"),
    (30.0, "偏高"),
    (50.0, "高"),
    (float("inf"), "极端压力"),
]
GVZ_BAND_BASIS = "系统研究参数（非官方阈值）"

# 研究参数（★系统研究参数★，仅本项目自定义，非官方）
GVZ_SPIKE_CHG_1D = 3.0    # 单日抬升 ≥3.0 个点 → 视为短期骤升
GVZ_SPIKE_RET_5 = 20.0    # 5 日相对涨幅 ≥20% → 视为短期骤升
GVZ_TREND_SLOPE = 0.20    # MA20 的 5 日斜率（点）超过该值才算上行/下行
GVZ_MA_WINDOW = 20
GVZ_YEAR_BARS = 252       # 近一年 ≈ 252 个交易日
GVZ_MIN_PLAUSIBLE = 3.0   # 合理性下限（第三方源误抓其他价格时会被拒）
GVZ_MAX_PLAUSIBLE = 150.0
MIN_HISTORY_BARS = 30     # 主口径最低历史根数（不足则弃用该源，绝不硬算）


def gvz_band(value) -> dict | None:
    """返回 GVZ 所处【系统研究分档】。index 越大 = 波动越高。非官方阈值。"""
    if value is None:
        return None
    for i, (hi, name) in enumerate(GVZ_BANDS):
        if value < hi:
            lo = 0.0 if i == 0 else GVZ_BANDS[i - 1][0]
            return {
                "index": i, "name": name, "lo": lo, "hi": hi,
                "basis": GVZ_BAND_BASIS, "official": False,
                # 低位 = 正常基准及以下（< 20）：条件② 用（研究参数）
                "below_normal": i <= 2,
                # 偏低 = 偏高及以下（< 30）：条件③ 的"低位拐头"前提（研究参数）
                "low_zone": i <= 3,
            }
    return None


def _plausible(value: float | None) -> bool:
    return value is not None and GVZ_MIN_PLAUSIBLE <= value <= GVZ_MAX_PLAUSIBLE


def _pack(dates, close, source: str, raw=None, data_time=None) -> dict:
    """统一内部数据契约：dates / close / raw / data_date / source。"""
    d = str(data_time or (dates[-1] if dates else ""))[:10]
    return {"symbol": "^GVZ", "name": "Cboe Gold ETF Volatility Index",
            "dates": [str(x)[:10] for x in dates], "close": close,
            "high": None, "low": None, "volume": None,
            "raw": raw, "data_date": d, "data_time": d, "source": source}


# --------------------------------------------------------------------------
# 各数据源实现
# --------------------------------------------------------------------------
def _fetch_cboe(timeout: int, retries: int) -> dict:
    """CBOE 官方历史数据：data 数组 [datetime, open, high, low, close]。"""
    text, err = http_get_text(CBOE_URL, timeout=timeout, retries=retries)
    if not text:
        raise RuntimeError(f"CBOE 不可用：{err}")
    try:
        payload = json.loads(text)
        rows = (payload.get("data") or [])
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"CBOE 响应解析失败：{exc}")
    dates, close = [], []
    for r in rows:
        try:
            dates.append(str(r[0])[:10])
            close.append(float(r[4]))
        except (ValueError, IndexError, TypeError):
            continue
    if len(dates) < 30:
        raise RuntimeError("CBOE 返回数据过短")
    return _pack(dates, close, "CBOE 官方", raw=close[-1] if close else None)


def _fetch_yahoo(timeout: int, retries: int) -> dict:
    """Yahoo Finance ^GVZ（10y 全历史优先，再退回 2y）。"""
    errors = []
    for sym in CANDIDATES:
        for rng in (YAHOO_RANGE, "2y"):
            try:
                data = fetch_chart(sym, rng, "1d", timeout, retries)
                if not data.get("close"):
                    raise RuntimeError("空序列")
                return _pack(data["dates"], data["close"],
                             f"Yahoo {sym}（{rng}）",
                             raw=data["close"][-1], data_time=data.get("data_time"))
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{sym}/{rng}: {exc}")
    raise RuntimeError("; ".join(errors) or "Yahoo 无可用 ^GVZ 序列")


def _fetch_fred(timeout: int, retries: int) -> dict:
    """FRED GVZCLS：Cboe 官方序列镜像，CSV 两列 observation_date,GVZCLS。"""
    text, err = http_get_text(FRED_URL, timeout=timeout, retries=retries)
    if not text:
        raise RuntimeError(f"FRED 不可用：{err}")
    dates, close = [], []
    for i, line in enumerate(text.splitlines()):
        parts = line.strip().split(",")
        if len(parts) < 2:
            continue
        d = parts[0].strip()
        v = parts[1].strip()
        if i == 0 or not d or v in ("", ".", "NA"):
            continue
        try:
            close.append(float(v))
            dates.append(d[:10])
        except ValueError:
            continue
    if len(dates) < 30:
        raise RuntimeError("FRED 返回数据过短")
    order = sorted(range(len(dates)), key=lambda i: dates[i])
    return _pack([dates[i] for i in order], [close[i] for i in order],
                 "FRED GVZCLS（Cboe 官方序列镜像）",
                 raw=close[order[-1]])


def _fetch_macromicro(timeout: int, retries: int) -> dict:
    """MacroMicro GVZ（第三方聚合）：尽力解析其 series JSON；失败即弃用。"""
    payload, err = http_get_json(MACROMICRO_URL, timeout=timeout, retries=retries)
    if payload is None:
        raise RuntimeError(f"MacroMicro 不可用：{err}")
    pairs: list[tuple[str, float]] = []
    stack = [payload]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            stack.extend(cur.values())
        elif isinstance(cur, list):
            for it in cur:
                if (isinstance(it, (list, tuple)) and len(it) >= 2):
                    try:
                        ts, v = it[0], float(it[1])
                    except (TypeError, ValueError):
                        continue
                    if _plausible(v):
                        pairs.append((str(ts), v))
                elif isinstance(it, (dict, list)):
                    stack.append(it)
    if len(pairs) < 30:
        raise RuntimeError("MacroMicro 未解析出足够历史（可能被反爬/改版）")
    pairs.sort(key=lambda x: x[0])
    ds = [_epoch_or_str(t) for t, _ in pairs]
    vs = [v for _, v in pairs]
    return _pack(ds, vs, "MacroMicro GVZ（第三方聚合）", raw=vs[-1])


def _fetch_moomoo(timeout: int, retries: int) -> dict:
    """Moomoo .GVZ-US（第三方聚合）：仅能尽力抓取最新值；失败即弃用。"""
    text, err = http_get_text(MOOMOO_URL, timeout=timeout, retries=retries)
    if not text:
        raise RuntimeError(f"Moomoo 不可用：{err}")
    m = re.search(r"GVZ[^0-9]{0,40}([0-9]{1,3}\.[0-9]{1,2})", text)
    if not m:
        raise RuntimeError("Moomoo 页面未解析出 GVZ 值")
    v = float(m.group(1))
    if not _plausible(v):
        raise RuntimeError(f"Moomoo 解析值不合理：{v}")
    raise RuntimeError("Moomoo 仅提供实时快照、无历史序列，不足以计算分位 → 弃用")


def _fetch_tradingview(timeout: int, retries: int) -> dict:
    """TradingView CBOE:GVZ（第三方聚合）：仅提供最新值；失败即弃用。"""
    payload, err = http_get_json(TRADINGVIEW_URL, timeout=timeout, retries=retries)
    if payload is None:
        raise RuntimeError(f"TradingView 不可用：{err}")
    v = (payload or {}).get("close") if isinstance(payload, dict) else None
    try:
        v = float(v)
    except (TypeError, ValueError):
        raise RuntimeError("TradingView 未返回 GVZ 数值")
    if not _plausible(v):
        raise RuntimeError(f"TradingView 解析值不合理：{v}")
    raise RuntimeError("TradingView 仅提供最新值、无历史序列，不足以计算分位 → 弃用")


def _epoch_or_str(ts: str) -> str:
    """把可能的时间戳统一成 YYYY-MM-DD。"""
    s = str(ts).strip()
    if s.isdigit() and len(s) >= 10:
        try:
            from datetime import datetime, timezone
            return datetime.fromtimestamp(int(s[:10]), tz=timezone.utc
                                          ).strftime("%Y-%m-%d")
        except Exception:  # noqa: BLE001
            return s[:10]
    return re.sub(r"[/]", "-", s)[:10]


class GVZSource:
    name = "黄金波动率指数GVZ"

    # 数据源优先级（顺序即尝试顺序）
    CHAIN = (
        ("CBOE 官方", _fetch_cboe),
        ("Yahoo Finance ^GVZ", _fetch_yahoo),
        ("FRED GVZCLS（官方镜像）", _fetch_fred),
        ("MacroMicro GVZ", _fetch_macromicro),
        ("Moomoo .GVZ-US", _fetch_moomoo),
        ("TradingView CBOE:GVZ", _fetch_tradingview),
    )

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.timeout = int(cfg.get("app.timeout_seconds", 10))
        self.retries = int(cfg.get("app.max_retries", 3))
        self.fred_enabled = bool(cfg.get("data_sources.fred_enabled", True))

    def _load(self) -> dict:
        errors = []
        for label, fn in self.CHAIN:
            if label.startswith("FRED") and not self.fred_enabled:
                continue
            try:
                data = fn(self.timeout, self.retries)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{label}: {exc}")
                continue
            # 主口径要求足够历史（分位 / 趋势 / z-score 都依赖序列长度）
            if len(data.get("close") or []) < MIN_HISTORY_BARS:
                errors.append(f"{label}: 历史不足 {MIN_HISTORY_BARS} 根，弃用")
                continue
            STATUS.report(self.name, True, f"来源 {label}", False, 1,
                          data.get("data_time"))
            return data
        # 全部失败：如实登记异常，绝不返回伪造值
        STATUS.report(self.name, False, "; ".join(errors) or "无可用 GVZ 源",
                      False, 0, None)
        raise RuntimeError("; ".join(errors) or "无可用 GVZ 源")

    def fetch(self, use_cache_first: bool = False) -> dict | None:
        res = fetch_with_cache(
            self.name, "gvz_index", self._load, DATA_CACHE,
            ttl_minutes=float(self.cfg.get("data_sources.cache_ttl_minutes.sentiment", 1440)),
            use_cache_first=use_cache_first,
        )
        data = res.value if res.ok else None
        if data:
            data["from_cache"] = res.from_cache
            data["cache_age_minutes"] = res.cache_age_minutes
        return data


# --------------------------------------------------------------------------
# 指标
# --------------------------------------------------------------------------
def _unavailable(reason: str = "暂无数据") -> dict:
    """源失败时的标准返回：全部字段 UNKNOWN，绝不用固定假值占位。"""
    return {
        "available": False, "status": "UNKNOWN", "reason": reason,
        "gvz": None, "gvz_raw_value": None, "last": None,
        "gvz_change_1d": None, "gvz_ret_5": None, "gvz_ret_10": None,
        "gvz_ret_20": None, "gvz_ma20": None, "gvz_bias20": None,
        "gvz_52w_high": None, "gvz_52w_low": None, "gvz_52w_percentile": None,
        "gvz_zscore": None, "gvz_spike": None, "gvz_trend": "UNKNOWN",
        "gvz_source": None, "gvz_data_date": None,
        "samples": 0, "band": None, "band_name": "暂无数据", "band_index": None,
        "chg_5d": None, "chg_10d": None, "chg_20d": None,
        "hist_low": None, "hist_high": None, "percentile_hist": None,
        "below_20": None, "low_zone": None, "turning_up": None,
    }


def gvz_metrics(data: dict | None) -> dict:
    """GVZ 当前值、变化、研究分档与底部判定。GVZ 是年化波动率（%）。"""
    if not data or not data.get("close"):
        return _unavailable()
    c = [v for v in data["close"] if v is not None]
    n = len(c)
    if not n:
        return _unavailable()

    last = round(c[-1], 2)
    raw = data.get("raw", c[-1])
    band = gvz_band(last)
    source = data.get("source")
    data_date = str(data.get("data_date") or data.get("data_time") or "")[:10] or None

    out: dict = {
        "available": True, "status": "OK",
        "gvz": last, "gvz_raw_value": raw, "last": last,
        "gvz_source": source, "gvz_data_date": data_date,
        "source": source, "data_time": data_date,
        "samples": n, "band": band,
        "band_name": (band or {}).get("name"),
        "band_index": (band or {}).get("index"),
    }

    def diff(k: int):
        return round(c[-1] - c[-1 - k], 2) if n > k else None

    def ret(k: int):
        if n <= k or not c[-1 - k]:
            return None
        return round((c[-1] / c[-1 - k] - 1.0) * 100.0, 2)

    # 兼容旧字段（chg_5d 等），保持既有策略口径不变
    out["chg_5d"] = diff(5)
    out["chg_10d"] = diff(10)
    out["chg_20d"] = diff(20)

    # 规范字段
    out["gvz_change_1d"] = diff(1)
    out["gvz_ret_5"] = ret(5)
    out["gvz_ret_10"] = ret(10)
    out["gvz_ret_20"] = ret(20)

    def mean(xs):
        return sum(xs) / len(xs) if xs else None

    def ma_at(idx: int, w: int):
        lo = idx - w + 1
        if lo < 0:
            return None
        return sum(c[lo:idx + 1]) / w

    gvz_ma20 = ma_at(n - 1, GVZ_MA_WINDOW)
    out["gvz_ma20"] = round(gvz_ma20, 2) if gvz_ma20 is not None else None
    out["gvz_bias20"] = (round((last - gvz_ma20) / gvz_ma20 * 100.0, 2)
                         if gvz_ma20 else None)

    # 近一年（52 周）分布
    win = c[-GVZ_YEAR_BARS:] if n > GVZ_YEAR_BARS else c[:]
    lo, hi = min(win), max(win)
    span = max(1e-9, hi - lo)
    out["gvz_52w_high"] = round(hi, 2)
    out["gvz_52w_low"] = round(lo, 2)
    out["gvz_52w_percentile"] = round((c[-1] - lo) / span * 100.0, 1)
    out["gvz_52w_basis"] = (f"近{GVZ_YEAR_BARS}个交易日（实际{len(win)}根）")

    mu = mean(win)
    try:
        sd = statistics.pstdev(win) if len(win) > 2 else 0.0
    except statistics.StatisticsError:  # noqa: PERF203
        sd = 0.0
    out["gvz_zscore"] = (round((c[-1] - mu) / sd, 2) if sd and mu is not None
                         else None)

    # 短期骤升（研究参数，非官方）
    spike = bool((out["gvz_change_1d"] is not None
                  and out["gvz_change_1d"] >= GVZ_SPIKE_CHG_1D)
                 or (out["gvz_ret_5"] is not None
                     and out["gvz_ret_5"] >= GVZ_SPIKE_RET_5))
    out["gvz_spike"] = spike
    out["gvz_spike_basis"] = (f"研究参数：单日≥{GVZ_SPIKE_CHG_1D}点 "
                              f"或 5日≥{GVZ_SPIKE_RET_5}%")

    # 趋势（MA20 的 5 日斜率，研究参数）
    slope = None
    if n >= GVZ_MA_WINDOW + 6:
        prev = ma_at(n - 6, GVZ_MA_WINDOW)
        if prev is not None and gvz_ma20 is not None:
            slope = gvz_ma20 - prev
    if slope is None:
        out["gvz_trend"] = "数据不足"
    elif slope > GVZ_TREND_SLOPE:
        out["gvz_trend"] = "上行"
    elif slope < -GVZ_TREND_SLOPE:
        out["gvz_trend"] = "下行"
    else:
        out["gvz_trend"] = "走平"

    # 兼容旧字段：全历史极值与分位
    out["hist_low"] = round(min(c), 2)
    out["hist_high"] = round(max(c), 2)
    out["percentile_hist"] = round((c[-1] - min(c)) /
                                   max(1e-9, max(c) - min(c)) * 100, 1)

    # 研究分档判定（★系统研究参数★，非官方规则）：
    #   条件②「低位（波动出清）」= 正常基准及以下（< 20）
    #   条件③ 的「低位拐头」前提 = 偏高及以下（< 30）
    out["below_20"] = bool(band and band["below_normal"])
    out["low_zone"] = bool(band and band["low_zone"])
    out["turning_up"] = bool(out["low_zone"] and out["chg_5d"] is not None
                             and out["chg_5d"] > 0)
    return out
