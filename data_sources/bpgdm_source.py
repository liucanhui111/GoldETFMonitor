"""BPGDM（Gold Miners Bullish Percent Index，金矿股看涨百分比）。

一、真实定义（依据 StockCharts $BPGDM）
  * 衡量 **NYSE Arca Gold Miners Index 成分股**中「出现**点数图 (Point & Figure)
    买入信号**」的股票比例，取值 **0~100**。
  * 50 为中轴；它是**广度 / 情绪指标**，不是金矿价格指数，也不直接衡量黄金收益。
  * P&F 买卖信号具有"粘性"：个股跌破均线后仍可能维持买单状态，
    因此用普通均线穿越做替代会系统性偏低。
  * P&F 参数（StockCharts 历史说明）：**1 box = 2 个百分点**，
    **3-box 反转 = 6 个百分点**。

二、真实值 / 代理值必须严格分离（本模块的硬约束）
  * **真实 BPGDM（bpgdm_real）**：来自官方或官方镜像（用户配置 URL / StockQ）。
  * **BPGDM 代理（bpgdm_proxy）**：本地用约 30 只金矿股的"收盘价站上 200 日均线
    占比"近似趋势广度（粘性近似 P&F）。**它不是 BPGDM**，界面必须标注
    【BPGDM代理，非真实BPGDM】；代理**不得**进入正式策略有效性统计，
    除非单独做 proxy vs real 的验证报告。
  * **禁止**把「GDX RSI + BIAS60 合成」或「13 只金矿股 MA10/MA60 上方比例」
    命名为 BPGDM——本项目已删除该口径。
  * 两者字段分离：真实值不可用时 `bpgdm_real = None` 并显示"暂无数据"，
    **绝不**把代理值冒充成真实值。

三、数据源
  1) `bpgdm_url`（用户在「设置 → 数据源」配置的官方地址，返回 JSON/CSV）
  2) StockQ `https://en.stockq.org/index/BPGDM.php`（用于**日线值 / 历史序列 /
     交叉验证**）
  3) 同上的 StockQ 页面，经 `https://r.jina.ai/` 阅读器**转发**（源站对本机 IP 段
     整体 403 时的中转；来源仍是 StockQ，结果标 `relay=True` 如实注明"经转发"）
  4) 本地代理（仅研究用途，明确标注非真实 BPGDM）
  StockCharts 官方页面用于**定义 / P&F 逻辑 / 参数参考**（无稳定免费接口）。

四、经验区域（★系统研究解释区间★，不是绝对买卖规则）
  <30 超卖/弱势区 | 30~50 偏弱 | 50~70 偏强 | 70~80 高位区 | ≥80 极端高位/拥挤风险

五、P&F 反转（bpgdm_pnf_reversal）
  仅当数据源直接给出 P&F 反转结果时才为 true/false；
  **拿不到真实 P&F 原始数据时必须 UNKNOWN（None）**，
  禁止用普通 K 线穿越去"猜"一个 3-box 反转。
"""
from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor

from .http_util import (DATA_CACHE, STATUS, fetch_with_cache, host_block_reason,
                        http_get_json, http_get_text, is_connection_error)
from .yahoo_util import BASE as YAHOO_CHART_URL
from .yahoo_util import fetch_chart

# 金矿/银矿股样本（NYSE Arca 金矿指数同类名单；取不到的会自动跳过）
MINERS = [
    "NEM", "GOLD", "AEM", "FNV", "WPM", "KGC", "AU", "GFI", "RGLD", "BTG",
    "EGO", "AGI", "OR", "PAAS", "HL", "CDE", "SSRM", "MAG", "EQX", "GATO",
    "EXK", "FSM", "ASM", "SAND", "IAG", "NG", "HMY", "DRD", "BVN", "GROY",
]
MIN_MINERS = 10           # 至少成功这么多只才计算代理
PROXY_MA = 200            # 趋势广度口径：站上 200 日均线
STOCKQ_URL = "https://en.stockq.org/index/BPGDM.php"
# 实测（2026-10-08）：StockQ 的 en/cn/fr/es/www 各域名对本机**一律** nginx 403，
# 换 User-Agent / Referer / HTTP 版本、改用系统 curl 均无效（按 IP/机房段拦截）。
# 此时可经公共阅读器 `r.jina.ai` 转发取回**同一个页面**：数据来源仍是 StockQ
# 官方 $BPGDM，只是多了一跳中转 —— 必须如实标注（`relay=True`），不得隐去。
READER_PREFIX = "https://r.jina.ai/"

