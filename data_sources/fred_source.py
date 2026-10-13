"""美国利率数据源：10 年期实际利率 与 10 年期国债收益率。

主源：美国财政部官方 Daily Treasury Yield Curve（真实公开 CSV，无 Key）
      * daily_treasury_real_yield_curve  → 10 年期实际利率（TIPS 口径）
      * daily_treasury_yield_curve       → 10 年期名义国债收益率
备源：FRED fredgraph.csv（DFII10 / DGS10），若网络可达则作为补充

说明：需求书中指定 FRED，但实测当前网络无法访问 fred.stlouisfed.org；
美国财政部官网提供同口径官方数据，属于更权威的真实来源，故作为主源。
取不到时显示"暂无数据"，绝不填 0 或随机值。
"""
from __future__ import annotations

import csv
import io
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime

from .http_util import DATA_CACHE, STATUS, fetch_with_cache, http_get_text

UST_CSV = (
    "https://home.treasury.gov/resource-center/data-chart-center/"
    "interest-rates/daily-treasury-rates.csv/{year}/all"
    "?type={kind}&field_tdr_date_value={year}&page&_format=csv"
)


def _parse_ust_csv(text: str, col_candidates: tuple[str, ...]) -> list[tuple[str, float]]:
    """解析财政部 CSV，返回 [(YYYY-MM-DD, value)] 升序。"""
    out: list[tuple[str, float]] = []
    reader = csv.reader(io.StringIO(text))
    header = None
    idx = -1
    for row in reader:
        if not row:
            continue
        if header is None:
            header = [h.strip() for h in row]
            for i, h in enumerate(header):
                norm = h.replace(" ", "").upper()
                if any(norm == cand.replace(" ", "").upper() for cand in col_candidates):
                    idx = i
                    break
            continue
        if idx < 0 or idx >= len(row):
            continue
        raw_date, raw_val = row[0].strip(), row[idx].strip()
        if not raw_val or raw_val.upper() in ("N/A", ""):
            continue
        try:
            dt = datetime.strptime(raw_date, "%m/%d/%Y").date()
            val = float(raw_val)
        except ValueError:
            continue
        out.append((dt.isoformat(), val))
    out.sort(key=lambda x: x[0])
    return out


def _fetch_ust(kind: str, col_candidates: tuple[str, ...], years: int,
               timeout: int, retries: int) -> dict:
    """美国财政部官方 Daily Treasury CSV。

    注意：该接口按"年"提供 CSV，取 N 年历史需要 N+1 次请求。
    逐个串行时这一步要花 20 秒以上，因此这里**并发**抓取各年份再合并。
    """
    cur = date.today().year
    req_years = list(range(cur - years, cur + 1))
    texts: dict[int, str] = {}
    err = ""

    def _one(y: int) -> tuple[int, tuple[str | None, str]]:
        return y, http_get_text(UST_CSV.format(year=y, kind=kind),
                                timeout=timeout, retries=retries)

    with ThreadPoolExecutor(max_workers=min(8, len(req_years)),
                            thread_name_prefix="ust") as pool:
        futs = [pool.submit(_one, y) for y in req_years]
        for fut in futs:
            try:
                y, (text, e) = fut.result()
            except Exception as exc:  # noqa: BLE001
                err = f"{type(exc).__name__}: {exc}"
                continue
            if not text:
                err = e
                continue
            texts[y] = text

    series: dict[str, float] = {}
    for y in sorted(texts):
        for d, v in _parse_ust_csv(texts[y], col_candidates):
            series[d] = v
    if not series:
        raise RuntimeError(f"美国财政部数据不可用：{err or '空数据'}")
    keys = sorted(series)
    return {
        "dates": keys,
        "close": [series[k] for k in keys],
        "data_time": keys[-1],
        "source": "美国财政部官方",
    }


