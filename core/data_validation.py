"""数据真实性验证（验收 §13/§14/§15/§16/§34）。

产出 DATA_VALIDATION_REPORT.md：
1. 每只 ETF 双数据源交叉验证（收盘/最高/最低/20日涨跌/MA20/1年2年相对价位），
   两个源不一致时【如实列出差异】，不擅自判定谁对；
2. 每只 ETF 技术指标逐项明细（明确标注 ETF 代码/名称/数据日期）；
3. 复权价 vs 真实交易价：qt.gtimg 实时原始价 + 腾讯 bfq 日线，
   输出复权因子，交易计算必须用 raw 价；
4. 每个数据源各自的【数据日期】与【获取时间】分开显示。

诚实原则：取不到第二源就写【无法交叉验证】，绝不伪造一致。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from . import indicators
from .config import get_config
from data_sources.etf_source import ETFDataSource, _ths_to_tencent
from data_sources.http_util import http_get_json, http_get_text


# --------------------------------------------------------------------------
# 小工具：单序列指标（用于第二数据源独立重算）
# --------------------------------------------------------------------------
def _clean(xs: list) -> list[float | None]:
    out = []
    for v in xs:
        try:
            out.append(float(v) if v not in (None, "") else None)
        except (TypeError, ValueError):
            out.append(None)
    return out


def _ma(cs: list[float | None], w: int) -> float | None:
    vals = [c for c in cs[-w:] if c is not None]
    return round(sum(vals) / len(vals), 4) if len(vals) >= w else None


def _ret20(cs: list[float | None]) -> float | None:
    if len(cs) < 21 or cs[-1] is None or cs[-21] in (None, 0):
        return None
    return round((cs[-1] / cs[-21] - 1) * 100, 2)


def _relpos(cs: list[float | None], w: int) -> float | None:
    vals = [c for c in cs[-w:] if c is not None]
    if len(vals) < w or not vals:
        return None
    lo, hi = min(vals), max(vals)
    return round((vals[-1] - lo) / (hi - lo) * 100, 1) if hi > lo else 50.0


# --------------------------------------------------------------------------
# 真实（未复权）交易价
# --------------------------------------------------------------------------
def fetch_raw_price(thscode: str, timeout: int = 10) -> dict:
    """腾讯实时行情（原始未复权价）+ bfq 日线最后一根收盘。

    交易计算（份数/金额/权重）必须用这里的 raw 价，不得用前复权价。
    """
    sym = _ths_to_tencent(thscode)
    out: dict = {"available": False, "source": "腾讯 qt.gtimg.cn / bfq"}
    try:
        text, _ = http_get_text(f"https://qt.gtimg.cn/q={sym}",
                                encoding="gbk", timeout=timeout, retries=2)
        if text and "~" in text:
            f = text.split("~")
            out["raw_last"] = float(f[3]) if f[3] else None
            out["raw_prev_close"] = float(f[4]) if len(f) > 4 and f[4] else None
            out["name"] = f[1] if len(f) > 1 else ""
            out["quote_time"] = (f[30] if len(f) > 30 else "") or None
            out["available"] = out["raw_last"] is not None
    except Exception as exc:  # noqa: BLE001
        out["quote_error"] = f"{type(exc).__name__}: {exc}"
    try:
        url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
               f"?param={sym},day,,,10,")
        payload, _ = http_get_json(url, timeout=timeout, retries=2)
        node = (payload.get("data") or {}).get(sym) or {}
        rows = node.get("day") or []
        if rows:
            out["bfq_last_date"] = rows[-1][0]
            out["bfq_last_close"] = float(rows[-1][2])
            out["available"] = True
    except Exception as exc:  # noqa: BLE001
        out["bfq_error"] = f"{type(exc).__name__}: {exc}"
    return out


def fetch_raw_trade_price_cached(thscode: str, cfg, timeout: int = 6) -> float | None:
    """带磁盘缓存的真实（未复权）交易价，供 pipeline 份数换算使用。"""
    from data_sources.http_util import DATA_CACHE, fetch_with_cache

    def loader() -> dict:
        sym = _ths_to_tencent(thscode)
        url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
               f"?param={sym},day,,,5,")
        payload, err = http_get_json(url, timeout=timeout, retries=2)
        node = (payload.get("data") or {}).get(sym) or {}
        rows = node.get("day") or []
        if not rows:
            raise RuntimeError(f"bfq 不可用：{err}")
        return {"price": float(rows[-1][2]), "date": rows[-1][0]}

    res = fetch_with_cache("真实交易价", f"rawpx_{thscode}", loader, DATA_CACHE,
                           ttl_minutes=10, use_cache_first=False)
    return res.value.get("price") if res.ok and res.value else None


# --------------------------------------------------------------------------
# 主入口
# --------------------------------------------------------------------------
def run_data_validation(cfg=None, progress=None) -> dict:
    cfg = cfg or get_config()

    def step(msg):
        if progress:
            progress(msg)

    ds = ETFDataSource(cfg)
    rows: list[dict] = []
    for asset in cfg.enabled_assets():
        aid = asset.get("id")
        name = asset.get("name") or aid
        thscode = asset.get("thscode") or ""
        step(f"验证 {name}（{thscode}）…")
        row: dict = {"id": aid, "name": name, "thscode": thscode}

        # ---- 主源（走正常缓存逻辑，即程序实际使用的数据） ----
        try:
            primary = ds.fetch(asset, use_cache_first=True)
        except Exception as exc:  # noqa: BLE001
            primary = None
            row["primary_error"] = str(exc)
        if not primary:
            row["status"] = "失败：主源无数据"
            rows.append(row)
            continue
        row["primary_source"] = primary.get("source")
        row["primary_data_time"] = primary.get("data_time")
        row["primary_bars"] = len(primary.get("dates") or [])

        # ---- 指标明细（从主源原始 K 线重算，§15）----
        # 【同一口径纪律】这里的每个值与下面交叉验证表的"主源值"来自
        # 同一次 compute_all 计算，禁止出现"明细是 None 而交叉验证又有值"的矛盾。
        ind = indicators.compute_all(
            primary["dates"], primary.get("open"), primary.get("high"),
            primary.get("low"), primary["close"], cfg,
            asset.get("asset_class", "stock"),
        )
        i = ind["n"] - 1
        dd1y = (ind.get("dd_windows") or {}).get("1y") or {}
        dd2y = (ind.get("dd_windows") or {}).get("2y") or {}
        rp = ind.get("relpos") or {}
        macd = ind.get("macd") or {}
        swing = ind.get("swing") or {}
        def _r4(v):
            v = _last(v) if isinstance(v, list) else v
            return None if v is None else round(float(v), 4)

        row["indicators"] = {
            "date": ind["dates"][-1],
            "close": _r4(ind["close"][-1]),
            "ma5": _r4(ind["ma"].get("ma5")),
            "ma10": _r4(ind["ma"].get("ma10")),
            "ma20": _r4(ind["ma"].get("ma20")),
            "ma30": _r4(ind["ma"].get("ma30")),
            "ma50": _r4(ind["ma"].get("ma50")),
            "ma60": _r4(ind["ma"].get("ma60")),
            "ma120": _r4(ind["ma"].get("ma120")),
            "ma200": _r4(ind["ma"].get("ma200")),
            "macd_dif": _r4(macd.get("dif")),
            "macd_dea": _r4(macd.get("dea")),
            "macd_hist": _r4(macd.get("hist")),
            "rsi14": _r4(ind.get("rsi14")),
            "atr14": _r4(ind.get("atr14")),
            "wr14": _r4(ind.get("wr14")),
            "bias20": _r4(ind.get("bias20")),
            "ret20": _r4(ind.get("ret20")),
            "relpos1y": _r4(_num(rp.get("1y"))),
            "relpos2y": _r4(_num(rp.get("2y"))),
            "dd1y_now": _r4(dd1y.get("current_drawdown_pct")),
            "dd1y_max": _r4(dd1y.get("max_drawdown_pct")),
            "dd2y_now": _r4(dd2y.get("current_drawdown_pct")),
            "dd2y_max": _r4(dd2y.get("max_drawdown_pct")),
            "swing_state": swing.get("state"),
            "swing_detail": swing.get("detail"),
        }

        # ---- 第二数据源交叉验证（§14） ----
        second = None
        sec_name = ""
        if primary.get("source") != "腾讯证券":
            sec_name = "腾讯证券"
            try:
                second = ds.tencent.history(thscode, int(cfg.get("app.history_years", 5)))
            except Exception as exc:  # noqa: BLE001
                row["second_error"] = f"腾讯: {exc}"
        if second is None and primary.get("source") != "新浪财经":
            sec_name = sec_name or "新浪财经"
            try:
                second = ds.sina.history(thscode, 2)
            except Exception as exc:  # noqa: BLE001
                row["second_error"] = (row.get("second_error") or "") + f" 新浪: {exc}"
        row["second_source"] = sec_name or None
        if second and second.get("dates"):
            row["second_data_time"] = second.get("data_time")
            # 传入主源 ind：当共同交易日就是主源最新交易日时，
            # 交叉验证的"主源值"直接复用【指标明细】的同一次计算，保证完全一致。
            row["cross"] = _cross_check(primary, second, cfg, ind_main=ind)
        else:
            row["cross"] = {"available": False,
                            "message": "第二数据源不可用，无法交叉验证"}

        # ---- 复权价 vs 真实交易价（§16/§17） ----
        rawp = fetch_raw_price(thscode, timeout=int(cfg.get("app.timeout_seconds", 10)))
        row["raw"] = rawp
        ac = row["indicators"]["close"]
        rc = rawp.get("bfq_last_close")
        if ac and rc:
            row["raw"]["adjust_factor"] = round(rc / ac, 4)
        rows.append(row)

    ok_all = True
    for r in rows:
        cross = r.get("cross") or {}
        if not cross.get("available"):
            r["verdict"] = "未验证（缺第二数据源）"
            ok_all = False
        elif cross.get("n_diff"):
            r["verdict"] = f"差异（{cross['n_diff']} 项不一致）"
            ok_all = False
        elif cross.get("n_unknown"):
            r["verdict"] = (f"部分验证（{cross['n_ok']}/{cross['n_total']} 一致，"
                            f"{cross['n_unknown']} 项未验证）")
            ok_all = False
        else:
            r["verdict"] = f"通过（{cross.get('n_total')} 项全部一致）"
    return {"ok": True, "ts": datetime.now(), "rows": rows, "all_ok": ok_all}


def _cross_check(a: dict, b: dict, cfg, ind_main: dict | None = None) -> dict:
    """按共同交易日对齐，两源各自从原始 OHLC 重算全套指标后逐项比较（P0-2）。

    每项输出：主源值 / 对比源值 / 绝对误差 / 相对误差 / 结论（一致|差异|未验证）。
    对比源历史不足（如新浪只有 2 年）导致某项算不出 → 该项【未验证】，不算通过。

    【同一口径纪律】主源指标优先复用 run_data_validation 里计算【指标明细】用的
    同一次 compute_all 结果（ind_main）；只有当共同交易日早于主源最新交易日
    （需要截断）时才重新计算截断版，并在结果里注明。
    """
    bd = {d: i for i, d in enumerate(b["dates"])}
    common = None
    for i in range(len(a["dates"]) - 1, -1, -1):
        j = bd.get(a["dates"][i])
        if j is not None:
            common = (a["dates"][i], i, j)
            break
    if not common:
        return {"available": False, "message": "两源无共同交易日"}
    date, i, j = common

    # 截到共同交易日（compute_all 严格因果，截断后取末位即当日值）
    a_cut = {k: (v[:i + 1] if isinstance(v, list) else v)
             for k, v in a.items() if k in ("dates", "open", "high", "low", "close")}
    b_cut = {k: (v[:j + 1] if isinstance(v, list) else v)
             for k, v in b.items() if k in ("dates", "open", "high", "low", "close")}
    same_day_as_detail = (ind_main is not None and i == len(a["dates"]) - 1)
    if same_day_as_detail:
        # 与【指标明细】同一口径：完全复用同一次计算结果
        ind_a = ind_main
    else:
        try:
            ind_a = indicators.compute_all(a_cut["dates"], a_cut.get("open"),
                                           a_cut.get("high"), a_cut.get("low"),
                                           a_cut["close"], cfg, "stock")
        except Exception as exc:  # noqa: BLE001
            return {"available": False,
                    "message": f"主源指标重算失败：{type(exc).__name__}: {exc}"}
    try:
        ind_b = indicators.compute_all(b_cut["dates"], b_cut.get("open"),
                                       b_cut.get("high"), b_cut.get("low"),
                                       b_cut["close"], cfg, "stock")
    except Exception as exc:  # noqa: BLE001
        return {"available": False,
                "message": f"对比源指标重算失败：{type(exc).__name__}: {exc}"}

    def val(ind: dict, path: str):
        cur = ind
        for p in path.split("."):
            if not isinstance(cur, dict):
                return None
            cur = cur.get(p) if p not in cur and isinstance(cur.get(p), dict) \
                else cur.get(p)
            if cur is None:
                return None
        return cur[-1] if isinstance(cur, list) else cur

    dd_a1 = (ind_a.get("dd_windows") or {}).get("1y") or {}
    dd_a2 = (ind_a.get("dd_windows") or {}).get("2y") or {}
    dd_b1 = (ind_b.get("dd_windows") or {}).get("1y") or {}
    dd_b2 = (ind_b.get("dd_windows") or {}).get("2y") or {}
    def _num(x):
        """relpos 可能是标量（最终值）或序列；统一取最新数值。"""
        if x is None:
            return None
        if isinstance(x, (int, float)):
            return float(x)
        return _last(x)

    rp_a = ind_a.get("relpos") or {}
    rp_b = ind_b.get("relpos") or {}

    # (显示名, 主源取值, 对比源取值, 容差类型, 容差)
    # kind: "rel"=相对误差%（价格类）；"abs"=绝对误差（百分比类）
    specs = [
        ("收盘", ind_a["close"][-1], ind_b["close"][-1], "rel", 0.30),
        ("MA5", _last(ind_a["ma"].get("ma5")), _last(ind_b["ma"].get("ma5")), "rel", 0.50),
        ("MA10", _last(ind_a["ma"].get("ma10")), _last(ind_b["ma"].get("ma10")), "rel", 0.50),
        ("MA20", _last(ind_a["ma"].get("ma20")), _last(ind_b["ma"].get("ma20")), "rel", 0.50),
        ("MA30", _last(ind_a["ma"].get("ma30")), _last(ind_b["ma"].get("ma30")), "rel", 0.50),
        ("MA50", _last(ind_a["ma"].get("ma50")), _last(ind_b["ma"].get("ma50")), "rel", 0.50),
        ("MA60", _last(ind_a["ma"].get("ma60")), _last(ind_b["ma"].get("ma60")), "rel", 0.50),
        ("MA120", _last(ind_a["ma"].get("ma120")), _last(ind_b["ma"].get("ma120")), "rel", 0.50),
        ("MA200", _last(ind_a["ma"].get("ma200")), _last(ind_b["ma"].get("ma200")), "rel", 0.50),
        ("MACD_DIF", _last((ind_a.get("macd") or {}).get("dif")),
         _last((ind_b.get("macd") or {}).get("dif")), "rel_floor", 0.50),
        ("MACD_DEA", _last((ind_a.get("macd") or {}).get("dea")),
         _last((ind_b.get("macd") or {}).get("dea")), "rel_floor", 0.50),
        ("RSI14", _last(ind_a.get("rsi14")), _last(ind_b.get("rsi14")), "abs", 3.0),
        ("ATR14", _last(ind_a.get("atr14")), _last(ind_b.get("atr14")), "rel", 5.0),
        ("WR14", _last(ind_a.get("wr14")), _last(ind_b.get("wr14")), "abs", 3.0),
        ("BIAS20", _last(ind_a.get("bias20")), _last(ind_b.get("bias20")), "abs", 0.50),
        ("20日涨跌%", _last(ind_a.get("ret20")), _last(ind_b.get("ret20")), "abs", 1.0),
        ("1年相对价位", _num(rp_a.get("1y")), _num(rp_b.get("1y")), "abs", 3.0),
        ("2年相对价位", _num(rp_a.get("2y")), _num(rp_b.get("2y")), "abs", 3.0),
        ("1年最大回撤", dd_a1.get("max_drawdown_pct"),
         dd_b1.get("max_drawdown_pct"), "abs", 2.0),
        ("2年最大回撤", dd_a2.get("max_drawdown_pct"),
         dd_b2.get("max_drawdown_pct"), "abs", 2.0),
    ]

    items: list[dict] = []
    n_ok = n_diff = n_unknown = 0
    for label, av, bv, kind, tol in specs:
        item: dict = {"item": label, "a": av, "b": bv,
                      "abs_err": None, "rel_err_pct": None,
                      "tol": tol, "kind": kind}
        if av is None or bv is None:
            item["verdict"] = "未验证"
            n_unknown += 1
        else:
            abs_err = abs(float(av) - float(bv))
            denom = max(abs(float(bv)), 0.01) if kind == "rel_floor" \
                else abs(float(bv))
            rel = abs_err / denom * 100.0 if denom else None
            item["abs_err"] = round(abs_err, 4)
            item["rel_err_pct"] = round(rel, 2) if rel is not None else None
            ok = (rel <= tol) if kind in ("rel", "rel_floor") else (abs_err <= tol)
            item["verdict"] = "一致" if ok else "差异"
            if ok:
                n_ok += 1
            else:
                n_diff += 1
        items.append(item)

    max_close_diff = 0.0
    c_a, c_b = ind_a["close"][-1], ind_b["close"][-1]
    if c_a and c_b:
        max_close_diff = round(abs(c_a / c_b - 1) * 100, 2)
    return {"available": True, "common_date": date, "items": items,
            "n_ok": n_ok, "n_diff": n_diff, "n_unknown": n_unknown,
            "n_total": len(items),
            "same_day_as_detail": bool(same_day_as_detail),
            "max_price_diff_pct": max_close_diff,
            "second_bars": len(b["dates"])}


def _last(seq):
    try:
        return seq[-1] if seq else None
    except (TypeError, IndexError):
        return None


def _num(x):
    """relpos 可能是标量或序列；统一取最新数值（模块级，供指标明细与交叉验证共用）。"""
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    return _last(x)


# --------------------------------------------------------------------------
# 报告渲染
# --------------------------------------------------------------------------
def render_data_md(res: dict) -> str:
    L: list[str] = []
    L.append("# 数据真实性验证报告（DATA_VALIDATION_REPORT）")
    L.append("")
    L.append(f"生成时间：{res['ts']:%Y-%m-%d %H:%M:%S}")
    L.append("")
    L.append("> 每只 ETF：双数据源交叉验证 + 从原始 K 线重算的指标明细 +"
             " 复权价与真实交易价分离。两个源不一致时如实列出差异，不擅自判对错。"
             " 对比源历史不足的指标按【未验证】处理，不计入通过。")
    L.append("")
    L.append("## 汇总")
    L.append("")
    L.append("ETF | 判定")
    L.append("--- | ---")
    for r in res["rows"]:
        L.append(f"{r.get('thscode')} {r.get('name')} | {r.get('verdict', '—')}")
    L.append("")
    if res.get("all_ok"):
        L.append("**总体：全部 ETF 关键指标双源一致。**")
    else:
        L.append("**总体：存在未验证或差异项，不能声称 100% 通过——明细见下。**")
    L.append("")
    for r in res["rows"]:
        L.append("=" * 46)
        L.append(f"{r.get('thscode')} {r.get('name')}")
        L.append("=" * 46)
        L.append(f"主源：{r.get('primary_source')}（数据日期 "
                 f"{r.get('primary_data_time')}，{r.get('primary_bars')} 根日线）")
        ind = r.get("indicators") or {}
        raw = r.get("raw") or {}
        if ind:
            L.append("")
            L.append(f"【指标明细】（由主源原始 OHLC 重算；"
                     f"与下方交叉验证表的主源值来自同一次计算，口径完全一致）")
            L.append(f"  最新交易日：{ind.get('date')}")
            L.append(f"  最新收盘价（前复权，分析用）：{ind.get('close')}"
                     f"   最新收盘价（真实未复权，交易用）：{raw.get('bfq_last_close')}"
                     f"{'（最新交易日 ' + str(raw.get('bfq_last_date')) + '）' if raw.get('bfq_last_date') else ''}")
            L.append(f"  MA5 {ind.get('ma5')} / MA10 {ind.get('ma10')} / "
                     f"MA20 {ind.get('ma20')} / MA30 {ind.get('ma30')}")
            L.append(f"  MA50 {ind.get('ma50')} / MA60 {ind.get('ma60')} / "
                     f"MA120 {ind.get('ma120')} / MA200 {ind.get('ma200')}")
            L.append(f"  MACD：DIF {ind.get('macd_dif')} / DEA {ind.get('macd_dea')}"
                     f" / HIST {ind.get('macd_hist')}")
            L.append(f"  RSI14：{ind.get('rsi14')}   ATR14：{ind.get('atr14')}   "
                     f"WR14：{ind.get('wr14')}   BIAS20：{ind.get('bias20')}")
            L.append(f"  20日涨跌：{ind.get('ret20')}%")
            L.append(f"  1年相对价位：{ind.get('relpos1y')}   "
                     f"2年相对价位：{ind.get('relpos2y')}")
            L.append(f"  1年回撤：当前 {ind.get('dd1y_now')}% / 最大 {ind.get('dd1y_max')}%   "
                     f"2年回撤：当前 {ind.get('dd2y_now')}% / 最大 {ind.get('dd2y_max')}%")
            L.append(f"  波段状态：{ind.get('swing_state') or '未知'}"
                     f"（{ind.get('swing_detail') or ''}）")
            _cr_early = r.get("cross") or {}
            if _cr_early.get("available") and _cr_early.get("same_day_as_detail") is False:
                L.append("  ⚠️ 交叉验证共同交易日早于主源最新交易日，"
                         "下表主源值为截断后重算结果，与上面明细可能相差一个交易日。")
        cross = r.get("cross") or {}
        L.append("")
        if cross.get("available"):
            L.append(f"【逐指标交叉验证】对比源：{r.get('second_source')}"
                     f"（数据日期 {r.get('second_data_time')}，"
                     f"共同交易日 {cross.get('common_date')}，"
                     f"对比源 {cross.get('second_bars')} 根日线）")
            L.append("")
            L.append("  指标 | 主源值 | 对比源值 | 绝对误差 | 相对误差% | 容差 | 结论")
            L.append("  --- | --- | --- | --- | --- | --- | ---")
            for it in cross.get("items", []):
                fmt = lambda v: ("—" if v is None else
                                 (f"{v:.4f}" if isinstance(v, float) and abs(v) < 1000
                                  else str(v)))
                L.append(f"  {it['item']} | {fmt(it.get('a'))} | {fmt(it.get('b'))}"
                         f" | {it.get('abs_err') if it.get('abs_err') is not None else '—'}"
                         f" | {it.get('rel_err_pct') if it.get('rel_err_pct') is not None else '—'}"
                         f" | {it.get('tol')}{('%' if it.get('kind') in ('rel', 'rel_floor') else '')}"
                         f" | {it.get('verdict')}")
            L.append("")
            n_ok, n_diff, n_unk = (cross.get("n_ok", 0), cross.get("n_diff", 0),
                                   cross.get("n_unknown", 0))
            tot = cross.get("n_total", 0)
            if n_diff:
                verdict = f"存在 {n_diff} 项差异——两个源不一致，相关项请人工复核后再用"
            elif n_unk:
                verdict = f"一致 {n_ok}/{tot} 项，另有 {n_unk} 项未验证（对比源历史不足）——不能算全部通过"
            else:
                verdict = f"全部 {tot} 项一致"
            L.append(f"  逐项结论：一致 {n_ok} / 差异 {n_diff} / 未验证 {n_unk}"
                     f"（共 {tot} 项）→ {verdict}")
        else:
            L.append(f"【逐指标交叉验证】不可用：{cross.get('message')}"
                     f"{'；' + r.get('second_error') if r.get('second_error') else ''}"
                     "——以下指标全部按【未验证】处理，不伪造一致。")
            L.append("")
            L.append("  结论：该 ETF 全部技术指标【未验证】（缺第二数据源），"
                     "相关验证项在 TEST_REPORT 中不得计为通过。")
        L.append("")
        L.append("【最新收盘价 vs 实时价（严格分开）】")
        if raw.get("available"):
            L.append(f"  最新收盘价（前复权，分析用）：{ind.get('close')}"
                     f"（最新交易日 {ind.get('date')}）")
            L.append(f"  最新收盘价（真实未复权，交易用）：{raw.get('bfq_last_close')}"
                     f"（最新交易日 {raw.get('bfq_last_date')}）")
            if raw.get("raw_last") is not None:
                L.append(f"  实时价：{raw.get('raw_last')}"
                         f"（实时更新时间：{raw.get('quote_time') or '未知'}；"
                         "行情快照，非收盘价）")
            else:
                L.append("  实时价：暂无数据（当前行情快照不可用，"
                         "上两项均为最新交易日收盘口径）")
            if raw.get("adjust_factor") is not None:
                L.append(f"  复权因子：{raw.get('adjust_factor')}"
                         "（交易金额/份数必须用真实未复权价换算）")
        else:
            L.append("  原始价获取失败：" + str(raw.get("quote_error")
                                            or raw.get("bfq_error") or "未知")
                     + "——交易份数换算将退化为前复权价，请注意核对。")
        L.append("")
    return "\n".join(L)


def run_and_write(cfg=None, progress=None) -> tuple[dict, Path]:
    from .config import project_root
    res = run_data_validation(cfg, progress)
    # 注意：必须用 project_root()（兼容 PyInstaller：打包后 = exe 所在目录）；
    # 用 Path(__file__).parent.parent 在冻结态会落到 _internal/ 里。
    out = project_root() / "DATA_VALIDATION_REPORT.md"
    out.write_text(render_data_md(res), encoding="utf-8")
    return res, out