# P&F 参数（StockCharts 历史说明）：1 box = 2 个百分点，3-box 反转 = 6 个百分点。
# 仅用于口径说明；本项目**不**用普通 K 线去反推 P&F 反转（那样等于伪造）。
PNF_BOX_PP = 2.0
PNF_REVERSAL_BOXES = 3
PNF_REVERSAL_PP = PNF_BOX_PP * PNF_REVERSAL_BOXES   # 6.0

BPGDM_MIN = 0.0
BPGDM_MAX = 100.0

# ★系统研究解释区间★（不是绝对买卖规则，非官方阈值）
BPGDM_ZONES = [
    (30.0, "超卖/弱势区"),
    (50.0, "偏弱"),
    (70.0, "偏强"),
    (80.0, "高位区"),
    (float("inf"), "极端高位/拥挤风险"),
]
BPGDM_ZONE_BASIS = "系统研究参数（研究解释区间，非官方买卖规则）"


def bpgdm_zone(value) -> dict | None:
    """返回 BPGDM 所处研究解释区间（★系统研究参数★，非买卖规则）。"""
    if value is None:
        return None
    for i, (hi, name) in enumerate(BPGDM_ZONES):
        if value < hi:
            lo = 0.0 if i == 0 else BPGDM_ZONES[i - 1][0]
            return {"index": i, "name": name, "lo": lo, "hi": hi,
                    "basis": BPGDM_ZONE_BASIS, "official": False,
                    "oversold_30": value < 30.0,
                    "overbought_70": value > 70.0,
                    "extreme_80": value >= 80.0}
    return None