def _fetch_ust_multi(kind: str, col_map: dict[str, tuple[str, ...]], years: int,
                     timeout: int, retries: int) -> dict:
    """同 _fetch_ust，但一次解析多列（如 2Yr 与 3Mo），返回：
    {"dates": [...], "series": {"y2": [...], "m3": [...]}, ...}
    """
    cur = date.today().year
    req_years = list(range(cur - years, cur + 1))
    texts: dict[int, str] = {}
    err = ""

    def _one(y: int) -> tuple[int, str | None]:
        text, e = http_get_text(UST_CSV.format(year=y, kind=kind),
                                timeout=timeout, retries=retries)
        return y, text

    with ThreadPoolExecutor(max_workers=min(8, len(req_years)),
                            thread_name_prefix="ust") as pool:
        futs = [pool.submit(_one, y) for y in req_years]
        for fut in futs:
            try:
                y, text = fut.result()
            except Exception as exc:  # noqa: BLE001
                err = f"{type(exc).__name__}: {exc}"
                continue
            if text:
                texts[y] = text

    if not texts:
        raise RuntimeError(f"美国财政部数据不可用：{err or '空数据'}")

    # 命中列索引：对每年每文件独立匹配（表头可能逐年微调）
    collected: dict[str, dict[str, float]] = {k: {} for k in col_map}
    for y in sorted(texts):
        reader = csv.reader(io.StringIO(texts[y]))
        header = None
        idx_map: dict[str, int] = {}
        for row in reader:
            if not row:
                continue
            if header is None:
                header = [h.strip() for h in row]
                norm = [h.replace(" ", "").replace("-", "").upper() for h in header]
                for key, cands in col_map.items():
                    for cand in cands:
                        cn = cand.replace(" ", "").replace("-", "").upper()
                        if cn in norm:
                            idx_map[key] = norm.index(cn)
                            break
                continue
            raw_date = row[0].strip() if row else ""
            if not raw_date:
                continue
            try:
                dt = datetime.strptime(raw_date, "%m/%d/%Y").date()
            except ValueError:
                continue
            for key, i in idx_map.items():
                if i >= len(row):
                    continue
                raw = row[i].strip()
                if not raw or raw.upper() == "N/A":
                    continue
                try:
                    collected[key][dt.isoformat()] = float(raw)
                except ValueError:
                    continue

    keys = sorted(set.intersection(*[set(v) for v in collected.values()])
                  if collected and all(collected.values()) else [])
    if len(keys) < 40:
        raise RuntimeError("财政部 CSV 缺少 2Yr/3Mo 列或数据过短")
    return {
        "dates": keys,
        "series": {k: [collected[k][d] for d in keys] for k in collected},
        "data_time": keys[-1],
        "source": "美国财政部官方",
    }


def _fetch_fred(series_id: str, timeout: int, retries: int) -> dict:
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
    text, e = http_get_text(url, timeout=timeout, retries=retries)
    if not text:
        raise RuntimeError(f"FRED 不可用：{e}")
    dates, vals = [], []
    for line in text.splitlines()[1:]:
        parts = line.split(",")
        if len(parts) < 2:
            continue
        d, v = parts[0].strip(), parts[1].strip()
        if v in ("", ".", "NaN"):
            continue
        try:
            vals.append(float(v))
            dates.append(d)
        except ValueError:
            continue
    if not dates:
        raise RuntimeError("FRED 返回空")
    return {"dates": dates, "close": vals, "data_time": dates[-1], "source": f"FRED {series_id}"}


class RateSource:
    """同时管理实际利率与名义收益率。"""

    name_real = "美国10年期实际利率"
    name_nominal = "美国10年期国债收益率"

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.timeout = int(cfg.get("app.timeout_seconds", 10))
        self.retries = int(cfg.get("app.max_retries", 3))
        self.years = int(cfg.get("app.history_years", 5))
        self.fred_enabled = bool(cfg.get("data_sources.fred_enabled", True))
        self.cache_ttl = float(cfg.get("data_sources.cache_ttl_minutes.macro", 1440))

    # ---------------- 10 年期实际利率 ----------------
    def fetch_real(self, use_cache_first: bool = False) -> dict | None:
        def loader() -> dict:
            errors = []
            try:
                return _fetch_ust(
                    "daily_treasury_real_yield_curve", ("10 YR", "10 Year", "10YR"),
                    self.years, self.timeout, self.retries,
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(str(exc))
            if self.fred_enabled:
                try:
                    return _fetch_fred("DFII10", self.timeout, self.retries)
                except Exception as exc:  # noqa: BLE001
                    errors.append(str(exc))
            raise RuntimeError("; ".join(errors))

        res = fetch_with_cache(
            self.name_real, "real_yield_10y", loader, DATA_CACHE,
            ttl_minutes=self.cache_ttl, use_cache_first=use_cache_first,
        )
        d = res.value if res.ok else None
        if d:
            d["from_cache"] = res.from_cache
            d["cache_age_minutes"] = res.cache_age_minutes
        return d

    # ---------------- 10 年期名义收益率 ----------------
    def fetch_nominal(self, use_cache_first: bool = False) -> dict | None:
        def loader() -> dict:
            errors = []
            try:
                return _fetch_ust(
                    "daily_treasury_yield_curve", ("10 Yr", "10 YR", "10 Year"),
                    self.years, self.timeout, self.retries,
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(str(exc))
            if self.fred_enabled:
                try:
                    return _fetch_fred("DGS10", self.timeout, self.retries)
                except Exception as exc:  # noqa: BLE001
                    errors.append(str(exc))
            try:
                from .yahoo_util import fetch_chart
                y = fetch_chart("^TNX", "5y", "1d", self.timeout, self.retries)
                y["close"] = [v / 10.0 for v in y["close"]]  # ^TNX 报价为收益率×10
                y["source"] = "Yahoo ^TNX"
                return y
            except Exception as exc:  # noqa: BLE001
                errors.append(str(exc))
            raise RuntimeError("; ".join(errors))

        res = fetch_with_cache(
            self.name_nominal, "nominal_yield_10y", loader, DATA_CACHE,
            ttl_minutes=self.cache_ttl, use_cache_first=use_cache_first,
        )
        d = res.value if res.ok else None
        if d:
            d["from_cache"] = res.from_cache
            d["cache_age_minutes"] = res.cache_age_minutes
        return d


    # ---------------- Fed 利率预期（2Y − 3M 政策预期代理） ----------------
    def fetch_policy(self, use_cache_first: bool = False) -> dict | None:
        """市场隐含政策利率预期。

        原理：2 年期国债收益率 ≈ 未来两年平均政策利率的市场定价；
        3 个月期收益率 ≈ 当前政策利率。两者之差（2Y − 3M）：
          * < 0 → 市场定价未来会降息（利多黄金）
          * > 0 → 市场定价未来仍会加息（压制黄金）
        数据来自与 10Y 收益率同一张美国财政部 CSV（零新增外部依赖）。
        """
        def loader() -> dict:
            d = _fetch_ust_multi(
                "daily_treasury_yield_curve",
                {"y2": ("2 Yr", "2YR"), "m3": ("3 Mo", "3MO")},
                2, self.timeout, self.retries)
            y2, m3 = d["series"]["y2"], d["series"]["m3"]
            d["close"] = [round(a - b, 4) for a, b in zip(y2, m3)]
            d["y2_last"], d["m3_last"] = y2[-1], m3[-1]
            d["label"] = "Fed利率预期(2Y-3M)"
            return d

        res = fetch_with_cache(
            "Fed利率预期", "policy_expectation", loader, DATA_CACHE,
            ttl_minutes=self.cache_ttl, use_cache_first=use_cache_first,
        )
        d = res.value if res.ok else None
        if d:
            d["from_cache"] = res.from_cache
            d["cache_age_minutes"] = res.cache_age_minutes
        return d


def policy_metrics(data: dict | None) -> dict:
    """政策预期差值序列（百分点）的当前值、变化与方向。"""
    if not data or not data.get("close"):
        return {"available": False, "reason": "暂无数据",
                "label": "Fed利率预期(2Y-3M)"}
    c = data["close"]
    n = len(c)
    out = {"available": True, "label": "Fed利率预期(2Y-3M)",
           "source": data.get("source"), "data_time": data.get("data_time"),
           "last": round(c[-1], 3), "y2_last": data.get("y2_last"),
           "m3_last": data.get("m3_last")}
    out["chg_5d"] = round(c[-1] - c[-6], 3) if n > 6 else None
    out["chg_20d"] = round(c[-1] - c[-21], 3) if n > 21 else None
    if out["chg_20d"] is not None:
        # spread < 0 → 市场预期降息
        out["easing"] = bool(out["chg_20d"] < -0.02)   # 降息预期正在增强
        out["direction"] = (
            "市场预期降息且预期增强" if out["last"] < 0 and out["easing"] else
            "市场预期降息" if out["last"] < -0.05 else
            "预期偏紧（仍在定价加息）" if out["last"] > 0.05 else "中性")
    else:
        out["easing"] = None
        out["direction"] = None
    return out


def rate_metrics(data: dict | None, label: str) -> dict:
    """利率序列的当前值、变化与趋势。单位：%（百分数原值）。"""
    if not data or not data.get("close"):
        return {"available": False, "reason": "暂无数据", "label": label}
    c = data["close"]
    n = len(c)
    out = {
        "available": True, "label": label, "source": data.get("source"),
        "data_time": data.get("data_time"), "last": c[-1],
    }
    out["chg_5d"] = c[-1] - c[-6] if n > 6 else None
    out["chg_20d"] = c[-1] - c[-21] if n > 21 else None
    out["chg_60d"] = c[-1] - c[-61] if n > 61 else None
    out["ma20"] = sum(c[-20:]) / 20 if n >= 20 else None
    out["ma60"] = sum(c[-60:]) / 60 if n >= 60 else None
    out["ma200"] = sum(c[-200:]) / 200 if n >= 200 else None
    if out["chg_20d"] is not None:
        out["direction"] = "下行" if out["chg_20d"] < -0.10 else (
            "上行" if out["chg_20d"] > 0.10 else "横盘")
        out["falling"] = out["chg_20d"] < -0.05
        out["rising"] = out["chg_20d"] > 0.05
    else:
        out["direction"] = None
        out["falling"] = None
        out["rising"] = None
    return out