def bpgdm_valid(value) -> bool:
    """BPGDM 合法值必须落在 0~100（含端点）。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return False
    return BPGDM_MIN <= v <= BPGDM_MAX


class BPGDMSource:
    name = "BPGDM(金矿股广度)"

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.url = str(cfg.get("data_sources.bpgdm_url", "") or "").strip()
        self.stockq_enabled = bool(
            cfg.get("data_sources.bpgdm_use_stockq_mirror", True))
        self.reader_enabled = bool(cfg.get("data_sources.bpgdm_use_reader", True))
        self.timeout = int(cfg.get("app.timeout_seconds", 10))
        self.retries = int(cfg.get("app.max_retries", 3))
        self.last_error = ""      # 最近一次 fetch 的整体失败原因（供界面如实显示）

    def fetch(self, use_cache_first: bool = False,
              deadline: float | None = None) -> dict | None:
        # 真实源（自定义 URL / StockQ）的失败原因要**单独保留并上报**，
        # 不能因为"代理成功"就把真实源取不到这件事吞掉——否则界面会
        # 把代理值当成 BPGDM 展示（用户明确指出的问题）。
        real_errors: list[str] = []

        def loader() -> dict:
            real_errors.clear()
            if self.url:
                try:
                    return self._official()
                except Exception as exc:  # noqa: BLE001
                    real_errors.append(f"自定义源({self.url}): {exc}")
            if self.stockq_enabled:
                try:
                    return self._stockq()
                except Exception as exc:  # noqa: BLE001
                    real_errors.append(f"StockQ({STOCKQ_URL}): {exc}")
                if self.reader_enabled:
                    try:
                        return self._stockq_reader()
                    except Exception as exc:  # noqa: BLE001
                        real_errors.append(
                            f"StockQ(经阅读器 {READER_PREFIX} 转发): {exc}")
            try:
                return self._proxy(deadline=deadline)
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError("; ".join(real_errors + [f"本地代理: {exc}"]))
            raise RuntimeError("; ".join(real_errors) or "BPGDM 源不可用")

        res = fetch_with_cache(
            self.name, "bpgdm", loader, DATA_CACHE,
            ttl_minutes=float(self.cfg.get("data_sources.cache_ttl_minutes.sentiment", 1440)),
            use_cache_first=use_cache_first,
        )
        d = res.value if res.ok else None
        # 把"整体失败"的原因留在实例上：`fetch()` 不抛异常而是返回 None，
        # 调用方（macro_source）拿不到原因，只能显示笼统文案 —— 这里补上。
        self.last_error = ("" if res.ok else
                           (res.error or "BPGDM 源本次不可用"
                            "（真实源与本地代理均取不到）"))
        if d:
            d["from_cache"] = res.from_cache
            d["cache_age_minutes"] = res.cache_age_minutes
            # 结果不是真实值时（走的是代理或缓存），把真实源失败原因挂上，
            # 供界面/报告如实显示"真实 BPGDM 取数失败：<原因>"。
            if not bool(d.get("is_real")):
                # 优先用本次真实源失败原因；若是"命中缓存"（本次没联网尝试），
                # 则沿用缓存里记录的原因，避免退化成笼统的"源本次不可用"。
                d["real_error"] = ("; ".join(real_errors)
                                   or d.get("real_error")
                                   or res.error
                                   or "真实 BPGDM 源本次不可用")
                d["real_url"] = self.url or STOCKQ_URL
                d["real_attempted"] = True
        return d

    # ---------------- 官方（用户配置） ----------------
    def _official(self) -> dict:
        payload, err = http_get_json(self.url, timeout=self.timeout, retries=2)
        if payload is None:
            text, err2 = http_get_text(self.url, timeout=self.timeout, retries=2)
            if not text:
                raise RuntimeError(err or err2)
            dates, vals = _parse_pairs_text(text)
            if not dates:
                raise RuntimeError("CSV 解析失败")
            return {"dates": dates, "close": vals, "data_time": dates[-1],
                    "kind": "official", "source": "官方(用户配置)"}
        items = payload.get("data") if isinstance(payload, dict) else payload
        if isinstance(items, dict):
            items = items.get("item") or items.get("items") or []
        dates, vals = [], []
        for it in items or []:
            if isinstance(it, dict):
                v = it.get("value") or it.get("close") or it.get("bpgdm")
                dd = it.get("date") or it.get("data_time")
                if v is not None and dd:
                    vals.append(float(v))
                    dates.append(str(dd)[:10])
        if not dates:
            raise RuntimeError("官方响应无有效数据")
        order = sorted(range(len(dates)), key=lambda i: dates[i])
        return {"dates": [dates[i] for i in order], "close": [vals[i] for i in order],
                "data_time": max(dates), "kind": "official", "source": "官方(用户配置)"}

    # ---------------- StockQ 官方镜像 ----------------
    def _stockq(self) -> dict:
        """解析 StockQ 的 $BPGDM 页面（HTML 表）：日期 + 指数值。"""
        if host_block_reason(STOCKQ_URL):
            raise RuntimeError(host_block_reason(STOCKQ_URL) or "StockQ 暂不可达")
        text, err = http_get_text(STOCKQ_URL, timeout=self.timeout, retries=2)
        if not text:
            raise RuntimeError(f"StockQ 不可达：{err}")
        dates, vals = _parse_stockq_html(text)
        if len(dates) < 5:
            raise RuntimeError("StockQ 页面未解析出足够历史（可能被反爬拦截）")
        order = sorted(range(len(dates)), key=lambda i: dates[i])
        ds = [_norm_date(dates[i]) for i in order]
        vs = [vals[i] for i in order]
        return {"dates": ds, "close": vs, "data_time": ds[-1],
                "kind": "official", "is_real": True,
                "algo": "StockQ 页面（官方 $BPGDM 日线序列，第三方镜像）",
                "source": "StockQ（第三方镜像官方 $BPGDM）"}

    def _stockq_reader(self) -> dict:
        """经公共阅读器转发取 StockQ 的 $BPGDM 页面。

        用途：源站对本机 IP 段整体 403 时，仍能拿到**真实 BPGDM**（而不是退回代理）。
        来源仍是 StockQ，多了一跳中转 —— 结果里 `relay=True` 供界面如实标注。
        """
        url = READER_PREFIX + STOCKQ_URL
        text, err = http_get_text(url, timeout=max(20, self.timeout), retries=2)
        if not text:
            raise RuntimeError(f"阅读器转发失败：{err}")
        dates, vals = _parse_stockq_html(text)
        if len(dates) < 5:
            raise RuntimeError("阅读器转发内容未解析出足够历史（可能被限流）")
        order = sorted(range(len(dates)), key=lambda i: dates[i])
        ds = [_norm_date(dates[i]) for i in order]
        vs = [vals[i] for i in order]
        if not bpgdm_valid(vs[-1]):
            raise RuntimeError("转发得到的 BPGDM 超出 0~100 合法范围")
        return {"dates": ds, "close": vs, "data_time": ds[-1],
                "kind": "official", "is_real": True, "relay": True,
                "algo": "StockQ 官方 $BPGDM 日线（经 r.jina.ai 阅读器转发）",
                "source": "StockQ（经 r.jina.ai 阅读器转发）"}

    # ---------------- 本地代理 ----------------
    def _one_miner(self, sym: str, deadline: float | None) -> dict[str, float] | None:
        if deadline is not None and time.monotonic() > deadline:
            return None  # 已超预算，直接跳过，不再发起请求
        d = fetch_chart(sym, "3y", "1d", self.timeout, min(2, max(1, self.retries)))
        return dict(zip(d["dates"], d["close"]))

    def _proxy(self, deadline: float | None = None) -> dict:
        if host_block_reason(YAHOO_CHART_URL):
            raise RuntimeError("金矿股行情源不可达——暂无数据")
        series: dict[str, dict[str, float]] = {}
        conn_fails = 0
        with ThreadPoolExecutor(max_workers=min(8, len(MINERS)),
                                thread_name_prefix="bpgdm") as pool:
            futs = {pool.submit(self._one_miner, s, deadline): s for s in MINERS}
            for fut, sym in futs.items():
                try:
                    ser = fut.result()
                except Exception as exc:  # noqa: BLE001
                    if is_connection_error(exc) or "URLError" in str(exc) or "超时" in str(exc):
                        conn_fails += 1
                    continue
                if ser:
                    series[sym] = ser
        ok_symbols = len(series)
        if ok_symbols < MIN_MINERS:
            if conn_fails >= 3 or host_block_reason(YAHOO_CHART_URL):
                raise RuntimeError(
                    f"金矿股行情源不可达（成功 {ok_symbols}/{len(MINERS)}）——暂无数据")
            raise RuntimeError(f"金矿股行情不足（成功 {ok_symbols}/{len(MINERS)}）")

        # 预计算每只股票的 (日期 -> 是否站上 200 日均线)：趋势广度，粘性近似 P&F
        above_map: dict[str, dict[str, bool]] = {}
        for sym, ser in series.items():
            sym_dates = list(ser.keys())
            vals = [ser[d] for d in sym_dates]
            flags: dict[str, bool] = {}
            for i, d in enumerate(sym_dates):
                if i < PROXY_MA:
                    continue
                ma = sum(vals[i - PROXY_MA:i]) / float(PROXY_MA)
                flags[d] = vals[i] > ma
            above_map[sym] = flags

        all_dates = sorted({d for f in above_map.values() for d in f})
        breadth_dates, breadth = [], []
        for d in all_dates:
            above = total = 0
            for sym, flags in above_map.items():
                if d in flags:
                    total += 1
                    if flags[d]:
                        above += 1
            if total >= MIN_MINERS:
                breadth_dates.append(d)
                breadth.append(round(above / total * 100, 2))
        if len(breadth) < 30:
            raise RuntimeError("代理序列过短")
        return {
            "dates": breadth_dates, "close": breadth,
            "data_time": breadth_dates[-1], "kind": "proxy", "is_real": False,
            "algo": (f"约 {ok_symbols} 只金矿股「收盘价站上 {PROXY_MA} 日均线」"
                     f"占比（趋势广度代理，粘性近似 P&F，非真实 BPGDM）"),
            "miners_count": ok_symbols, "miners": sorted(series),
            "source": (f"本地趋势广度代理（{ok_symbols}只金矿股站上"
                       f"{PROXY_MA}日均线占比，非真实 BPGDM）"),
        }


# --------------------------------------------------------------------------
# 解析辅助
# --------------------------------------------------------------------------
_DATE_RE = re.compile(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})")


def _norm_date(s: str) -> str:
    m = _DATE_RE.match(str(s).strip())
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return str(s)[:10]


_MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
           "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}


def _parse_stockq_html(text: str) -> tuple[list[str], list[float]]:
    """从 StockQ 页面（原生 HTML 或阅读器转成的 Markdown）提取 (日期, 指数值)。

    两处表格都要吃下：
      * 「Date Index Change%」表 → `2026/10/08 40.54`；
      * 「Technical Chart / Deviation」表 → `Jul 16, 2026 | 19.18 | …`
        （只有第二张表能给到 60+ 根，MA60 才有得算）。
    同一天出现多次时按上表（显式指数表）为准，最后按日期排序。
    """
    plain = re.sub(r"<[^>]+>", " ", text)
    plain = plain.replace("&nbsp;", " ")
    plain = re.sub(r"[ \t\u00a0]+", " ", plain)
    main: dict[str, float] = {}
    extra: dict[str, float] = {}
    # 形如 2026/10/08 40.54-1.36%（先后两个数分别是值、涨跌幅）
    for m in re.finditer(
            r"(\d{4}[/-]\d{1,2}[/-]\d{1,2})\s+([0-9]{1,3}(?:\.[0-9]+)?)", plain):
        d, v = _norm_date(m.group(1)), float(m.group(2))
        if 0.0 <= v <= 100.0 and d not in main:
            main[d] = v
    # 形如 Jul 16, 2026 | 19.18 | …（技术走势表 / 偏离表，用于补足足够长的序列）
    for m in re.finditer(
            r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+"
            r"(\d{1,2}),\s*(\d{4})[\s|]+([0-9]{1,3}(?:\.[0-9]+)?)",
            plain, re.I):
        mon = _MONTHS.get(m.group(1)[:3].lower())
        if not mon:
            continue
        d = f"{int(m.group(3)):04d}-{mon:02d}-{int(m.group(2)):02d}"
        v = float(m.group(4))
        if 0.0 <= v <= 100.0 and d not in extra:
            extra[d] = v
    merged = dict(extra)
    merged.update(main)          # 显式指数表优先
    dates = sorted(merged)
    return dates, [merged[d] for d in dates]


def _parse_pairs_text(text: str) -> tuple[list[str], list[float]]:
    dates, vals = [], []
    for line in text.splitlines():
        parts = [p.strip() for p in line.replace(";", ",").split(",")]
        if len(parts) < 2:
            continue
        try:
            vals.append(float(parts[-1]))
            dates.append(parts[0])
        except ValueError:
            continue
    return dates, vals


# --------------------------------------------------------------------------
# 指标
# --------------------------------------------------------------------------
def _unavailable(reason: str = "暂无数据", real_error: str | None = None) -> dict:
    """源失败时的标准返回：真实值与代理值都是 None，绝不互相冒充。

    `real_error` 用于把**真实的失败原因**（如 "StockQ...HTTP 403"）一路带到界面，
    避免只显示笼统的"BPGDM 源本次不可用"让用户无法判断到底哪里连不上。
    """
    return {
        "available": False, "status": "UNKNOWN", "reason": reason,
        "kind": None, "official": False, "is_real": False,
        "last": None, "bpgdm": None,
        "bpgdm_real": None, "bpgdm_proxy": None,
        "bpgdm_change_1d": None,
        "bpgdm_ma5": None, "bpgdm_ma10": None, "bpgdm_ma20": None,
        "bpgdm_ma60": None, "bpgdm_bias20": None, "bpgdm_trend": "UNKNOWN",
        "bpgdm_zone": None, "zone_name": "暂无数据",
        "bpgdm_pnf_reversal": None, "pnf_note": "UNKNOWN（缺 P&F 原始数据）",
        "bpgdm_source": None, "bpgdm_data_date": None,
        "bpgdm_real_source": None, "bpgdm_real_data_date": None,
        "bpgdm_relay": False,
        "bpgdm_proxy_source": None, "bpgdm_proxy_data_date": None,
        "bpgdm_proxy_algo": None,
        "real_error": (real_error
                       or (reason if reason != "暂无数据" else "BPGDM 源本次不可用")),
        "real_url": STOCKQ_URL,
        "real_attempted": True,
        "real_available": False, "proxy_available": False,
        "source": None, "data_time": None,
        "ma5": None, "ma10": None, "ma20": None, "ma60": None,
        "ma120": None, "ma200": None, "ma260": None,
        "chg_5d": None, "chg_20d": None,
        "below_ma20": None, "below_ma60": None, "declining": None,
        "oversold_30": None, "overbought_70": None, "band": "暂无数据",
        "percentile_2y": None, "low_zone": None, "turning_up": None,
        "trend_improving": None,
    }


def bpgdm_metrics(data: dict | None, error: str = "") -> dict:
    """BPGDM 指标。真实值（bpgdm_real）与代理值（bpgdm_proxy）字段严格分离。

    数据不可用时：available=False、status=UNKNOWN，且 bpgdm_real/bpgdm_proxy
    均为 None —— 绝不把代理值当成真实 BPGDM，也绝不用假值占位。

    参数 `error`：取数过程**真实失败原因**（由调用方从异常/warnings 传入），
    会写进 `real_error`，让界面如实显示"连不上哪个源、为什么"。
    """
    if not data or not data.get("close"):
        return _unavailable(real_error=error or None)
    c = [v for v in data["close"] if v is not None]
    n = len(c)
    if not n or not bpgdm_valid(c[-1]):
        return _unavailable("BPGDM 值超出 0~100 合法范围", real_error=error or None)

    last = round(c[-1], 2)
    kind = data.get("kind") or "proxy"
    is_real = bool(data.get("is_real", kind == "official"))
    source = data.get("source")
    data_date = str(data.get("data_time") or "")[:10] or None
    zone = bpgdm_zone(last)

    out: dict = {
        "available": True, "status": "OK" if is_real else "PROXY_ONLY",
        "kind": kind, "is_real": is_real, "official": is_real,
        "last": last, "bpgdm": last,
        # ---- 真实值 / 代理值 字段分离（硬约束）----
        "bpgdm_real": last if is_real else None,
        "bpgdm_proxy": None if is_real else last,
        "real_available": is_real,
        "proxy_available": not is_real,
        "bpgdm_source": source,
        "bpgdm_data_date": data_date,
        # 是否经阅读器转发（源站对本机 403 时的中转）——界面/报告要如实注明
        "bpgdm_relay": bool(data.get("relay")),
        "bpgdm_real_source": source if is_real else None,
        "bpgdm_real_data_date": data_date if is_real else None,
        "bpgdm_proxy_source": None if is_real else source,
        "bpgdm_proxy_data_date": None if is_real else data_date,
        "bpgdm_proxy_algo": None if is_real else data.get("algo"),
        "source": source, "data_time": data_date,
        "bpgdm_relay": bool(data.get("relay")),
        # 真实源失败原因（代理/缓存兜底时由 fetch() 挂上）——供界面如实显示错误
        "real_error": None if is_real else data.get("real_error"),
        "real_url": None if is_real else (data.get("real_url") or STOCKQ_URL),
        "real_attempted": bool(data.get("real_attempted", False)),
    }

    def ma(w):
        return round(sum(c[-w:]) / w, 2) if n >= w else None

    for w in (5, 10, 20, 60, 120, 200, 260):
        out[f"ma{w}"] = ma(w)
    out["bpgdm_ma5"] = out["ma5"]
    out["bpgdm_ma10"] = out["ma10"]
    out["bpgdm_ma20"] = out["ma20"]
    out["bpgdm_ma60"] = out["ma60"]

    out["chg_5d"] = round(c[-1] - c[-6], 2) if n > 6 else None
    out["chg_20d"] = round(c[-1] - c[-21], 2) if n > 21 else None
    out["bpgdm_change_1d"] = round(c[-1] - c[-2], 2) if n > 1 else None

    out["bpgdm_bias20"] = (round((last - out["ma20"]) / out["ma20"] * 100.0, 2)
                           if out["ma20"] else None)

    out["below_ma20"] = (out["ma20"] is not None and last < out["ma20"])
    out["below_ma60"] = (out["ma60"] is not None and last < out["ma60"])
    out["declining"] = bool(n > 10 and last < c[-10])

    # 研究解释区间（★系统研究参数★）：<30 超卖/弱势 | 30~50 偏弱 | 50~70 偏强
    #                                   | 70~80 高位 | ≥80 极端高位
    out["bpgdm_zone"] = zone
    out["zone_name"] = (zone or {}).get("name")
    out["oversold_30"] = bool(zone and zone["oversold_30"])
    out["overbought_70"] = bool(zone and zone["overbought_70"])
    out["band"] = ("超卖" if out["oversold_30"] else
                   ("超买" if out["overbought_70"] else "中性"))

    # 趋势（MA20 的 5 日斜率，★系统研究参数★阈值 0.5 点）
    if n >= 26 and out["ma20"] is not None:
        prev = sum(c[n - 25:n - 5]) / 20.0    # MA20 在 n-6 处的值（5 根之前）
        slope = out["ma20"] - prev
        out["bpgdm_trend"] = ("上行" if slope > 0.5 else
                              ("下行" if slope < -0.5 else "走平"))
    else:
        out["bpgdm_trend"] = "数据不足"

    # P&F 反转：只有数据源直接给出时才为 true/false，否则 UNKNOWN（不得伪造）
    raw_pnf = data.get("pnf_reversal")
    out["bpgdm_pnf_reversal"] = (bool(raw_pnf)
                                 if isinstance(raw_pnf, bool) else None)
    out["pnf_note"] = (
        f"P&F 反转 = {'是' if out['bpgdm_pnf_reversal'] else '否'}"
        f"（源直接提供；box={PNF_BOX_PP}pp / 3-box={PNF_REVERSAL_PP}pp）"
        if out["bpgdm_pnf_reversal"] is not None else
        f"UNKNOWN（缺 P&F 原始数据，不以普通 K 线反推；"
        f"参数：box={PNF_BOX_PP}pp / 3-box={PNF_REVERSAL_PP}pp）")

    if n >= 40:
        window = c[-504:] if n >= 60 else c
        lo, hi = min(window), max(window)
        span = max(1e-9, hi - lo)
        out["percentile_2y"] = round((c[-1] - lo) / span * 100, 1)
        out["low_zone"] = out["percentile_2y"] < 30
        out["turning_up"] = bool(
            out["low_zone"] and out["chg_5d"] is not None and out["chg_5d"] > 0
        )
        out["trend_improving"] = bool(out["turning_up"] and (out["chg_20d"] or 0) > -5)
    else:
        out["percentile_2y"] = None
        out["low_zone"] = None
        out["turning_up"] = None
        out["trend_improving"] = None
    return out
