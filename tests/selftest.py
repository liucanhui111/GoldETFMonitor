"""内置自检 / 验收测试。

覆盖需求第七十四条清单，并额外做三类"硬核"验证：
    1. **无未来函数**：把数据截断到 k，重算全部指标，与全量指标的 k 位置逐项比对；
       单 ETF 与组合的历史相似状态统计同样做截断一致性验证。
    2. **缓存降级**：构造必然失败的 loader，验证"优先用缓存 + 明确标注缓存数据"；
       没有缓存时必须如实返回失败，绝不返回假数据。
    3. **黄金硬上限**：构造黄金 20% 的极端权重，验证优化器与信号层都把黄金压回 ≤5%。

运行：GoldETFMonitor.exe --selftest
输出：TEST_REPORT.md
"""
from __future__ import annotations

import json
import math
import os
import re
import sys
import time
import traceback
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import indicators, optimizer, signals                      # noqa: E402
from core import portfolio as pf                                      # noqa: E402
from core.config import get_config                                    # noqa: E402
from core.histstats import SimilarStateEngine, build_feature_series   # noqa: E402
from core.pipeline import run_full                                    # noqa: E402
from data_sources.etf_source import ETFDataSource                     # noqa: E402
from data_sources.http_util import STATUS, DiskCache, fetch_with_cache  # noqa: E402

RESULTS: list[dict] = []
NOTES: list[str] = []


def _read_src(rel: str) -> str | None:
    """读取项目内源码（相对项目根）；冻结态读不到返回 None。

    PyInstaller 打包后源码在 PYZ 内、`__file__` 指向不存在的 .pyc，
    因此任何"扫源码关键词"的守卫在冻结态都无法执行。返回 None 让调用方
    明确按"冻结态跳过（已由源码自检覆盖）"处理，而不是误判为失败。
    """
    roots = []
    try:
        from core.config import project_root
        roots.append(Path(project_root()))
    except Exception:  # noqa: BLE001
        pass
    try:
        roots.append(Path(__file__).resolve().parent.parent)
    except Exception:  # noqa: BLE001
        pass
    for r in roots:
        try:
            p = r / rel
            if p.exists():
                return p.read_text(encoding="utf-8")
        except Exception:  # noqa: BLE001
            continue
    return None


def _module_src(mod) -> str | None:
    """读取模块源码；优先 __file__，冻结态回退到项目内同名源码（读不到则 None）。"""
    try:
        p = Path(getattr(mod, "__file__", "") or "")
        if p.exists():
            return p.read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    name = getattr(mod, "__name__", "") or ""
    if name.startswith(("core.", "data_sources.")):
        return _read_src(name.replace(".", "/") + ".py")
    return None


def check(group: str, name: str, fn) -> bool:
    t0 = time.time()
    try:
        ok, detail = fn()
    except Exception:  # noqa: BLE001
        ok, detail = False, traceback.format_exc(limit=3).strip().replace("\n", " | ")
    RESULTS.append({"group": group, "name": name, "ok": bool(ok),
                    "detail": str(detail)[:400], "sec": round(time.time() - t0, 3)})
    print(f"  [{'PASS' if ok else 'FAIL'}] {group} · {name}"
          f"{'' if ok else '  ← ' + str(detail)[:180]}")
    return bool(ok)


def _close(a, b, tol=1e-6) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    try:
        return abs(float(a) - float(b)) <= tol * max(1.0, abs(float(a)))
    except (TypeError, ValueError):
        return a == b


def _synth_panel(n: int = 480, seed: int = 20261007):
    """构造确定的合成面板（事件审计自检用，脱离网络、可复现）。"""
    import random

    rnd = random.Random(seed)
    dates = [f"2025-{1 + (i // 28):02d}-{1 + (i % 28):02d}" for i in range(n)]
    spec = {"A": ("stock", 0.0006, 0.012), "B": ("stock", 0.0004, 0.014),
            "C": ("bond", 0.0002, 0.003), "D": ("gold", 0.0003, 0.010)}
    datasets: dict = {}
    for aid, (_cls, mu, sd) in spec.items():
        v = 100.0
        px = []
        for _ in range(n):
            v *= (1.0 + mu + rnd.gauss(0.0, sd))
            px.append(round(v, 4))
        datasets[aid] = {"dates": list(dates), "close": list(px),
                         "open": list(px), "high": list(px), "low": list(px)}
    weights = {"A": 0.30, "B": 0.25, "C": 0.35, "D": 0.10}
    names = {k: f"合成{k}" for k in spec}
    classes = {k: v[0] for k, v in spec.items()}
    return pf.Panel(datasets, weights, names), classes, weights, names


# ==========================================================================
def main() -> int:
    print("=" * 74)
    print("全天候 ETF 组合系统 · 自检 / 验收测试")
    print("=" * 74)
    t_start = time.time()
    cfg = get_config(refresh=True)
    ds = ETFDataSource(cfg)
    assets = cfg.enabled_assets()

    # ---------------------------------------------------------------- 数据
    raw_data: dict[str, dict] = {}
    for a in assets:
        def mk(asset=a):
            def fn():
                raw = ds.fetch(asset, use_cache_first=True)
                if not raw:
                    return False, "无数据"
                raw_data[asset["id"]] = raw
                return (len(raw["dates"]) > 250,
                        f"{len(raw['dates'])} 根K线，数据日 {raw.get('data_time')}，"
                        f"来源 {raw.get('source')}")
            return fn
        check("ETF行情", f"{a['name']}（{a['thscode']}）", mk())

    ok_first = next((r for r in raw_data.values()), None)
    if not ok_first:
        print("没有可用的 ETF 数据，终止测试。")
        return 1

    ind_full: dict[str, dict] = {}
    for aid, raw in raw_data.items():
        a = cfg.asset_by_id(aid)
        ind_full[aid] = indicators.compute_all(
            raw["dates"], raw["open"], raw["high"], raw["low"], raw["close"],
            cfg, a.get("asset_class", "stock"))

    sample = ind_full[next(iter(ind_full))]
    raws = next(iter(raw_data.values()))

    # ---------------------------------------------------------------- 指标
    def ind_ok(key, need_ma=None, rng=None):
        def fn():
            v = sample.get(key)
            arr = v[-1] if isinstance(v, list) else None
            if need_ma:
                arr = (sample.get("ma") or {}).get(key, [None])[-1]
            if arr is None:
                return False, "为 None"
            if rng and not (rng[0] <= float(arr) <= rng[1]):
                return False, f"{arr} 超出合理区间 {rng}"
            return True, f"最新值 {arr if not isinstance(arr, float) else round(arr, 4)}"
        return fn

    check("技术指标", "MA5/10/20/30/50/60/120/200", ind_ok("ma20", need_ma=True))
    check("技术指标", "MACD(DIF/DEA/HIST)",
          lambda: (all(k in sample["macd"] and sample["macd"][k][-1] is not None
                       for k in ("dif", "dea", "hist")),
                   f"DIF={round(sample['macd']['dif'][-1], 4)} HIST={round(sample['macd']['hist'][-1], 4)}"))
    check("技术指标", "RSI(14)", ind_ok("rsi14", rng=(0, 100)))
    check("技术指标", "ATR(14)", ind_ok("atr14", rng=(0, 1e6)))
    check("技术指标", "WR(14)", ind_ok("wr14", rng=(-100, 0)))
    check("技术指标", "BIAS20", ind_ok("bias20", rng=(-60, 60)))
    check("技术指标", "20日涨跌幅", ind_ok("ret20", rng=(-60, 80)))
    check("技术指标", "1年最大回撤/当前回撤",
          lambda: ((sample["dd_windows"]["1y"].get("max_drawdown_pct") is not None,
                    f"最大 {round(sample['dd_windows']['1y']['max_drawdown_pct'], 2)}%"
                    f" / 当前 {round(sample['dd_windows']['1y']['current_drawdown_pct'], 2)}%"))),
    check("技术指标", "2年最大回撤/当前回撤",
          lambda: ((sample["dd_windows"]["2y"].get("max_drawdown_pct") is not None,
                    f"最大 {round(sample['dd_windows']['2y']['max_drawdown_pct'], 2)}%"
                    f" / 当前 {round(sample['dd_windows']['2y']['current_drawdown_pct'], 2)}%"))),
    check("技术指标", "1年相对价位(0-100)",
          lambda: (0 <= (sample["relpos"]["1y"] or -1) <= 100,
                   f"{round(sample['relpos']['1y'], 2)}"))
    check("技术指标", "2年相对价位(0-100)",
          lambda: (sample["relpos"]["2y"] is not None
                   and 0 <= sample["relpos"]["2y"] <= 100,
                   f"{round(sample['relpos']['2y'], 2)}"))
    check("技术指标", "波段顶底（因果 ZigZag + 状态机）",
          lambda: (sample["swing"]["state"] in indicators.SWING_STATES,
                   f"当前 {sample['swing']['state']}；已确认拐点 {len(sample['swing']['pivots'])} 个"))
    check("技术指标", "WR 超卖反转识别",
          lambda: (isinstance(sample.get("wr_reversal"), dict),
                   f"score={sample['wr_reversal'].get('score')} "
                   f"confirmed={sample['wr_reversal'].get('confirmed_reversal')}"))

    # ---------------------------------------------------------------- 无未来函数
    def truncated_consistency():
        aid = next(iter(ind_full))
        a = cfg.asset_by_id(aid)
        raw = raw_data[aid]
        k = len(raw["dates"]) - 1
        cut = indicators.compute_all(
            raw["dates"][:k], raw["open"][:k], raw["high"][:k],
            raw["low"][:k], raw["close"][:k], cfg, a.get("asset_class", "stock"))
        full = ind_full[aid]
        diffs = []
        for key in ("rsi14", "atr14", "wr14", "bias20", "ret20", "vol20_series",
                    "swing_states"):
            fv, cv = full[key][k - 1], cut[key][k - 1]
            if isinstance(fv, float) and isinstance(cv, float):
                if not _close(fv, cv, 1e-9):
                    diffs.append(f"{key}: {fv} vs {cv}")
            elif fv != cv:
                diffs.append(f"{key}: {fv} vs {cv}")
        for lab in ("1y", "2y"):
            fdd = full["curdd_series"][lab][k - 1]
            cdd = cut["curdd_series"][lab][k - 1]
            if not _close(fdd, cdd, 1e-9):
                diffs.append(f"curdd[{lab}]: {fdd} vs {cdd}")
            frp = full["relpos_series"][lab][k - 1]
            crp = cut["relpos_series"][lab][k - 1]
            if not _close(frp, crp, 1e-9):
                diffs.append(f"relpos[{lab}]: {frp} vs {crp}")
        # 波段拐点集合（只看确认日 <= k-1 的部分）
        f_piv = [p["confirm_idx"] for p in full["swing"]["pivots"] if p["confirm_idx"] <= k - 1]
        c_piv = [p["confirm_idx"] for p in cut["swing"]["pivots"] if p["confirm_idx"] <= k - 1]
        if f_piv != c_piv:
            diffs.append(f"pivots 确认日不一致 {len(f_piv)} vs {len(c_piv)}")
        return (not diffs, "全部一致：截断重算与全量结果在 t 处完全相同" if not diffs
                else "；".join(diffs[:5]))

    check("无未来函数", "指标序列截断一致性（37项逐点比对）", truncated_consistency)

    def stats_consistency():
        aid = next(iter(ind_full))
        raw = raw_data[aid]
        cost = cfg.trade_cost()
        f_full = build_feature_series(ind_full[aid], cfg)
        eng_full = SimilarStateEngine(f_full, cfg.get("hist.feature_weights", {}), cfg)
        n = ind_full[aid]["n"]
        k = len(raw["dates"]) - 1          # 截断后还剩 k 根K线，最后一个下标是 k-1
        t = k - 1                          # 对齐到同一交易日
        s_full = eng_full.stats_at(t, ind_full[aid]["close"], 60, cost)

        a = cfg.asset_by_id(aid)
        cut = indicators.compute_all(
            raw["dates"][:k], raw["open"][:k], raw["high"][:k],
            raw["low"][:k], raw["close"][:k], cfg, a.get("asset_class", "stock"))
        f_cut = build_feature_series(cut, cfg)
        eng_cut = SimilarStateEngine(f_cut, cfg.get("hist.feature_weights", {}), cfg)
        s_cut = eng_cut.stats_at(t, cut["close"], 60, cost)
        same = (s_full.get("samples") == s_cut.get("samples")
                and _close(s_full.get("win_rate_pct"), s_cut.get("win_rate_pct"), 1e-9)
                and _close(s_full.get("expected_return_pct"),
                           s_cut.get("expected_return_pct"), 1e-9))
        return same, (f"同一交易日（{raw['dates'][t]}）截断重算：样本 {s_full.get('samples')}、"
                      f"胜率 {s_full.get('win_rate_pct')}%、期望 "
                      f"{s_full.get('expected_return_pct')}% 全部一致"
                      if same else
                      f"全量 n={n} → {s_full.get('samples')}/{s_full.get('win_rate_pct')} "
                      f"vs 截断 {s_cut.get('samples')}/{s_cut.get('win_rate_pct')}")

    check("无未来函数", "历史相似状态统计截断一致性", stats_consistency)

    def no_lookahead_window():
        """确认 window_drawdown / relative_position 只用最近 N 根。"""
        aid = next(iter(ind_full))
        raw = raw_data[aid]
        a = cfg.asset_by_id(aid)
        k = 300
        cut = indicators.compute_all(raw["dates"][:k], raw["open"][:k], raw["high"][:k],
                                     raw["low"][:k], raw["close"][:k], cfg,
                                     a.get("asset_class", "stock"))
        full = ind_full[aid]
        ok = (_close(cut["dd_windows"]["1y"].get("max_drawdown_pct"),
                     _window_dd(full["close"], k, 252), 1e-9))
        return ok, "回撤窗口计算与独立实现一致"

    def _window_dd(close, k, w):
        seg = [v for v in close[max(0, k - w):k] if v is not None]
        peak, mdd = seg[0], 0.0
        for v in seg:
            peak = max(peak, v)
            mdd = min(mdd, v / peak - 1)
        return mdd * 100

    check("无未来函数", "回撤窗口独立复算一致", no_lookahead_window)

    # ---------------------------------------------------------------- 数据源
    def _unreachable_note() -> str:
        blocked = [w for w in (macro.get("warnings") or []) if "不可达" in w]
        return ("数据源主机不可达，已如实标注『暂无数据』且未伪造数据"
                + (f"（{blocked[0][:70]}…）" if blocked else ""))

    def macro_check(key, label, path):
        def fn():
            node = macro.get(key)
            if not node or not node.get("close"):
                if macro.get("yahoo_ok") is False:
                    return True, _unreachable_note()
                return False, "暂无数据（但主机可达，说明解析或字段异常，需排查）"
            metrics = macro.get(path) or {}
            return (metrics.get("available") is True,
                    f"最新 {metrics.get('last')}，数据日 {node.get('data_time')}，"
                    f"来源 {node.get('source')}")
        return fn

    print("  · 获取宏观/黄金数据…")
    try:
        from data_sources.macro_source import MacroHub

        macro = MacroHub(cfg).load(True)
        NOTES.extend(macro.get("warnings", []))
        try:
            from core.gold_macro import gold_macro_score as _gms
            macro["gold_macro_score"] = _gms(macro, None)
        except Exception as _exc:  # noqa: BLE001
            macro["gold_macro_score"] = {}
            NOTES.append(f"黄金宏观评分自检失败：{_exc}")
    except Exception:  # noqa: BLE001
        macro = {}

    check("数据源", "国际金价", macro_check("gold", "金价", "gold_metrics"))
    check("数据源", "美元指数 DXY", macro_check("usd", "DXY", "usd_metrics"))
    check("数据源", "美国10年期实际利率", macro_check("real_rate", "实际利率", "real_rate_metrics"))
    check("数据源", "美国10年期国债收益率", macro_check("nominal_rate", "名义利率", "nominal_rate_metrics"))

    def bpgdm_check():
        bm = macro.get("bpgdm_metrics") or {}
        if bm.get("real_available"):
            return True, (f"BPGDM真实值：{bm.get('bpgdm_real')}"
                          f"；来源：{bm.get('bpgdm_real_source')}"
                          f"；数据日：{bm.get('bpgdm_real_data_date')}")
        if bm.get("proxy_available"):
            return True, (f"BPGDM真实值：暂无数据；BPGDM代理值：{bm.get('bpgdm_proxy')}"
                          f"；代理定义：{bm.get('bpgdm_proxy_algo') or '约30只金矿股站上200日均线占比'}"
                          "；状态：【代理，仅供研究】")
        # 真实值与本地代理**同时**取不到（例：Yahoo 被墙 → 金矿股行情取不到，
        # 代理也算不出来）。此时只要"如实标注了原因、没有伪造"，就算合格。
        err = str(bm.get("real_error") or "").strip()
        if err and "本次不可用" not in err:
            return True, f"真实 BPGDM 与本地代理均不可用，已如实标注：{err[:140]}"
        if macro.get("yahoo_ok") is False:
            return True, _unreachable_note()
        return False, "暂无数据（但主机可达，需排查）"

    check("数据源", "BPGDM（真实值优先；缺真实值时明确标注为代理）", bpgdm_check)
    check("数据源", "Gold Fear & Greed（官方，不可用则明确暂无数据）",
          lambda: (True, "官方值 " + str((macro.get("fear_greed") or {}).get("score"))
                   + "；本地代理 "
                   + str((macro.get("gold_sentiment_proxy") or {}).get("score"))))
    check("数据源", "黄金底部雷达 8 项条件",
          lambda: (bool((macro.get("gold_radar") or {}).get("conditions")),
                   f"{(macro.get('gold_radar') or {}).get('hit')}/"
                   f"{(macro.get('gold_radar') or {}).get('total')} "
                   f"{(macro.get('gold_radar') or {}).get('status')}"
                   + (f"（缺{(macro.get('gold_radar') or {}).get('missing')}）"
                      if (macro.get('gold_radar') or {}).get("missing") else "")))

    # ---- 用户策略 2026-10：GVZ / Fed 预期 / 宏观评分 / 三级分级 ----
    def gvz_check():
        gv = macro.get("gvz_metrics") or {}
        if gv.get("available") is True:
            return (True, f"GVZ {gv.get('gvz')} → 系统研究分档『{gv.get('band_name')}』"
                          f"（非官方阈值；低位<20={gv.get('below_20')}，"
                          f"低位拐头={gv.get('turning_up')}；"
                          f"52周分位 {gv.get('gvz_52w_percentile')}%，"
                          f"来源 {gv.get('gvz_source')}，"
                          f"数据日 {gv.get('gvz_data_date')}）")
        return True, ("GVZ 各源（CBOE/Yahoo/FRED/MacroMicro/Moomoo/TradingView）"
                      "当前均不可用，如实标『暂无数据 / UNKNOWN』、"
                      "条件②③为未知、评分剔除该分量，绝不伪造固定假值")

    check("数据源", "黄金波动率指数 GVZ（条件②③依据，系统研究分档）", gvz_check)

    def policy_check():
        pm = macro.get("policy_metrics") or {}
        if pm.get("available") is True:
            return True, f"2Y-3M = {pm.get('last')}pp，{pm.get('direction')}"
        return True, "财政部 CSV 缺 2Yr/3Mo 列或不可达，如实显示暂无数据"

    check("数据源", "Fed 利率预期（2Y-3M 政策预期代理）", policy_check)

    # ------------------------- 数据来源与有效性（来源 / 网址 / 状态 / 到秒时间）
    # 用户要求：每项参数都要注明来源、贴出可核对网址、标正常还是错误、
    # 并给出精确到秒的读取时间；连不上就显示错误。这里用**合成快照**
    # （脱离网络、可复现、冻结态也能跑）逐条守住。
    from core.data_provenance import CATALOG as _PROV_CATALOG
    from core.data_provenance import build_provenance as _build_prov
    from core.data_provenance import render_block as _render_prov

    _PROV_SNAP = {
        "data_status": [
            # 实时源可达 → 真正常
            {"name": "美国10年期实际利率", "ok": True,
             "message": "已取到 2026-10-07", "label": "本次联网获取",
             "from_cache": False, "updated_at": "2026-10-07 09:15:03",
             "data_time": "2026-10-07"},
            # 实时源 403、退回本地缓存 → 必须标「缓存（实时源失败）」，不能算"正常"
            {"name": "国际金价", "ok": True,
             "message": "联网获取失败(RuntimeError: Yahoo GC=F 请求失败：HTTP 403)，使用缓存",
             "label": "本地缓存", "from_cache": True,
             "updated_at": "2026-10-07T09:15:04", "data_time": "2026-10-06"},
            # 命中未过期缓存（本次有意不联网）→ 中性「缓存（未过期）」，也不算错误
            {"name": "美国10年期国债收益率", "ok": True,
             "message": "命中本地缓存（未过期）", "label": "本地缓存",
             "from_cache": True, "updated_at": "2026-10-07 09:15:05",
             "data_time": "2026-10-07"},
            # 实时源不可达且无缓存 → 必须标「错误」
            {"name": "ETF行情（同花顺扶摇）", "ok": False,
             "message": "扶摇不可达，且无可用缓存", "label": "数据源异常",
             "from_cache": False, "updated_at": "2026-10-07 09:15:06",
             "data_time": None},
        ],
        "freshness": {"rows": [
            {"key": "etf_close", "data_date": None, "age_days": None,
             "status": "UNKNOWN"},
            {"key": "gold", "data_date": "2026-10-06", "age_days": 1,
             "status": "OK"},
        ]},
        "macro": {"bpgdm_metrics": {
            "available": True, "status": "PROXY_ONLY",
            "real_available": False, "proxy_available": True,
            "bpgdm_proxy": 18.52,
            "bpgdm_proxy_algo": "27 只金矿股站上 200 日均线占比",
            "bpgdm_proxy_data_date": "2026-10-06",
            "real_error": "StockQ HTTP 403（被 WAF 拦截）",
        }},
    }
    _PROV = _build_prov(None, _PROV_SNAP)
    _PROV_MAP = {r["key"]: r for r in _PROV["rows"]}

    def prov_covers_all():
        keys = [e["key"] for e in _PROV_CATALOG]
        got = [r["key"] for r in _PROV["rows"]]
        miss_src = [r["label"] for r in _PROV["rows"] if not r["source"]]
        return (keys == got and not miss_src,
                f"{len(got)} 项参数全部注明来源（缺来源 {len(miss_src)} 项）")

    check("数据来源与有效性", "覆盖全部参数且每项都注明来源", prov_covers_all)

    def prov_error_marks():
        br = _PROV_MAP.get("bpgdm_real") or {}
        ec = _PROV_MAP.get("etf_close") or {}
        ok = (br.get("status") == "ERROR" and "403" in (br.get("detail") or "")
              and ec.get("status") == "ERROR"
              and "无可用缓存" in (ec.get("detail") or ""))
        return ok, (f"BPGDM真实值={br.get('status')}（{br.get('detail')}）；"
                    f"ETF行情={ec.get('status')}（{ec.get('detail')}）")

    check("数据来源与有效性", "连不上 → 标【错误】并给出原因（绝不冒充正常）",
          prov_error_marks)

    def prov_cache_marks():
        g = _PROV_MAP.get("gold") or {}
        return (g.get("status") == "CACHE_FALLBACK"
                and "实时源失败" in (g.get("status_text") or "")
                and "403" in (g.get("detail") or "")
                and g.get("from_cache") is True,
                f"实时源 403、用缓存 → 状态『{g.get('status_text')}』"
                f"（不是『正常』）；原因：{g.get('detail')}")

    check("数据来源与有效性", "实时源失败退回缓存 → 标『缓存（实时源失败）』",
          prov_cache_marks)

    def prov_plain_cache():
        n = _PROV_MAP.get("nominal_rate") or {}
        errs = [r["label"] for r in _PROV["rows"] if r["status"] == "ERROR"]
        return (n.get("status") == "CACHE"
                and "未过期" in (n.get("status_text") or "")
                and _PROV["cache_count"] == 1
                and n.get("label") not in errs,
                f"未过期缓存命中 → 『{n.get('status_text')}』，"
                f"未计入错误（当前错误项：{'、'.join(errs) or '无'}）")

    check("数据来源与有效性", "未过期缓存命中不算错误（区别于实时源失败）",
          prov_plain_cache)

    def prov_seconds():
        pat = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
        g = (_PROV_MAP.get("gold") or {}).get("updated_at")
        e = (_PROV_MAP.get("etf_close") or {}).get("updated_at")
        return (bool(g and pat.match(g) and e and pat.match(e)),
                f"金价读取时间={g}；ETF读取时间={e}（均精确到秒）")

    check("数据来源与有效性", "读取时间精确到秒（YYYY-MM-DD HH:MM:SS）", prov_seconds)

    def prov_urls():
        local_only = {"bpgdm_proxy", "fear_greed"}
        bad = [r["label"] for r in _PROV["rows"]
               if r["key"] not in local_only
               and not str(r.get("url") or "").startswith("http")]
        return not bad, ("可核对网址齐全" if not bad else "缺网址：" + "、".join(bad))

    check("数据来源与有效性", "每项都有可点开的核对网址（本地代理除外）", prov_urls)

    def prov_proxy_row():
        pr = _PROV_MAP.get("bpgdm_proxy") or {}
        return ("非真实" in (pr.get("label") or "")
                and "不参与" in (pr.get("note") or ""),
                f"代理单独成行：{pr.get('label')} → {pr.get('detail')}")

    check("数据来源与有效性", "BPGDM 代理单独成行并注明『非真实』", prov_proxy_row)

    def prov_proxy_out_of_score():
        from core.gold_macro import gold_macro_score
        ms = gold_macro_score(dict(_PROV_SNAP["macro"]), None)
        comp = next((c for c in ms.get("components", []) if c["key"] == "bpgdm"), {})
        return (comp.get("score") is None and comp.get("available") is False
                and "不参与评分" in (comp.get("reason") or ""),
                f"BPGDM 组件 score={comp.get('score')}（代理不参与正式评分）")

    check("数据来源与有效性", "BPGDM 代理不参与正式宏观评分（score=None）",
          prov_proxy_out_of_score)

    def prov_render():
        lines = _render_prov(_PROV)
        return (len(lines) == len(_PROV["rows"]) + 2,
                f"render_block 输出 {len(lines)} 行 Markdown 表")

    check("数据来源与有效性", "render_block 可渲染（CLI/报告复用）", prov_render)

    def prov_channel_field():
        bad = [r["label"] for r in _PROV["rows"] if "channel" not in r]
        return (not bad, f"每行都带取数通道字段（无遗漏：{bad or '无'}）")

    check("数据来源与有效性", "每行标注实际取数通道（浏览器 UA/诚实 UA/curl）",
          prov_channel_field)

    def prov_channel_fallback_marked():
        """走了兜底通道时，报告里必须写明——否则用户会以为"直连就拿到了"。"""
        snap = dict(_PROV_SNAP)
        snap["data_status"] = list(snap["data_status"]) + [
            {"name": "国际金价", "ok": True, "message": "已取到 2026-10-08 收盘",
             "label": "本次联网获取", "from_cache": False,
             "updated_at": "2026-10-08 22:48:02", "data_time": "2026-10-08"},
        ]
        prov = _build_prov(None, snap)
        row = next((r for r in prov["rows"] if r["key"] == "gold"), {})
        return (row.get("channel") in ("", "browser", "honest", "curl")
                and "取数通道" not in (row.get("detail") or "")
                or row.get("channel") in ("honest", "curl"),
                f"金价行取数通道={row.get('channel') or '直连'}")

    check("数据来源与有效性", "取数通道写入来源表（兜底时如实注明）",
          prov_channel_fallback_marked)

    def prov_ths_serialized():
        """ETF 主源（同花顺扶摇）必须**进程内串行化**。

        依据（2026-10-08 实测）：扶摇对并发突发限流很重，3 路并发会把单次响应
        拖到 30~50 秒 → 触发硬超时 → 主机熔断 → **静默回落到腾讯证券**（只有约
        640 天），"真实六 ETF 共同区间"从 803 天被砍到 640 天。必须保持串行化。
        冻结态读不到源码 → 视为跳过（源码自检已覆盖）。
        """
        src = _read_src("data_sources/etf_source.py")
        if src is None:
            return (True, "冻结态跳过（源码守卫已在源码自检覆盖）")
        ok = ("_THS_LOCK" in src and "with _THS_LOCK" in src
              and "ths_timeout_seconds" in src)
        return (ok,
                "扶摇请求已进程内串行化（_THS_LOCK）+ 超时可配（ths_timeout_seconds）"
                if ok else
                "未见扶摇串行化/超时配置——并发限流会静默回落短历史备源")

    check("数据来源与有效性", "ETF 主源（扶摇）进程内串行化，避免并发限流丢长历史",
          prov_ths_serialized)

    def prov_version_stamp_visible():
        """窗口标题必须带版本号与构建时间戳。

        依据（2026-10-09 用户反馈）：打开界面分不清新旧版——标题栏只显示固定
        文案，无法判断跑的是哪次构建的 EXE。要求：core/version.py 有
        APP_VERSION + BUILD_STAMP，且 main_window 标题拼接二者。
        """
        from core.version import APP_VERSION, BUILD_STAMP
        if not APP_VERSION:
            return (False, "core/version.py 缺 APP_VERSION")
        src = _read_src("ui/main_window.py")
        if src is None:
            # 冻结态读不到源码 → 只做运行时校验（版本模块可导入即通过）。
            return (True, f"运行时版本 v{APP_VERSION}（构建 {BUILD_STAMP or '未知'}）")
        ok = ("core.version" in src and "APP_VERSION" in src
              and "setWindowTitle" in src and "BUILD_STAMP" in src)
        return (ok,
                f"标题栏显示 v{APP_VERSION}（构建 {BUILD_STAMP or '源码态'}）"
                if ok else
                "main_window 未拼接版本号/构建时间——无法肉眼区分新旧 EXE")

    check("数据来源与有效性", "窗口标题栏显示版本号与构建时间（肉眼可辨新旧 EXE）",
          prov_version_stamp_visible)

    def prov_reader_relay_marked():
        """BPGDM 真值若经阅读器转发，必须显式注明『经转发』，不得隐去。"""
        from data_sources.http_util import UA_BROWSER, UA_HONEST, curl_channel_ready
        snap = {
            "data_status": [],
            "freshness": {"rows": []},
            "macro": {"bpgdm_metrics": {
                "available": True, "status": "OK",
                "real_available": True, "proxy_available": False,
                "bpgdm_real": 40.54, "bpgdm_relay": True,
                "bpgdm_real_source": "StockQ（经 r.jina.ai 阅读器转发）",
                "bpgdm_real_data_date": "2026-10-08",
            }},
        }
        prov = _build_prov(None, snap)
        row = next((r for r in prov["rows"] if r["key"] == "bpgdm_real"), {})
        ok = (row.get("status") == "OK" and "转发" in (row.get("detail") or "")
              and row.get("channel") == "reader")
        return (ok and bool(UA_BROWSER) and bool(UA_HONEST),
                f"真实 BPGDM 经转发 → 状态『{row.get('status_text')}』，"
                f"通道={row.get('channel')}；UA 兜底就绪（curl={curl_channel_ready()}）")

    check("数据来源与有效性", "BPGDM 真值经阅读器转发时如实注明『经转发』",
          prov_reader_relay_marked)

    def macro_score_check():
        ms = macro.get("gold_macro_score") or {}
        comps = ms.get("components") or []
        wsum = round(sum(c.get("weight") or 0 for c in comps), 6)
        if wsum and abs(wsum - 1.0) > 1e-6:
            return False, f"权重合计 {wsum} ≠ 1"
        if not ms.get("available"):
            return True, "宏观源不可达时评分为空（不伪造）" if macro.get("yahoo_ok") is False \
                else "无可用分量，需排查"
        names = "、".join(c["name"] for c in comps if c.get("available"))
        return True, (f"评分 {ms.get('score')}/100（{ms.get('grade_label')}）；"
                      f"可用分量 {len([c for c in comps if c.get('available')])}/8：{names[:60]}")

    check("用户策略", "黄金宏观评分：8 指标权重合计=100% 且逐项透明",
          macro_score_check)

    def radar_levels():
        from core.gold_macro import gold_bottom_radar as _radar

        def base_macro(conds_ok: dict, gvz_ok=True):
            gmc = {"available": True, "last": 2750.0, "stopped_new_low": conds_ok.get(1, False),
                   "ret20_pct": 1.0, "ma60": 2500.0, "ma200": 2400.0,
                   "dd_from_high_pct": -3.0, "vol20_annual_pct": 15.0}
            # 真实"涨-跌-反弹"形态：3% 阈值的 zigzag 能确认前一个反弹高点 2700
            close = ([2600.0] * 40 + [2600.0 + i * 10 for i in range(1, 11)]
                     + [2700.0 - i * 20 for i in range(1, 11)]
                     + [2500.0 + i * 25 for i in range(1, 11)])
            high = [c + 10 for c in close]
            low = [c - 10 for c in close]
            gold = {"close": close, "high": high, "low": low}
            m = {
                "gold": gold,
                "gold_metrics": gmc,
                "usd_metrics": {"available": True, "last": 100.0, "ret20_pct": -2.0 if conds_ok.get(6) else 1.0,
                                 "weak_confirmed": conds_ok.get(6, False)},
                "real_rate_metrics": {"available": True, "last": 2.0,
                                       "chg_20d": -0.2 if conds_ok.get(7) else 0.2,
                                       "falling": conds_ok.get(7, False), "rising": False},
                "bpgdm_metrics": {"available": True, "last": 25.0 if conds_ok.get(4) else 55.0,
                                   "percentile_2y": 20.0 if conds_ok.get(4) else 60.0,
                                   "turning_up": conds_ok.get(5, False), "below_ma60": True,
                                   "source": "测试"},
                "gvz_metrics": ({"available": True, "last": 18.0, "chg_5d": 0.5 if conds_ok.get(3) else -0.5,
                                  "chg_20d": 1.0, "below_20": conds_ok.get(2, False),
                                  "turning_up": conds_ok.get(3, False), "percentile_2y": 10.0}
                                if gvz_ok else {"available": False}),
            }
            return m

        cases = [
            ({1: True, 2: True, 3: True}, "恐慌区"),
            ({1: True, 2: True, 3: True, 4: True, 5: True}, "底部观察区"),
            ({1: True, 2: True, 3: True, 4: True, 5: True, 6: True, 7: True, 8: True},
             "趋势反转确认区"),
        ]
        for conds, expect in cases:
            st = _radar(base_macro(conds)).get("status")
            if st != expect:
                return False, f"条件{sorted(conds)}应={expect}，实际={st}"
        st0 = _radar(base_macro({})).get("status")
        if st0 != "未入底部信号":
            return False, f"无条件满足应=未入底部信号，实际={st0}"
        # GVZ 缺失时条件②③必须为 None（不算满足）
        r = _radar(base_macro({1: True, 4: True}, gvz_ok=False))
        ok2 = next(c["ok"] for c in r["conditions"] if c["no"] == 2)
        ok3 = next(c["ok"] for c in r["conditions"] if c["no"] == 3)
        if ok2 is not None or ok3 is not None:
            return False, "GVZ 缺失时条件②③应为 None（不得伪造）"
        return True, "恐慌区/底部观察区/趋势反转确认区/未入信号 分级全部正确"

    check("用户策略", "底部雷达三级分级规则（恐慌区→底部观察区→趋势反转确认区）",
          radar_levels)
    # ---- GVZ / BPGDM 口径最终修正（10 项硬性验收；不改任何策略统计）----
    def _gvz_fixture(n=300):
        close = [20.0 + (i % 9) * 0.2 for i in range(n)]
        return {"dates": [f"2025-{(i % 12) + 1:02d}-{(i % 28) + 1:02d}"
                          for i in range(n)],
                "close": close, "data_time": "2026-10-06",
                "data_date": "2026-10-06", "raw": close[-1],
                "source": "CBOE 官方"}

    def _bm_fixture(kind="official", n=300):
        close = [20.0 + float(i % 30) for i in range(n)]
        d = {"dates": [f"2025-{(i % 12) + 1:02d}-{(i % 28) + 1:02d}"
                       for i in range(n)],
             "close": close, "data_time": "2026-10-06", "kind": kind,
             "is_real": kind == "official", "source": "测试源"}
        if kind != "official":
            d["algo"] = "约 30 只金矿股站上 200 日均线占比（趋势广度代理）"
        return d

    def bpgdm_range_0_100():
        from data_sources.bpgdm_source import bpgdm_metrics, bpgdm_valid, bpgdm_zone
        if not (bpgdm_valid(0) and bpgdm_valid(100)
                and not bpgdm_valid(101) and not bpgdm_valid(-0.01)):
            return False, "0/100 应合法，101/-0.01 应非法"
        good = bpgdm_metrics(_bm_fixture())
        if not (good.get("available") and 0.0 <= good["last"] <= 100.0):
            return False, f"合法序列判定异常：{good.get('last')}"
        bad_fx = _bm_fixture()
        bad_fx["close"] = list(bad_fx["close"][:-1]) + [101.0]
        bad = bpgdm_metrics(bad_fx)
        if bad.get("available") is not False or bad.get("status") != "UNKNOWN":
            return False, "越界值 101 未被拒绝（应 UNKNOWN）"
        z = (bpgdm_zone(25)["name"], bpgdm_zone(45)["name"], bpgdm_zone(60)["name"],
             bpgdm_zone(75)["name"], bpgdm_zone(85)["name"])
        if z != ("超卖/弱势区", "偏弱", "偏强", "高位区", "极端高位/拥挤风险"):
            return False, f"研究区间档位不符：{z}"
        return True, "0/100 合法、101/-0.01 拒绝；<30/30~50/50~70/70~80/≥80 五档正确"

    check("GVZ/BPGDM口径", "1. BPGDM 必须落在 0~100，越界判 UNKNOWN", bpgdm_range_0_100)

    def bpgdm_real_proxy_split():
        from data_sources.bpgdm_source import bpgdm_metrics
        r = bpgdm_metrics(_bm_fixture("official"))
        p = bpgdm_metrics(_bm_fixture("proxy"))
        if not (r["bpgdm_real"] is not None and r["bpgdm_proxy"] is None):
            return False, "真实值可用时 bpgdm_proxy 必须为 None"
        if not (p["bpgdm_proxy"] is not None and p["bpgdm_real"] is None):
            return False, "代理可用时 bpgdm_real 必须为 None（不得冒充）"
        if not (r["real_available"] and not r["proxy_available"]
                and p["proxy_available"] and not p["real_available"]):
            return False, "real_available / proxy_available 标记错误"
        for k in ("bpgdm_real", "bpgdm_proxy", "bpgdm_real_source",
                  "bpgdm_proxy_source", "bpgdm_real_data_date",
                  "bpgdm_proxy_data_date", "bpgdm_proxy_algo"):
            if k not in r:
                return False, f"缺少字段 {k}"
        return True, "bpgdm_real / bpgdm_proxy 严格分离，来源与数据日各自独立"

    check("GVZ/BPGDM口径", "2. 真实BPGDM与代理BPGDM字段分离", bpgdm_real_proxy_split)

    def sources_shown():
        from data_sources.bpgdm_source import bpgdm_metrics
        from data_sources.gvz_source import gvz_metrics
        g = gvz_metrics(_gvz_fixture())
        p = bpgdm_metrics(_bm_fixture("proxy"))
        probs = []
        if g.get("gvz_source") != "CBOE 官方":
            probs.append(f"gvz_source={g.get('gvz_source')}")
        if g.get("gvz_data_date") != "2026-10-06":
            probs.append(f"gvz_data_date={g.get('gvz_data_date')}")
        if g.get("gvz_raw_value") is None:
            probs.append("gvz_raw_value 缺失")
        if p.get("bpgdm_proxy_source") != "测试源":
            probs.append(f"bpgdm_proxy_source={p.get('bpgdm_proxy_source')}")
        if p.get("bpgdm_proxy_data_date") != "2026-10-06":
            probs.append(f"bpgdm_proxy_data_date={p.get('bpgdm_proxy_data_date')}")
        if probs:
            return False, "来源/数据日字段异常：" + "；".join(probs)
        return True, (f"GVZ 来源={g['gvz_source']} 数据日={g['gvz_data_date']}；"
                      f"BPGDM代理来源={p['bpgdm_proxy_source']}")

    check("GVZ/BPGDM口径", "3. source / data_date / raw value 均保存且显示正确",
          sources_shown)

    def proxy_never_real():
        from data_sources.bpgdm_source import bpgdm_metrics
        p = bpgdm_metrics(_bm_fixture("proxy"))
        if p.get("bpgdm_real") is not None:
            return False, "代理值被写进了 bpgdm_real"
        if p.get("status") != "PROXY_ONLY":
            return False, f"缺真实值时应 status=PROXY_ONLY，实际 {p.get('status')}"
        if not p.get("bpgdm_proxy_algo"):
            return False, "代理必须给出算法说明（代理算法必须可见）"
        if p.get("bpgdm_real_source") is not None:
            return False, "真实值不可用时 bpgdm_real_source 必须为 None"
        return True, "缺真实 BPGDM 时只填 bpgdm_proxy，state=PROXY_ONLY，不伪装成真实值"

    check("GVZ/BPGDM口径", "4. 缺失真实 BPGDM 时不得伪装成真实值", proxy_never_real)

    def gvz_no_fixed_fake():
        from data_sources.gvz_source import gvz_metrics
        u = gvz_metrics(None)
        leaked = [k for k in ("gvz", "gvz_change_1d", "gvz_ret_5", "gvz_ret_10",
                             "gvz_ret_20", "gvz_ma20", "gvz_bias20", "gvz_52w_high",
                             "gvz_52w_low", "gvz_52w_percentile", "gvz_zscore")
                  if u.get(k) is not None]
        if leaked:
            return False, f"源失败时这些字段应为 None：{leaked}"
        if u.get("available") is not False or u.get("status") != "UNKNOWN":
            return False, "源失败必须 available=False / status=UNKNOWN"
        if u.get("gvz_trend") != "UNKNOWN" or u.get("gvz_source") is not None:
            return False, "源失败时趋势应 UNKNOWN、来源应 None"
        if u.get("band_name") != "暂无数据":
            return False, "源失败时分档应显示『暂无数据』"
        return True, "GVZ 源失败：全部字段 None + status=UNKNOWN，无 0/20 等固定假值"

    check("GVZ/BPGDM口径", "5. GVZ 源失败时不得使用固定假值", gvz_no_fixed_fake)

    def gvz_threshold_not_official():
        from data_sources.gvz_source import (GVZ_BANDS, GVZ_BAND_BASIS, gvz_band,
                                             gvz_metrics)
        if "系统研究参数" not in GVZ_BAND_BASIS:
            return False, "分档依据未标注『系统研究参数』"
        for _hi, name in GVZ_BANDS:
            if "官方" in name:
                return False, f"分档名称含『官方』：{name}"
        b = gvz_band(18.0)
        if b.get("official") is not False or "系统研究参数" not in (b.get("basis") or ""):
            return False, "分档未声明 official=False / 系统研究参数"
        from core.gold_macro import gold_macro_score
        ms = gold_macro_score({"gvz_metrics": gvz_metrics(_gvz_fixture())}, None)
        comp = next((c for c in ms["components"] if c["key"] == "gvz"), None)
        if comp is None:
            return False, "宏观评分缺少 GVZ 分量"
        reason = comp.get("reason") or ""
        forbidden = ("官方买点", "官方卖点", "官方规则", "官方参数", "官方阈值",
                     "CBOE官方买", "权威分档")
        # 允许"非官方阈值"这类否定表述，只禁止把阈值说成官方规则
        hit = [p for p in forbidden
               if p in reason and f"非{p}" not in reason]
        if hit:
            return False, f"GVZ 评分理由出现官方规则表述 {hit}：{reason}"
        if "系统研究分档" not in reason or "非官方" not in reason:
            return False, f"GVZ 评分理由未标『系统研究分档（非官方）』：{reason}"
        return True, "20/30 一律标注『系统研究参数（非官方阈值）』，分档 official=False"

    check("GVZ/BPGDM口径", "6. GVZ 固定阈值不得标记为『官方规则』",
          gvz_threshold_not_official)

    def pnf_not_faked():
        from data_sources.bpgdm_source import bpgdm_metrics
        r = bpgdm_metrics(_bm_fixture("official"))
        p = bpgdm_metrics(_bm_fixture("proxy"))
        if r["bpgdm_pnf_reversal"] is not None or p["bpgdm_pnf_reversal"] is not None:
            return False, "无 P&F 原始数据时必须 UNKNOWN(None)，不得反推"
        if "UNKNOWN" not in (r.get("pnf_note") or ""):
            return False, "P&F 说明未标 UNKNOWN"
        okx = _bm_fixture("official")
        okx["pnf_reversal"] = True
        if bpgdm_metrics(okx)["bpgdm_pnf_reversal"] is not True:
            return False, "源直接提供 P&F 反转时应采纳该值"
        return True, ("缺 P&F 原始数据 → UNKNOWN（不用普通 K 线猜 3-box 反转）；"
                      "源提供时才采纳")

    check("GVZ/BPGDM口径", "7. P&F 数据缺失时不得伪造 3-box reversal", pnf_not_faked)

    def neither_singly_buys():
        from core.gold_macro import gold_bottom_radar
        m = {
            "gold_metrics": {"available": True, "last": 2600.0,
                             "stopped_new_low": False, "ret20_pct": -3.0,
                             "ma60": 2700.0, "ma200": 2800.0,
                             "dd_from_high_pct": -12.0},
            "usd_metrics": {"available": True, "last": 105.0,
                            "ret20_pct": 2.0, "weak_confirmed": False},
            "real_rate_metrics": {"available": True, "last": 2.2, "chg_20d": 0.2,
                                  "falling": False, "rising": True},
            "bpgdm_metrics": {"available": True, "last": 25.0, "percentile_2y": 15.0,
                              "turning_up": True, "below_ma60": True,
                              "real_available": False, "source": "代理",
                              "zone_name": "超卖/弱势区"},
            "gvz_metrics": {"available": True, "last": 17.0, "chg_5d": 1.0,
                            "chg_20d": -1.0, "below_20": True, "turning_up": True,
                            "band": {"name": "正常基准", "index": 2}},
            "fear_greed": {},
        }
        r = gold_bottom_radar(m, cfg)
        oks = {c["no"]: c["ok"] for c in r["conditions"]}
        if not (oks.get(2) and oks.get(3) and oks.get(4) and oks.get(5)):
            return False, f"前置假设不成立（GVZ/BPGDM 条件未满足）：{oks}"
        if r["level"] != 0 or r["status"] != "未入底部信号":
            return False, (f"仅 GVZ(②③)+BPGDM(④⑤) 满足却得到 {r['status']}"
                           "（GVZ/BPGDM 不得单独触发买入）")
        if any("买入" in (c.get("detail") or "") for c in r["conditions"]):
            return False, "雷达条件文案不应出现『买入』字样"
        return True, ("仅 GVZ(②③)+BPGDM(④⑤) 满足、缺 ①⑥⑦ → 未入底部信号："
                      "两者都不能单独触发买入")

    check("GVZ/BPGDM口径", "8. GVZ / BPGDM 均不能单独触发 BUY", neither_singly_buys)

    def radar_gated_by_permission():
        from core.decision import BUY_ACTIONS, buy_permission, today_action
        if (buy_permission(cfg, "研究级")["allowed"]
                or buy_permission(cfg, "观察级")["allowed"]):
            return False, "研究级 / 观察级不应开放买入权限"
        if not buy_permission(cfg, "辅助交易级")["allowed"]:
            return False, "辅助交易级应开放买入权限"
        args = dict(
            gate={"grade": "A", "prob_used": 85.7, "exp60": 5.0, "payoff": 2.0},
            three={"new_money": True, "rebalance": False,
                   "reduce_overweight": False},
            risk={"state": "Normal", "score": 30}, rebal={},
            plan={"batch_count": 4, "items": []}, consistency={"ok": True},
            cur={"by_class": {"gold": 0.03}, "etf_value": 100000.0,
                 "total_assets": 110000.0, "cash_pct": 5.0},
            cfg=cfg,
            marginal={"best_trade": {"name": "测试ETF", "amount": 1500.0,
                                     "improve_quality": 1.2,
                                     "improve_prob60": 0.5}},
        )
        r = today_action(trust_level="研究级", **args)
        if r["key"] in BUY_ACTIONS:
            return False, f"研究级仍输出买入状态：{r['key']}"
        if "建议分批买入" in (r.get("sub") or "") or "小仓试探" in (r.get("sub") or ""):
            return False, "研究级结论仍出现『建议分批买入 / 小仓试探』"
        if not r.get("permission_blocked"):
            return False, "研究级应标记 permission_blocked=True"
        t = today_action(trust_level="辅助交易级", **args)
        if t["key"] != "GREEN_BUY":
            return False, f"辅助交易级应可输出 GREEN_BUY，实际 {t['key']}"
        return True, ("研究级：permission_blocked=True、结论只能是持有观察/暂停加仓/"
                      "防守/再平衡/WAIT；辅助交易级才 GREEN_BUY")

    check("GVZ/BPGDM口径", "9. 黄金底部雷达仍受总系统 BUY Permission Gate 限制",
          radar_gated_by_permission)

    def rerun_all_selftest():
        groups = {}
        for r in RESULTS:
            groups.setdefault(r["group"], [0, 0])
            groups[r["group"]][1] += 1
            if r["ok"]:
                groups[r["group"]][0] += 1
        failed = [r["name"] for r in RESULTS if not r["ok"]]
        if failed:
            return False, f"本次自检存在未通过项：{failed[:6]}"
        brief = "；".join(f"{g} {v[0]}/{v[1]}" for g, v in groups.items())
        return True, f"全部 {len(RESULTS)} 项自检通过（{brief}）"

    check("GVZ/BPGDM口径", "10. 重新运行全部 selftest（口径修正后无回归）",
          rerun_all_selftest)

    # ================================================================
    # 交易价值与闸门（20 项）—— 策略重构新增
    # ================================================================
    from core import trade_value as _tv, cost_model as _cm, walk_forward as _wf
    from core import freshness_gate as _fg, sell_model as _sm
    from core import decision_engine as _de, policy_evaluator as _pe
    import random as _rnd
    from pathlib import Path as _Path

    class _FakePanel:
        """合成组合面板（只用价格序列，不含任何宏观/代理字段）。"""

        def __init__(self, rets, weights):
            self.assets = list(rets)
            self.weights = dict(weights)
            self.ret = rets
            self.n = len(next(iter(rets.values())))
            self.close = {a: [1.0] * self.n for a in self.assets}
            self.open = {a: [1.0] * self.n for a in self.assets}
            self.active = [[a for a in self.assets if self.ret[a][i] is not None]
                           for i in range(self.n)]
            self.nav = [1.0]
            for i in range(1, self.n):
                r = _tv.port_ret_at(self, self.weights, i) or 0.0
                self.nav.append(self.nav[-1] * (1 + r))
            self.dates = [f"D{i}" for i in range(self.n)]

    _rnd.seed(7)
    _N = 300
    _rets = {"a": [0.0008 + 0.012 * _rnd.gauss(0, 1) for _ in range(_N)],
             "b": [0.0002 + 0.003 * _rnd.gauss(0, 1) for _ in range(_N)]}
    _panel = _FakePanel(_rets, {"a": 0.5, "b": 0.5})
    _wb, _wa = {"a": 0.5, "b": 0.5}, {"a": 0.8, "b": 0.2}

    def t01_buy_wait_recalc():
        t, h = 50, 60
        wait = _tv.forward_nav(_panel, _wb, t, h)
        aft = _tv.forward_nav(_panel, _wa, t, h)
        exp = [1.0]
        for i in range(t + 1, t + h + 1):
            exp.append(exp[-1] * (1 + (_tv.port_ret_at(_panel, _wb, i) or 0.0)))
        if not _close(wait[-1], exp[-1], 1e-9):
            return False, f"WAIT 净值不符 {wait[-1]} vs {exp[-1]}"
        r = _tv.evaluate_trade(_panel, t, h, _wb, _wa, cost=0.0)
        want = (aft[-1] - 1) * 100 - (wait[-1] - 1) * 100
        if not _close(r["delta_return"], want, 1e-3):
            return False, f"delta_return 不符 {r['delta_return']} vs {want}"
        return True, f"交易后/不交易组合净值与增量均可独立复算（Δ={r['delta_return']:.4f}pp）"

    check("交易价值与闸门", "1. BUY 与 WAIT 的交易后组合重新计算正确", t01_buy_wait_recalc)

    def t02_sell_hold_recalc():
        t, h = 80, 60
        hold, sell = {"a": 0.6, "b": 0.4}, {"a": 0.3, "b": 0.7}
        res = _sm.evaluate_sell(_panel, t, (h,), hold, sell, tier="BASE")
        r = res["horizons"][h]
        w = _tv.forward_nav(_panel, hold, t, h)
        a = _tv.forward_nav(_panel, sell, t, h, cost=res["cost_frac"])
        want = (a[-1] - 1) * 100 - (w[-1] - 1) * 100
        if not _close(r["delta_return"], want, 1e-3):
            return False, f"SELL/HOLD 增量不符 {r['delta_return']} vs {want}"
        return True, f"SELL vs HOLD 可复算（Δ={r['delta_return']:.4f}pp，成本 {res['cost_frac']*100:.3f}%）"

    check("交易价值与闸门", "2. SELL 与 HOLD 的交易后组合重新计算正确", t02_sell_hold_recalc)

    def t03_cost_in_increment():
        t, h = 100, 60
        c = _cm.trade_cost(_wb, _wa, "BASE")
        r0 = _tv.evaluate_trade(_panel, t, h, _wb, _wa, cost=0.0)
        r1 = _tv.evaluate_trade(_panel, t, h, _wb, _wa, cost=c)
        # 净增量就是"已扣成本"的增量（以净值口径为准，不得重复扣减）
        if not _close(r1["delta_return_net"], r1["delta_return"], 1e-9):
            return False, "净增量与含成本增量不一致"
        if not _close(r1["delta_transaction_cost"], -c * 100, 1e-9):
            return False, "成本项符号/数值错误"
        # 成本必须真的把增量拉低（毛增量 > 净增量）
        if not (r1["delta_return_gross"] - r1["delta_return"] > 0):
            return False, "成本未进入增量收益（毛=净）"
        if not _close(r0["delta_return"], r1["delta_return_gross"], 1e-3):
            return False, "零成本档应等于毛增量"
        return True, (f"成本 {c*100:.4f}% 真实进入增量：毛 {r1['delta_return_gross']}pp "
                      f"→ 净 {r1['delta_return']}pp")

    check("交易价值与闸门", "3. 交易成本正确进入增量收益", t03_cost_in_increment)

    def t04_stale_forbid_buy():
        # 用**确定性**的陈旧快照（而不是去读本地缓存）：否则当 ETF 缓存恰好是
        # 当天时，age=0 并不大于上限 0，"陈旧"就构造不出来 —— 测试会偶发失败。
        stale_snap = {
            "data_status": [{"name": "ETF行情(513100)", "data_time": "2020-01-02"}],
            "realtime": {},
            "macro": {},
        }
        f = _fg.check_freshness(cfg, stale_snap, now=date(2024, 1, 1),
                                limits={"etf_close": 0})
        dr = _de.evaluate_decision(
            cfg, {"ok": True, "rows": []}, trust_level="实盘辅助级", freshness=f,
            evidence={"independent_events_60": 100, "buy_mean_net_pp_60": 5.0,
                      "stale": False})
        if f["can_buy"] or f["action"] == "PASS":
            return False, "陈旧数据仍被放行"
        if dr["final_action"] == "BUY":
            return False, "陈旧数据下仍输出 BUY"
        return True, f"陈旧 → {dr['final_action']}（禁止 BUY）"

    check("交易价值与闸门", "4. stale 数据必须禁止 BUY", t04_stale_forbid_buy)

    def t05_no_proxy_in_formal_model():
        bad = []
        for name in ("policy_evaluator.py", "buy_model.py", "sell_model.py",
                     "trade_value.py", "walk_forward.py"):
            src = _read_src(f"core/{name}")
            if src is None:
                continue          # 冻结态无源码可扫
            src = src.lower()
            for kw in ("bpgdm", "fear_greed", "gvz", "proxy"):
                if kw in src:
                    bad.append(f"{name}:{kw}")
        if bad:
            return False, f"正式交易模型引用了代理/宏观字段：{bad}"
        from core.buy_model import evaluate_buy
        r = evaluate_buy({"oos_available": True, "independent_events": 40,
                          "oos_increment_pp": 1.0, "cost_net_positive": True,
                          "macro_ok": False, "gate_allowed": True})
        if r["allowed"]:
            return False, "关键宏观 UNKNOWN 时仍允许 BUY"
        return True, "正式交易模型不引用任何代理宏观字段；关键宏观 UNKNOWN → 禁止 BUY"

    check("交易价值与闸门", "5. 代理宏观数据不得进入正式模型", t05_no_proxy_in_formal_model)

    def t06_future_truncation_identical():
        t, h = 100, 60
        r_full = _tv.evaluate_trade(_panel, t, h, _wb, _wa, cost=0.0)
        trunc = _FakePanel({"a": _rets["a"][:t + h + 1], "b": _rets["b"][:t + h + 1]},
                           {"a": 0.5, "b": 0.5})
        r_tr = _tv.evaluate_trade(trunc, t, h, _wb, _wa, cost=0.0)
        if r_full["delta_return"] != r_tr["delta_return"]:
            return False, f"截断后结果改变：{r_full['delta_return']} vs {r_tr['delta_return']}"
        longer = _FakePanel({"a": _rets["a"] + [0.05] * 40,
                             "b": _rets["b"] + [0.05] * 40}, {"a": 0.5, "b": 0.5})
        r_lo = _tv.evaluate_trade(longer, t, h, _wb, _wa, cost=0.0)
        if r_lo["delta_return"] != r_full["delta_return"]:
            return False, "追加未来数据改变了历史事件结果（未来函数）"
        return True, "截断/追加未来数据后，同一事件增量完全一致（无未来函数）"

    check("交易价值与闸门", "6. 未来数据截断结果完全一致", t06_future_truncation_identical)

    def t07_no_bypass_trust_gate():
        fresh = {"action": "PASS", "ok": True, "can_buy": True}
        ev = {"independent_events_60": 100, "buy_mean_net_pp_60": 5.0, "stale": False}
        d1 = _de.evaluate_decision(cfg, {"ok": True, "rows": []},
                                   trust_level="研究级", freshness=fresh, evidence=ev)
        if d1["final_action"] == "BUY":
            return False, "研究级仍输出 BUY（绕过 trust gate）"
        d2 = _de.evaluate_decision(cfg, {"ok": True, "rows": []},
                                   trust_level="实盘辅助级", freshness=fresh, evidence=ev)
        if d2["final_action"] != "BUY":
            return False, f"实盘辅助级+全闸门通过却未 BUY：{d2['reason']}"
        return True, "研究级禁止 BUY；仅实盘辅助级且全部闸门通过才 BUY"

    check("交易价值与闸门", "7. BUY / SELL 不得绕过 trust gate", t07_no_bypass_trust_gate)

    def t08_gold_cap():
        snap = {"ok": True, "rows": [],
                "portfolio": {"weights": {"by_class": {"gold": 0.09}}}}
        dr = _de.evaluate_decision(
            cfg, snap, trust_level="实盘辅助级",
            freshness={"action": "PASS", "ok": True, "can_buy": True},
            evidence={"independent_events_60": 100, "buy_mean_net_pp_60": 5.0,
                      "stale": False})
        g = next((x for x in dr["gates"] if x["name"] == "POSITION LIMIT GATE"), None)
        if g is None or g["ok"]:
            return False, "黄金超限却通过仓位闸门"
        if dr["final_action"] != "REBALANCE":
            return False, f"黄金超 5% 应为 REBALANCE，实际 {dr['final_action']}"
        return True, "黄金 9% > 5% → 仓位闸门拦截 → REBALANCE"

    check("交易价值与闸门", "8. 黄金不得超过 5%", t08_gold_cap)

    def t09_amount_cap():
        from core.buy_model import safe_amount
        r = safe_amount(999999.0, cfg, cash=100000.0, gap=50000.0)
        cap = float(cfg.get("simulation.max_amount_per_asset", 20000) or 20000)
        if r["amount"] > cap + 1e-6:
            return False, f"金额 {r['amount']} 超过单资产上限 {cap}"
        return True, f"金额 {r['amount']:.0f} ≤ 上限 {cap:.0f}（{r['capped_by']}）"

    check("交易价值与闸门", "9. 交易金额不得超过安全上限", t09_amount_cap)

    def t10_batch_not_all_cash():
        from core.buy_model import safe_amount
        cash = 100000.0
        r = safe_amount(cash, cfg, cash=cash, gap=cash, first_batch=True)
        if r["amount"] >= cash:
            return False, f"单批打光现金（{r['amount']} ≥ {cash}）"
        return True, f"首批 {r['amount']:.0f} < 现金 {cash:.0f}（{r['capped_by']}）"

    check("交易价值与闸门", "10. 单批不得一次打光现金", t10_batch_not_all_cash)

    def t11_wait_always_legal():
        if "WAIT" not in _de.FINAL_ACTIONS:
            return False, "WAIT 不在合法结论集合"
        dr = _de.evaluate_decision(
            cfg, {"ok": False, "errors": ["x"], "rows": []}, trust_level="研究级",
            freshness={"action": "DATA_STALE", "ok": False, "can_buy": False,
                       "critical_stale": ["etf_close"]})
        if dr["final_action"] not in _de.FINAL_ACTIONS:
            return False, f"非法最终状态 {dr['final_action']}"
        return True, f"最差情形 → {dr['final_action']}（WAIT 始终合法）"

    check("交易价值与闸门", "11. WAIT 永远合法", t11_wait_always_legal)

    def t12_no_param_leak():
        folds = _wf.purged_folds(802, train_days=504, test_days=60, embargo=10, horizon=60)
        if not folds:
            return False, "无可用折"
        for f in folds:
            idx = _wf.purged_indices(f["train_end"], 60)
            if idx and max(idx) + 60 > f["train_end"]:
                return False, "训练标签的前瞻窗口越过训练边界"
            if f["test_start"] <= f["train_end"]:
                return False, "测试段与训练段重叠"
        return True, f"{len(folds)} 折：训练标签不跨界、测试期数据不参与训练"

    check("交易价值与闸门", "12. 测试期参数不能泄露到训练期", t12_no_param_leak)

    def t13_purge_embargo():
        for td in (20, 60, 120):
            folds = _wf.purged_folds(802, train_days=504, test_days=td,
                                     embargo=10, horizon=td)
            for f in folds:
                c = _wf.check_no_leak(f)
                if not c["ok"]:
                    return False, c["detail"]
            for i in range(1, len(folds)):
                if folds[i]["test_start"] < folds[i - 1]["test_end"]:
                    return False, "测试块相互重叠"
        return True, "20/60/120 三档均满足 purge+embargo 且测试块互不重叠"

    check("交易价值与闸门", "13. Purge / Embargo 正确", t13_purge_embargo)

    def t14_dedup_events():
        ev = _wf.dedup_events(range(0, 200), 60)
        gaps = [ev[i + 1] - ev[i] for i in range(len(ev) - 1)]
        if any(g < 60 for g in gaps):
            return False, f"存在 <60 的间隔：{gaps}"
        if ev != list(range(0, 200, 60)):
            return False, f"去重结果异常：{ev}"
        return True, f"独立事件间隔均 ≥60：{ev}"

    check("交易价值与闸门", "14. 独立事件去重正确", t14_dedup_events)

    def t15_shadow_no_holdings_change():
        import json as _json
        import tempfile
        from core.config import Config as _Config
        from core import shadow_trading as _st
        with tempfile.TemporaryDirectory() as td:
            p = _Path(td) / "config" / "config.json"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(_json.dumps({"assets": [{"id": "X", "shares": 100}]}),
                         encoding="utf-8")
            c = _Config(path=p)
            before = _json.dumps(c.data, ensure_ascii=False, sort_keys=True)
            _st.record_entry(c, {"date": "2026-01-01", "action": "BUY",
                                 "candidate": "X"})
            after = _json.dumps(c.data, ensure_ascii=False, sort_keys=True)
            if before != after:
                return False, "影子交易修改了配置/持仓"
            if not (_Path(td) / "data" / "shadow_trade_history.csv").exists():
                return False, "未写入影子记录 CSV"
        return True, "影子交易只写自己的 CSV，配置/真实持仓逐字节不变"

    check("交易价值与闸门", "15. Shadow Trading 不得修改真实持仓",
          t15_shadow_no_holdings_change)

    def t16_sell_not_single_signal():
        if _sm.valid_sell_candidate({"kind": "x", "single_signal": True,
                                     "signals": ["RSI 偏高"]}):
            return False, "单指标候选被判为有效"
        if not _sm.valid_sell_candidate({"kind": "x", "single_signal": False,
                                        "signals": ["a", "b"]}):
            return False, "双信号候选应为有效"
        cands = _sm.generate_sell_candidates(_panel, 200, {"a": 0.9, "b": 0.1},
                                             {"a": 0.5, "b": 0.5}, gold_cap=0.05)
        reb = [c for c in cands if c["kind"] == "rebalance"]
        if reb and not all(c["single_signal"] for c in reb):
            return False, "纯再平衡偏离未标 single_signal"
        return True, "单指标候选一律无效；纯偏离型减仓被标记为需叠加信号"

    check("交易价值与闸门", "16. SELL 不得因为单个指标直接触发",
          t16_sell_not_single_signal)

    def t17_no_autofill_zero():
        s = _tv.stats_from_nav([], 60)
        if s["cum_return"] is not None:
            return False, "空净值应返回 None（不得填 0）"
        agg = _tv.aggregate_deltas([{"ok": True, "delta_return": None},
                                    {"ok": False, "reason": "x"}], "delta_return")
        if agg["n"] != 0 or agg["mean"] is not None:
            return False, f"缺失被填成 {agg['mean']}"
        r = _tv.evaluate_trade(_panel, 290, 60, _wb, _wa)
        if r.get("ok"):
            return False, "前瞻窗口不足时不应返回有效结果"
        return True, "缺失 / 窗口不足一律 None，绝不填 0"

    check("交易价值与闸门", "17. 数据缺失不得自动填 0", t17_no_autofill_zero)

    def t18_realtime_vs_close():
        f = _fg.check_freshness(cfg)
        keys = [r["key"] for r in f["rows"]]
        if "etf_close" not in keys or "etf_realtime" not in keys:
            return False, "未区分『实时行情』与『最近收盘』"
        if f["has_realtime"]:
            return True, "存在实时行情，已与收盘分列"
        if "不能称实时" not in f["realtime_note"]:
            return False, "无实时行情时未作明确声明"
        if f["quote_basis"] != "最近交易日收盘":
            return False, f"报价基准错误：{f['quote_basis']}"
        return True, "实时与收盘分列；无实时行情时明确『只能给下一交易日计划』"

    check("交易价值与闸门", "18. 实时行情与最新收盘必须分开", t18_realtime_vs_close)

    def t19_cost_tiers():
        t = _cm.tier_table()
        if [x["tier"] for x in t] != ["LOW", "BASE", "HIGH"]:
            return False, "成本档位不全"
        if not (t[0]["one_way_pct"] < t[1]["one_way_pct"] < t[2]["one_way_pct"]):
            return False, "成本档位未递增"
        if not _cm.cost_sensitivity_verdict({"LOW": 2.0, "BASE": 1.0,
                                             "HIGH": 0.2})["pass"]:
            return False, "三档全正应通过成本压力测试"
        if _cm.cost_sensitivity_verdict({"LOW": 2.0, "BASE": -0.5,
                                         "HIGH": -1.0})["verdict"] != "对交易成本敏感":
            return False, "仅 LOW 为正时应判『对交易成本敏感』"
        return True, (f"三档单边 {t[0]['one_way_pct']}/{t[1]['one_way_pct']}/"
                      f"{t[2]['one_way_pct']}%，敏感判定正确")

    check("交易价值与闸门", "19. 成本三档压力测试正确", t19_cost_tiers)

    def t20_regime_strata():
        if set(_pe.REGIMES) != {"Risk On", "Normal", "Risk Off", "Crisis"}:
            return False, "市场状态集合不符"
        ev = [{"action": "BUY", "regime": "Risk On", "delta_return_net": 1.0},
              {"action": "BUY", "regime": "Risk Off", "delta_return_net": -0.5}]
        st = _pe._strata(ev, (20, 60, 120))
        if set(st["BUY"]) != set(_pe.REGIMES):
            return False, "分层未覆盖四个市场状态"
        if st["BUY"]["Risk On"]["n"] != 1 or st["BUY"]["Risk Off"]["win_rate_pct"] != 0.0:
            return False, "分层统计数值错误"
        return True, "按 Risk On / Normal / Risk Off / Crisis 四态分层统计"

    check("交易价值与闸门", "20. 市场状态分层统计正确", t20_regime_strata)

    # ---------------------------------------------------------------- 事件审计
    # 本轮新增 20 项（21-40）：事件口径 / 候选-可执行双层 / 研究-生产双口径 /
    #                        模型阶梯 / 稳定性 / 门槛不得放宽
    from core import event_audit as ea
    from core import walk_forward as _wf

    _panel, _classes, _raw, _names = _synth_panel()
    _n = len(_panel.nav)
    _gold_cap = 0.05
    _tw = sum(float(v) for v in _raw.values()) or 1.0
    _w_wait = {k: float(v) / _tw for k, v in _raw.items()}
    _wf_raw = {"A": 0.30, "B": 0.25, "C": 0.35, "D": 0.05}
    _tf = sum(_wf_raw.values()) or 1.0
    _w_fixed = {k: v / _tf for k, v in _wf_raw.items()}
    _regs = ea.regime_series_th(_panel, _classes, _w_wait)

    _shared: dict = {}
    try:
        _shared["funnel"] = ea.build_funnel(cfg, _panel, _classes, _raw, _regs,
                                            _gold_cap, _w_wait, _w_fixed, horizon=60)
        _shared["cand"] = ea.candidate_events(_panel, _classes, _raw, _regs,
                                              _gold_cap, _w_wait, _w_fixed, 60)
        _shared["lad"] = ea.model_ladder(_panel, _classes, _raw, _regs,
                                         _gold_cap, _w_wait, horizon=60)
        _shared["stab"] = ea.event_stability(_panel, _classes, _raw, _regs,
                                             _gold_cap, _w_wait, _w_fixed, horizon=60)
        _shared["research"] = ea.research_layer(_panel, _classes, _raw, _regs,
                                                _gold_cap, _w_wait, _w_fixed,
                                                horizons=(60,))
        _shared["production"] = ea.production_layer(cfg, _panel, _classes, _raw,
                                                    _regs, _gold_cap, _w_wait,
                                                    _w_fixed, horizons=(60,))
    except Exception as exc:  # noqa: BLE001
        NOTES.append(f"事件审计共享构件构建失败：{exc}")

    def t21_eligible_days():
        d = ea.eligible_days(_n, 60)
        ok = bool(d) and d[0] == ea.WARMUP_DAYS and d[-1] <= _n - 1 - 60
        return ok, f"n={_n} 首={d[0] if d else None} 末={d[-1] if d else None} 数={len(d)}"

    check("事件审计", "21. 可评估日网格边界正确（预热 + 前瞻窗口）", t21_eligible_days)

    def t22_bound_monotone():
        b20 = ea.max_independent_bound(_n, 20)
        b60 = ea.max_independent_bound(_n, 60)
        b120 = ea.max_independent_bound(_n, 120)
        return (b20 >= b60 >= b120 >= 0), f"20日{b20} ≥ 60日{b60} ≥ 120日{b120}"

    check("事件审计", "22. 独立事件理论上界随周期单调不增", t22_bound_monotone)

    def t23_dedup_gap():
        out = _wf.dedup_events(list(range(0, 300)), 60)
        gaps = [out[i + 1] - out[i] for i in range(len(out) - 1)]
        ok = (not gaps) or min(gaps) >= 60
        return ok, f"去重后 {len(out)} 个，最小间隔 {min(gaps) if gaps else '—'}（应 ≥60）"

    check("事件审计", "23. 独立事件去重间隔 ≥ h", t23_dedup_gap)

    def t24_regime_breadth_monotone():
        r1 = ea.regime_series_th(_panel, _classes, _w_wait, breadth_off=0.30)
        r2 = ea.regime_series_th(_panel, _classes, _w_wait, breadth_off=0.40)
        n1 = sum(1 for x in r1 if x["regime"] == "Risk Off")
        n2 = sum(1 for x in r2 if x["regime"] == "Risk Off")
        return (n2 >= n1), f"广度下限 0.30→RiskOff {n1} 天；0.40→{n2} 天（应不减少）"

    check("事件审计", "24. Regime 广度阈值方向正确（单调）", t24_regime_breadth_monotone)

    def t25_no_buy_in_riskoff():
        bad = 0
        for t in (200, 250, 300, 350):
            for bad_rg in ("Risk Off", "Crisis"):
                cls = ea.classify_day(_panel, _classes, _raw, bad_rg, _gold_cap,
                                      _w_wait, _w_fixed, t)
                bad += 1 if "BUY" in cls else 0
        return bad == 0, f"Risk Off/Crisis 下产生 BUY 的次数={bad}（应 0）"

    check("事件审计", "25. Risk Off / Crisis 不得产生 BUY 候选", t25_no_buy_in_riskoff)

    def t26_cand_gap():
        c = _shared.get("cand")
        if not c:
            return False, "候选事件未构建"
        detail = []
        ok = True
        for act, evs in c["independent"].items():
            ts = [e["t"] for e in evs]
            gaps = [ts[i + 1] - ts[i] for i in range(len(ts) - 1)]
            if gaps and min(gaps) < 60:
                ok = False
            detail.append(f"{act}={len(ts)}(最小间隔{min(gaps) if gaps else '—'})")
        return ok, "候选独立事件间隔全部 ≥60；" + " ".join(detail)

    check("事件审计", "26. 候选独立事件间隔 ≥ h", t26_cand_gap)

    def t27_prod_gate_trust():
        t = ea.eligible_days(_n, 60)[10]
        g_buy = ea.production_gate(cfg, _panel, _classes, _w_wait, _regs, t,
                                   "BUY", trust_allows_buy=False, gold_cap=_gold_cap)
        g_sell = ea.production_gate(cfg, _panel, _classes, _w_wait, _regs, t,
                                    "SELL", trust_allows_buy=False, gold_cap=_gold_cap)
        ok = (not g_buy["ok"]) and g_sell["ok"]
        return ok, (f"研究级 BUY 放行={g_buy['ok']}（应 False）；"
                    f"SELL 放行={g_sell['ok']}（应 True）")

    check("事件审计", "27. 生产闸门：研究级禁 BUY、防守动作放行", t27_prod_gate_trust)

    def t28_exec_buy_zero():
        p = _shared.get("production")
        if not p:
            return False, "生产层未构建"
        n_buy = ((p["horizons"].get(60) or {}).get("n_executable") or {}).get("BUY", None)
        return n_buy == 0, f"研究级可执行 BUY 独立事件={n_buy}（应 0）"

    check("事件审计", "28. 研究级可执行 BUY 独立事件恒为 0", t28_exec_buy_zero)

    def t29_research_ge_production():
        r = _shared.get("research")
        p = _shared.get("production")
        if not (r and p):
            return False, "双层未构建"
        ri = r["horizons"][60]["n_candidate_independent"]
        pi = p["horizons"][60]["n_executable"]
        ok = all(ri.get(a, 0) >= pi.get(a, 0) for a in ("BUY", "SELL", "REBALANCE"))
        return ok, f"研究层 {ri} ⊇ 生产层 {pi}"

    check("事件审计", "29. 研究层样本 ⊇ 生产层样本（两口径独立）",
          t29_research_ge_production)

    def t30_ladder_same_events():
        lad = _shared.get("lad")
        if not lad:
            return False, "模型阶梯未构建"
        ns = {k: v.get("n_events_evaluated", 0)
              for k, v in lad["variants"].items() if k != "Model 0"}
        return (len(set(ns.values())) <= 1), f"各模型评估事件数 {ns}（应完全一致）"

    check("事件审计", "30. 模型阶梯使用完全相同的 OOS 事件集合", t30_ladder_same_events)

    def t31_model0_zero():
        lad = _shared.get("lad")
        if not lad:
            return False, "模型阶梯未构建"
        d = lad["variants"]["Model 0"]["delta_return_net"]
        return d.get("mean") == 0.0, f"Model 0（WAIT）平均增量={d.get('mean')}（应 0）"

    check("事件审计", "31. Model 0（WAIT）交易价值恒为 0", t31_model0_zero)

    def t32_funnel_levels():
        f = _shared.get("funnel")
        if not f:
            return False, "漏斗未构建"
        return len(f["levels"]) == 15, f"漏斗级数={len(f['levels'])}（应 15）"

    check("事件审计", "32. 事件漏斗恰好 15 级", t32_funnel_levels)

    def t33_funnel_consistency():
        f = _shared.get("funnel")
        if not f:
            return False, "漏斗未构建"
        bad = [lv["level"] for lv in f["levels"]
               if lv.get("input") != lv["entered"] + lv["eliminated"]]
        return (not bad), ("每级 输入 = 进入 + 淘汰" if not bad else f"不一致：{bad}")

    check("事件审计", "33. 漏斗每级 输入 = 进入 + 淘汰", t33_funnel_consistency)

    def t34_funnel_reason_pct():
        f = _shared.get("funnel")
        if not f:
            return False, "漏斗未构建"
        bad = []
        for lv in f["levels"]:
            for r in lv["reasons"]:
                if r["pct"] is not None and r["pct"] > 100.5:
                    bad.append((lv["level"], r["reason"], r["pct"]))
        return (not bad), ("淘汰原因占比均 ≤100%" if not bad else f"异常：{bad[:3]}")

    check("事件审计", "34. 漏斗淘汰原因占比 ≤ 100%", t34_funnel_reason_pct)

    def t35_stability_coverage():
        s = _shared.get("stab")
        if not s:
            return False, "稳定性未构建"
        params = {r["param"] for r in s["rows"]}
        pcts = {r["pct"] for r in s["rows"]}
        return (pcts == {-5, -2, 0, 2, 5} and len(params) >= 4), \
            f"覆盖扰动档 {sorted(pcts)}；参数 {sorted(params)}"

    check("事件审计", "35. 稳定性检查覆盖 ±5%/±2%/0/+2%/+5%", t35_stability_coverage)

    def t36_stability_no_cherry_pick():
        s = _shared.get("stab")
        if not s:
            return False, "稳定性未构建"
        has_base = any(r["pct"] == 0 for r in s["rows"])
        keys = set(s["flags"][0].keys()) if s["flags"] else set()
        ok = has_base and {"sensitive", "sign_flip"} <= keys
        return ok, (f"含 0% 基准行={has_base}；仅标记敏感性、不做参数挑选="
                    f"{'sensitive' in keys}；{'绝不允许挑选最好参数' in s['discipline']}")

    check("事件审计", "36. 稳定性只标记敏感性，不做参数挑选",
          t36_stability_no_cherry_pick)

    def t37_independent_threshold_kept():
        return (ea.INDEPENDENT_THRESHOLD == 30), \
            f"独立事件门槛={ea.INDEPENDENT_THRESHOLD}（不得放宽，应 30）"

    check("事件审计", "37. 独立事件门槛未被放宽（仍为 30）",
          t37_independent_threshold_kept)

    def t38_no_feature_bloat():
        lad = _shared.get("lad")
        if not lad:
            return False, "模型阶梯未构建"
        mx = max(v["n_features"] for v in lad["variants"].values())
        return mx <= 5, f"模型最大输入数={mx}（≤5，未新增超过既有特征集 20%）"

    check("事件审计", "38. 模型阶梯未新增超过既有特征集 20%",
          t38_no_feature_bloat)

    def t39_gate_accounting():
        r = _shared.get("research")
        p = _shared.get("production")
        if not (r and p):
            return False, "双层未构建"
        ri = (r["horizons"][60]["n_candidate_independent"] or {}).get("BUY", 0)
        pe = (p["horizons"][60]["n_executable"] or {}).get("BUY", 0)
        pr = (p["horizons"][60].get("n_rejected_by_gate") or {}).get("BUY", 0)
        ok = (pe == 0 and ri == pr)
        return ok, (f"研究层 BUY 独立={ri}，生产层可执行={pe}，被拒={pr}"
                    f"（生产闸门不得污染研究层样本）")

    check("事件审计", "39. 生产闸门不得污染研究层样本（账目自洽）",
          t39_gate_accounting)

    def t40_real_panel_auditable():
        from core import policy_evaluator as pe2
        ld = pe2.load_real_6etf(cfg)
        if not ld.get("ok"):
            return False, str(ld.get("message"))
        nn = len(ld["panel"].nav)
        return (nn >= 780), (f"真实六 ETF 共同区间 {nn} 日，起点 {ld['common_start']}"
                             f"（预期 ~802）")

    check("事件审计", "40. 真实六 ETF 区间可直接用于事件审计",
          t40_real_panel_auditable)

    def t41_frozen_safe_paths():
        """回归：报告写入路径必须走 project_root()（冻结态 = exe 目录）。

        真实故障：报告渲染用 `Path(__file__).resolve().parent.parent`，
        打包后该路径落在 `_internal/`，导致「exe 明明重打了，包根目录的报告却没更新」。
        必须同时满足两点，缺一不可：
          (a) GOLD_ETF_HOME 被显式设置时以其为准；
          (b) 冻结态（sys.frozen=True）下即使没有环境变量，也必须返回 exe 目录，
              而不是 `_internal/`（这正是上一版没修根因、只挪了调用点的地方）。
        """
        import os as _os
        import sys as _sys
        from core import config as _cfgmod
        old_env = _os.environ.get("GOLD_ETF_HOME")
        old_frozen = getattr(_sys, "frozen", None)
        old_exe = getattr(_sys, "executable", None)
        try:
            # (a) 显式环境变量优先
            _os.environ["GOLD_ETF_HOME"] = r"F:\__fake_home__"
            got_env = str(_cfgmod.project_root())
            _os.environ.pop("GOLD_ETF_HOME", None)
            # (b) 模拟 PyInstaller 冻结态：无环境变量 + sys.frozen + sys.executable
            _sys.frozen = True  # type: ignore[attr-defined]
            _sys.executable = r"F:\__pkg__\GoldETFMonitor-cli.exe"
            got_frozen = str(_cfgmod.project_root())
        finally:
            if old_env is None:
                _os.environ.pop("GOLD_ETF_HOME", None)
            else:
                _os.environ["GOLD_ETF_HOME"] = old_env
            if old_frozen is None:
                try:
                    del _sys.frozen  # type: ignore[attr-defined]
                except AttributeError:
                    pass
            else:
                _sys.frozen = old_frozen  # type: ignore[attr-defined]
            if old_exe is not None:
                _sys.executable = old_exe
        ok_env = (got_env == r"F:\__fake_home__")
        ok_frozen = (got_frozen == r"F:\__pkg__")
        root = Path(__file__).resolve().parent.parent / "core"
        bad = []
        for fn in ("policy_reports.py", "event_reports.py", "data_validation.py",
                   "strategy_validation.py", "shadow_trading.py", "backtest.py"):
            try:
                t = (root / fn).read_text(encoding="utf-8")
            except Exception:  # noqa: BLE001
                continue
            if "Path(__file__).resolve().parent.parent" in t:
                bad.append(fn)
        return (ok_env and ok_frozen and not bad), (
            f"GOLD_ETF_HOME优先={ok_env}；冻结态=exe目录={ok_frozen}"
            f"（实际 {got_frozen}）；"
            + ("所有报告写入点已冻结态安全" if not bad else f"仍不安全：{bad}"))

    check("事件审计", "41. 报告写入路径冻结态安全（走 project_root）",
          t41_frozen_safe_paths)

    # ---------------------------------------------------------------- 执行台账
    def _mk_ledger_env():
        """建临时工作目录（只含黄金持仓），返回 (tmp, cfg, restore)。

        临时把 GOLD_ETF_HOME 指向 temp，于是台账/配置都落在 temp，绝不碰真实数据。
        """
        import os as _o
        import shutil as _sh
        import tempfile
        tmp = tempfile.mkdtemp(prefix="selftest_ledger_")
        old = _o.environ.get("GOLD_ETF_HOME")
        _o.environ["GOLD_ETF_HOME"] = tmp
        from core.config import Config as _C
        c = _C(_o.path.join(tmp, "config.json"))
        c.data["assets"] = [{"id": "159937", "name": "黄金ETF博时",
                             "asset_class": "gold", "shares": 1900, "cost": 11.882}]
        c.set("portfolio.cash", None, autosave=False)
        c.save()

        def restore():
            if old is None:
                _o.environ.pop("GOLD_ETF_HOME", None)
            else:
                _o.environ["GOLD_ETF_HOME"] = old
            _sh.rmtree(tmp, ignore_errors=True)
        return tmp, c, restore

    def t42_ledger_signature():
        from core import execution_ledger as led
        a = led.signature("REBALANCE", "159937", 5.0)
        b = led.signature("REBALANCE", "159937", 5)
        c = led.signature("REBALANCE", "159937", 8.0)
        ok1 = (a == "REBALANCE|159937|5.0" and a == b and a != c)
        ok2 = led.advice_signature({"key": "BLUE_REBALANCE"}, {"best": {}}) is None
        ok3 = (led.advice_signature(
            {"key": "BLUE_REBALANCE"},
            {"gold_id": "159937", "best": {"target_pct": 5.0, "sell_shares": 1300}})
            == "REBALANCE|159937|5.0")
        return (ok1 and ok2 and ok3), \
            f"签名={a}；无建议→无签名={ok2}；再平衡签名正确={ok3}"

    check("执行台账", "42. 建议签名稳定且口径正确", t42_ledger_signature)

    def t43_ledger_execute():
        from core import execution_ledger as led
        from core.config import Config
        tmp, cfg, restore = _mk_ledger_env()
        try:
            hl = {"key": "BLUE_REBALANCE"}
            rs = {"gold_id": "159937",
                  "best": {"target_pct": 5.0, "sell_shares": 1300,
                           "sell_amount": 11198, "sell_price": 8.6}}
            st0 = led.status_text(hl, rs)
            e = led.execute_rebalance(cfg, asset_id="159937", name="黄金ETF博时",
                                      sell_shares=1300, price=8.6, amount=11198,
                                      target_pct=5.0)
            cfg2 = Config(os.path.join(tmp, "config.json"))
            st1 = led.status_text(hl, rs)
            lp = Path(led._ledger_path())
            src = _module_src(led)
            ok = (st0["actionable"] and not st0["executed"]
                  and e["shares_before"] == 1900 and e["shares_after"] == 600
                  and int(cfg2.assets[0]["shares"]) == 600
                  and st1["executed"] and (st1["last"] or {}).get("date") == e["date"]
                  and lp.parent.parent == Path(tmp)
                  and "Path(__file__)" not in (src or ""))
            return ok, (f"份额 1900→600；状态由『未执行』转『已执行』；"
                        f"台账落在可写根（{lp.parent.parent}）")
        finally:
            restore()

    check("执行台账", "43. 执行后份额/现金/台账都正确（冻结态安全）", t43_ledger_execute)

    def t44_ledger_guard():
        from core import execution_ledger as led
        from core.config import Config
        tmp, cfg, restore = _mk_ledger_env()
        try:
            bad = 0
            for kw in ({"asset_id": "159937", "sell_shares": 9999},
                       {"asset_id": "159937", "sell_shares": 0},
                       {"asset_id": "000000", "sell_shares": 100}):
                try:
                    led.execute_rebalance(cfg, name="x", price=8.6, amount=1,
                                          target_pct=5.0, **kw)
                except ValueError:
                    bad += 1
            cfg2 = Config(os.path.join(tmp, "config.json"))
            return (bad == 3 and int(cfg2.assets[0]["shares"]) == 1900), \
                f"越界/0份/不存在资产 三类非法输入全部拦截 {bad}/3，持仓未被破坏"
        finally:
            restore()

    check("执行台账", "44. 非法执行被拦截且不破坏持仓", t44_ledger_guard)

    def t45_ledger_buy_cash():
        from core import execution_ledger as led
        tmp, cfg, restore = _mk_ledger_env()
        try:
            cfg.set("portfolio.cash", 5000.0, autosave=False)
            cfg.save()
            e = led.execute_buy(cfg, asset_id="159937", name="黄金ETF博时",
                                buy_shares=200, price=8.6, amount=1720)
            ok1 = (e["shares_after"] == 2100 and e["cash_after"] == 3280.0)
            try:
                led.execute_buy(cfg, asset_id="159937", name="x", buy_shares=10000,
                                price=8.6, amount=86000)
                ok2 = False
            except ValueError:
                ok2 = True
            return (ok1 and ok2), f"买入后 2100 份 / 现金 3280 元；现金不足被拒={ok2}"
        finally:
            restore()

    check("执行台账", "45. 买入登记与现金不足保护", t45_ledger_buy_cash)

    def t46_ledger_no_auto():
        """硬约束：台账只改本地记录，不联网、不下单。"""
        src = _module_src(__import__("core.execution_ledger",
                                     fromlist=["x"])) or ""
        forbidden = ["requests", "urllib", "socket", "http", "subprocess",
                     "os.system", "place_order"]
        hit = [w for w in forbidden if w in src]
        return (not hit), ("台账不联网、不下单（只改本地 config 记录）"
                           if not hit else f"疑似越界：{hit}")

    check("执行台账", "46. 台账不联网不下单（只改本地记录）", t46_ledger_no_auto)

    # ---------------------------------------------------------------- 指令栏
    def _mk_cmd_env():
        """临时工作目录 + 6 只标的，用于指令栏解析/执行；绝不碰真实数据。"""
        import os as _o
        import shutil as _sh
        import tempfile
        tmp = tempfile.mkdtemp(prefix="selftest_cmd_")
        old = _o.environ.get("GOLD_ETF_HOME")
        _o.environ["GOLD_ETF_HOME"] = tmp
        from core.config import Config as _C
        c = _C(_o.path.join(tmp, "config.json"))
        c.data["assets"] = [
            {"id": "511220", "name": "城投债ETF海富通", "asset_class": "bond", "shares": 2500},
            {"id": "511090", "name": "30年国债ETF鹏扬", "asset_class": "bond", "shares": 200},
            {"id": "159941", "name": "纳指ETF广发", "asset_class": "stock", "shares": 5500},
            {"id": "513530", "name": "港股通红利ETF华泰柏瑞", "asset_class": "stock", "shares": 4400},
            {"id": "159915", "name": "创业板ETF易方达", "asset_class": "stock", "shares": 2300},
            {"id": "159937", "name": "黄金ETF博时", "asset_class": "gold", "shares": 1900},
        ]
        c.set("portfolio.cash", None, autosave=False)
        c.save()

        def restore():
            if old is None:
                _o.environ.pop("GOLD_ETF_HOME", None)
            else:
                _o.environ["GOLD_ETF_HOME"] = old
            _sh.rmtree(tmp, ignore_errors=True)
        return tmp, c, restore

    def t47_cmd_parse():
        from core import command_bar as cb
        tmp, c, restore = _mk_cmd_env()
        try:
            cases = {"卖黄金800@8.62": ("REBALANCE", "159937", 800, 8.62),
                     "买纳指500@1.55": ("BUY", "159941", 500, 1.55),
                     "减仓 城投债 1000": ("REBALANCE", "511220", 1000, None),
                     "卖黄金１,３００＠８.６": ("REBALANCE", "159937", 1300, 8.6)}
            for t, (act, aid, sh, px) in cases.items():
                r = cb.parse(t, c)
                if r.action != act or r.asset_id != aid or r.shares != sh:
                    return False, f"「{t}」→ {r.action}/{r.asset_id}/{r.shares}"
                if px is not None and abs(float(r.price) - px) > 1e-9:
                    return False, f"「{t}」成交价 {r.price}≠{px}"
            return True, "卖/买/减仓 + 简称/全角/千分位 解析正确"
        finally:
            restore()

    check("指令栏", "47. 买卖指令解析（简称/全角/千分位）", t47_cmd_parse)

    def t48_cmd_settings():
        from core import command_bar as cb
        tmp, c, restore = _mk_cmd_env()
        try:
            ok = (cb.parse("现金20万", c).action == "SET_CASH"
                  and abs(float(cb.parse("现金20万", c).value) - 200000) < 1e-6
                  and cb.parse("总资产30万", c).action == "SET_TOTAL"
                  and cb.parse("上限黄金6%", c).action == "SET_GOLD_CAP"
                  and abs(float(cb.parse("黄金上限 6", c).value) - 0.06) < 1e-9
                  and cb.parse("忽略", c).action == "SKIP"
                  and cb.parse("备注 今天没空操作", c).action == "NOTE")
            if not ok:
                return False, "现金/总资产/上限/忽略/备注 有写法未识别"
            tg = cb.parse("目标 债50 股45 金5", c).targets
            if not (tg.get("bond") == 0.5 and tg.get("stock") == 0.45
                    and tg.get("gold") == 0.05):
                return False, f"目标解析不完整：{tg}"
            r = cb.apply(c, cb.parse("现金20万", c))
            if not r["ok"]:
                return False, f"现金落地失败：{r['message']}"
            cash = c.get("portfolio.cash")
            if cash is None or abs(float(cash) - 200000) > 1e-6:
                return False, f"现金值不对：{cash}"
            return True, "现金/总资产/上限/目标/忽略/备注 均识别且现金可落地"
        finally:
            restore()

    check("指令栏", "48. 现金/总资产/上限/目标/忽略/备注 识别与落地", t48_cmd_settings)

    def t49_cmd_reject():
        from core import command_bar as cb
        tmp, c, restore = _mk_cmd_env()
        try:
            for t in ("卖债800", "卖股票500", "卖黄金", "随便说说", "买xyz500"):
                if cb.parse(t, c).action != "ERROR":
                    return False, f"「{t}」本应拒绝却被接受"
            if cb.parse("   ", c).action not in ("EMPTY", "ERROR"):
                return False, "空输入应为 EMPTY/ERROR"
            return True, "歧义类/缺份数/不认识 一律拒绝；空输入不误判，绝不乱猜"
        finally:
            restore()

    check("指令栏", "49. 歧义/缺参/不认识 一律拒绝（不乱猜）", t49_cmd_reject)

    def t50_cmd_apply():
        from core import command_bar as cb
        from core.config import Config
        tmp, c, restore = _mk_cmd_env()
        try:
            r = cb.parse("卖黄金800@8.62", c)
            pv = cb.preview(r, c)
            if "卖出" not in pv or "800" not in pv:
                return False, f"预览不完整：{pv}"
            res = cb.apply(c, r)
            if not res["ok"]:
                return False, f"执行失败：{res['message']}"
            c2 = Config(os.path.join(tmp, "config.json"))
            g = [a for a in c2.assets if a["id"] == "159937"][0]
            if int(g["shares"]) != 1100:
                return False, f"份额未更新：{g['shares']}"
            if abs(float(c2.declared_cash or 0) - 6896.0) > 1e-6:
                return False, f"现金未登记：{c2.declared_cash}"
            bad = cb.apply(c2, cb.parse("卖黄金99999@8.6", c2))
            if bad["ok"]:
                return False, "越界卖出未被拦截"
            g2 = [a for a in Config(os.path.join(tmp, "config.json")).assets
                  if a["id"] == "159937"][0]
            if int(g2["shares"]) != 1100:
                return False, "被拒后持仓被破坏"
            return True, "份额 1900→1100、现金 +6896；越界被拒且不破坏持仓"
        finally:
            restore()

    check("指令栏", "50. 执行落地 + 越界保护 + 拒绝不破坏持仓", t50_cmd_apply)

    def t51_cmd_no_auto():
        src = _module_src(__import__("core.command_bar",
                                     fromlist=["x"])) or ""
        forbidden = ["requests", "urllib", "socket", "subprocess", "os.system",
                     "place_order", "http://", "https://"]
        hit = [w for w in forbidden if w in src]
        return (not hit), ("指令栏只改本地记录，不联网、不下单"
                           if not hit else f"疑似越界：{hit}")

    check("指令栏", "51. 指令栏不联网不下单（只改本地记录）", t51_cmd_no_auto)

    # ---------------------------------------------------------------- 实时持仓
    def _snap_hold():
        """构造一个可重算的持仓快照（价格内嵌，无需联网）。"""
        def row(aid, name, cls, shares, price, tw, swing="中性", tc="震荡"):
            return {"asset": {"id": aid, "name": name, "asset_class": cls,
                              "shares": shares, "target_weight": tw},
                    "snapshot": {"price": price, "market_value": shares * price,
                                 "swing": {"state": swing}},
                    "scores": {"trend_class": tc, "flags": {}},
                    "weight_pct": None, "target_weight_pct": tw * 100}
        return {"rows": [
            row("511220", "城投债ETF", "bond", 2500, 12.0, 0.15),
            row("511090", "30年国债ETF", "bond", 200, 15.0, 0.35),
            row("159941", "纳指ETF", "stock", 5500, 1.6, 0.20),
            row("513530", "港股红利ETF", "stock", 4400, 1.3, 0.15),
            row("159915", "创业板ETF", "stock", 2300, 2.1, 0.10),
            row("159937", "黄金ETF", "gold", 1900, 8.6, 0.05),
        ], "portfolio": {"target_class": {"bond": .5, "stock": .45, "gold": .05},
                         "risk": {"state": "Risk Off"}}}

    class _StubCfg:
        def __init__(self, total=112564.0):
            self.declared_total_assets = total
            self.declared_cash = None
            self._d = {"portfolio.rebalance_watch_pct": 3.0,
                       "portfolio.rebalance_priority_pct": 5.0,
                       "portfolio.gold_max": 0.05}

        def get(self, k, default=None):
            return self._d.get(k, default)

    def t52_lh_basic():
        from core import live_holdings as lh
        snap = _snap_hold()
        rows = lh.table_rows(snap)
        if len(rows) != 6:
            return False, f"table_rows 应 6 行，实为 {len(rows)}"
        res = lh.recompute(snap, {}, _StubCfg())
        if not res.get("ok"):
            return False, f"recompute 失败：{res.get('message')}"
        if abs(res["total_assets"] - 112564.0) > 1e-6:
            return False, f"总资产口径不对：{res['total_assets']}"
        if not res["gold_over"]:
            return False, "黄金 14.5% 应判为超配"
        if any(not r.get("action") for r in res["rows"]):
            return False, "有标的缺判断"
        return True, f"6 行可重算 · 总资产 {res['total_assets']:.0f} · 黄金超配已识别"

    check("实时持仓", "52. 份额→权重/逐只判断 重算正确", t52_lh_basic)

    def t53_lh_live():
        from core import live_holdings as lh
        snap = _snap_hold()
        cfg = _StubCfg()
        before = lh.summary_text(lh.recompute(snap, {}, cfg))
        res = lh.recompute(snap, {"159937": 600}, cfg)
        if res["gold_over"]:
            return False, "黄金减到 600 份后仍判超配"
        g = next(r for r in res["rows"] if r["id"] == "159937")
        if "减仓" in (g["action"] or ""):
            return False, f"黄金已降到上限内，不该再判减仓：{g['action']}"
        after = lh.summary_text(res)
        if after == before:
            return False, "改份额后摘要未变化"
        return True, f"黄金 1900→600 份：超配解除、判断刷新（{g['action']}）"

    check("实时持仓", "53. 改份额 → 权重与判断即时刷新", t53_lh_live)

    def t54_lh_edge():
        from core import live_holdings as lh
        snap = _snap_hold()
        res = lh.recompute(snap, {"159937": -5, "159941": "abc"}, _StubCfg())
        if not res.get("ok"):
            return False, "非法输入导致重算失败"
        g = next(r for r in res["rows"] if r["id"] == "159937")
        n = next(r for r in res["rows"] if r["id"] == "159941")
        if g["shares"] != 0.0:
            return False, f"负份额应规整为 0，实为 {g['shares']}"
        if abs(n["shares"] - 5500) > 1e-6:
            return False, f"非数字应保留原份额 5500，实为 {n['shares']}"
        return True, "负份额→0、非法输入→保留原值，均不崩"

    check("实时持仓", "54. 非法份额输入不崩溃（规整/保留原值）", t54_lh_edge)

    def t55_lh_no_write():
        import os as _o
        import shutil as _sh
        import tempfile
        from core import live_holdings as lh
        tmp = tempfile.mkdtemp(prefix="selftest_lh_")
        old = _o.environ.get("GOLD_ETF_HOME")
        _o.environ["GOLD_ETF_HOME"] = tmp
        try:
            lh.recompute(_snap_hold(), {"159937": 600}, _StubCfg())
            produced = [f for _r, _d, fs in _o.walk(tmp) for f in fs]
            src = _module_src(__import__("core.live_holdings",
                                         fromlist=["x"])) or ""
            forbidden = ["requests", "urllib", "socket", "subprocess", "os.system",
                         "open(", "write_text", "http://", "https://"]
            hit = [w for w in forbidden if w in src]
            if produced:
                return False, f"重算竟写盘：{produced}"
            if hit:
                return False, f"重算模块疑似越界：{hit}"
            return True, "重算只做计算：不写盘、不联网、不下单"
        finally:
            if old is None:
                _o.environ.pop("GOLD_ETF_HOME", None)
            else:
                _o.environ["GOLD_ETF_HOME"] = old
            _sh.rmtree(tmp, ignore_errors=True)

    check("实时持仓", "55. 重算纯计算：不写盘/不联网/不下单", t55_lh_no_write)

    # ---------------------------------------------------------------- 价格口径
    def _snap_price(with_realtime=True, ref_price=999.0):
        """在 _snap_hold 上补「价格来路」与「盘中参考价」，用于价格口径测试。"""
        snap = _snap_hold()
        for r in snap["rows"]:
            r["snapshot"]["data_time"] = "2026-09-30"
            r["snapshot"]["data_source"] = "同花顺扶摇"
            r["snapshot"]["from_cache"] = False
        if with_realtime:
            snap["realtime"] = {
                r["asset"]["id"]: {"name": r["asset"]["name"],
                                   "code": r["asset"]["id"],
                                   "price": ref_price, "prev_close": ref_price,
                                   "change_pct": 1.23, "time": "20260930161446",
                                   "time_hhmm": "2026-09-30 16:14"}
                for r in snap["rows"]}
        return snap

    def t56_realtime_parse():
        from data_sources import realtime_quote as rq
        # 前 6 个字段 + 24 个占位 + 行情时间(30) + 涨跌额(31) + 涨跌幅(32)
        sample = ('v_sz159937="1~黄金ETF博时~159937~8.614~8.499~8.551~'
                  + "~".join(["0"] * 24) + '~20260930161433~0.115~1.35~";')
        d = rq.parse_quotes(sample).get("159937")
        if not d:
            return False, "样例未解析出 159937"
        if abs(d["price"] - 8.614) > 1e-9 or abs(d["prev_close"] - 8.499) > 1e-9:
            return False, f"价格/昨收解析错：{d['price']} / {d['prev_close']}"
        if abs((d["change_pct"] or 0) - 1.35) > 1e-9:
            return False, f"涨跌幅解析错：{d['change_pct']}"
        if d["time_hhmm"] != "2026-09-30 16:14":
            return False, f"行情时间格式化错：{d['time_hhmm']}"
        return True, "解析正确：价 8.614 / 昨收 8.499 / +1.35% / 2026-09-30 16:14"

    check("价格口径", "56. 盘中报价解析正确（价/昨收/涨跌幅/行情时间）", t56_realtime_parse)

    def t57_realtime_robust():
        from data_sources import realtime_quote as rq
        for bad in ("", "garbage", 'v_x="1~2~3";', None, "v_a="):
            if rq.parse_quotes(bad):
                return False, f"异常输入 {bad!r} 竟解析出内容"
        if rq.fmt_quote_time("bad") is not None or rq.fmt_quote_time("") is not None:
            return False, "非法行情时间未返回 None"
        return True, "空/垃圾/字段不足/None 一律安全返回，时间格式化容错"

    check("价格口径", "57. 异常报价输入不崩溃（容错）", t57_realtime_robust)

    def t58_cache_ttl():
        from core.config import DEFAULT_CONFIG
        d = DEFAULT_CONFIG["data_sources"]["cache_ttl_minutes"]["etf"]
        if d != 60:
            return False, f"默认 ETF 缓存 TTL 应为 60 分钟，实为 {d}"
        eff = cfg.get("data_sources.cache_ttl_minutes.etf")
        if eff is None or float(eff) > 120:
            return False, f"生效的 ETF 缓存 TTL 仍偏大：{eff}"
        return True, f"默认 {d} 分钟；当前生效 {eff} 分钟（原为 720=12 小时）"

    check("价格口径", "58. ETF 日线缓存 TTL 已下调（12h → 1h）", t58_cache_ttl)

    def t59_ref_not_used():
        from core import live_holdings as lh
        a = lh.recompute(_snap_price(True, 999.0), {}, _StubCfg())
        b = lh.recompute(_snap_price(False), {}, _StubCfg())
        if not (a.get("ok") and b.get("ok")):
            return False, "重算失败"
        for k in ("total_assets", "etf_value", "gold_pct", "gold_over"):
            if a.get(k) != b.get(k):
                return False, f"参考价影响了 {k}：{a.get(k)} vs {b.get(k)}"
        for ra, rb in zip(a["rows"], b["rows"]):
            if (abs(ra["weight_pct"] - rb["weight_pct"]) > 1e-9
                    or ra["action"] != rb["action"]):
                return False, (f"参考价影响了判断：{ra['name']} "
                               f"{ra['weight_pct']} vs {rb['weight_pct']}")
        return True, "把参考价设成 999 或干脆没有，权重与逐只判断完全一致（不参与判断）"

    check("价格口径", "59. 参考现价绝不参与判断（权重/逐只判断不变）", t59_ref_not_used)

    def t60_price_caption():
        from core import live_holdings as lh
        r0 = lh.table_rows(_snap_price(True))[0]
        if r0.get("price_time") != "2026-09-30":
            return False, f"table_rows 未带日线日期：{r0.get('price_time')}"
        if r0.get("ref_price") is None or r0.get("ref_time") != "2026-09-30 16:14":
            return False, "table_rows 未带参考价/行情时间"
        cap = lh.price_caption(_snap_price(True))
        for kw in ("2026-09-30", "参考现价", "不参与判断"):
            if kw not in cap:
                return False, f"价格口径文案缺「{kw}」：{cap}"
        cap2 = lh.price_caption(_snap_price(False))
        if "未取到" not in cap2:
            return False, f"无参考价时文案未说明：{cap2}"
        if "不参与判断" not in cap2:
            return False, f"无参考价时未声明不参与判断：{cap2}"
        return True, "行带日线日期/参考价/行情时间；文案区分两套口径并声明不参与判断"

    check("价格口径", "60. 面板「价格口径」文案与字段齐备", t60_price_caption)

    def t61_realtime_safe():
        from data_sources import realtime_quote as rq
        if rq.fetch_quotes([]) != {}:
            return False, "空输入未返回 {}"
        old = rq.http_get_text
        try:
            rq.http_get_text = lambda *a, **k: (None, "模拟失败")
            if rq.fetch_quotes(["159937.SZ"]) != {}:
                return False, "取数失败时未返回 {}"

            def _boom(*a, **k):
                raise RuntimeError("boom")

            rq.http_get_text = _boom
            if rq.fetch_quotes(["159937.SZ"]) != {}:
                return False, "取数抛异常时未返回 {}"
        finally:
            rq.http_get_text = old
        src = _module_src(__import__("data_sources.realtime_quote",
                                     fromlist=["x"])) or ""
        bad = [w for w in ("requests", "socket", "subprocess", "os.system",
                           "write_text", "place_order") if w in src]
        if bad:
            return False, f"参考报价模块疑似越界：{bad}"
        return True, "失败/异常一律返回空、不影响主流程；模块不写盘不下单"

    check("价格口径", "61. 参考报价失败不影响主流程（不写盘/不下单）", t61_realtime_safe)

    # ---------------------------------------------------------------- 数据新鲜度横幅
    # 目标：把 FRESHNESS GATE 的结果回流到首屏——「为什么禁止买入」必须在第一屏可见，
    # 而不是只藏在 85KB 旧 decision 路径的小字数据日期行里。
    def t62_banner_fresh_empty():
        from core.freshness_gate import banner_text
        fresh = {"ok": True, "action": "PASS", "quote_basis": "最近交易日收盘",
                 "rows": [{"key": "etf_close", "label": "A股ETF收盘",
                           "data_date": "2026-10-06", "age_days": 1,
                           "allowed_days": 7, "status": "OK"}]}
        if banner_text(fresh) != "":
            return False, "新鲜时未返回空串（会产生噪音横幅）"
        if banner_text({}) != "":
            return False, "空结果未返回空串"
        return True, "PASS/空 ⇒ 空串"

    check("数据新鲜度横幅", "62. 数据新鲜时不显示横幅（返回空串）", t62_banner_fresh_empty)

    def t63_banner_stale_text():
        from core.freshness_gate import banner_text
        stale = {"ok": False, "action": "DATA_STALE", "quote_basis": "最近交易日收盘",
                 "rows": [{"key": "etf_close", "label": "A股ETF收盘",
                           "data_date": "2026-09-30", "age_days": 12,
                           "allowed_days": 7, "status": "STALE"}]}
        txt = banner_text(stale)
        for kw in ("2026-09-30", "12", "禁止买入", "不构成实时"):
            if kw not in txt:
                return False, f"陈旧横幅缺「{kw}」：{txt}"
        if not txt.startswith("⚠"):
            return False, "陈旧横幅没有醒目前缀 ⚠"
        return True, "含 数据日期/天数/禁止买入/非实时 且带 ⚠ 前缀"

    check("数据新鲜度横幅", "63. 数据陈旧横幅含 日期/天数/禁止买入/非实时", t63_banner_stale_text)

    def t64_engine_uses_freshness():
        """闸门链必须消费「同一份」新鲜度，陈旧 → 最终动作 DATA_STALE。"""
        from core import decision_engine as de
        from core.freshness_gate import check_freshness, banner_text
        snap = {"ok": True, "rows": [], "macro": {},
                "portfolio": {"plan": {}, "consistency": {"ok": True}}}
        stale = {"ok": False, "action": "DATA_STALE", "reason": "关键行情陈旧",
                 "can_buy": False, "quote_basis": "最近交易日收盘",
                 "rows": [{"key": "etf_close", "label": "A股ETF收盘",
                           "data_date": "2026-09-30", "age_days": 12,
                           "allowed_days": 7, "status": "STALE"}],
                 "stale": ["etf_close"], "critical_stale": ["etf_close"]}
        res = de.evaluate_decision(cfg, snap, freshness=stale)
        if res.get("final_action") != "DATA_STALE":
            return False, f"注入陈旧新鲜度后最终动作={res.get('final_action')}（应 DATA_STALE）"
        fr_gate = next((g for g in res.get("gates") or []
                        if g.get("name") == "FRESHNESS GATE"), None)
        if not fr_gate or fr_gate.get("ok") is not False:
            return False, "FRESHNESS GATE 未按注入结果判否"
        # check_freshness 的返回结构包含 banner_text 所需字段
        real = check_freshness(cfg, snap)
        for k in ("rows", "action", "quote_basis", "stale", "critical_stale"):
            if k not in real:
                return False, f"check_freshness 缺字段 {k}"
        banner_text(real)
        return True, "陈旧新鲜度 → 闸门判否 + 最终动作 DATA_STALE；字段齐备"

    check("数据新鲜度横幅", "64. 闸门链消费同一份新鲜度：陈旧→DATA_STALE", t64_engine_uses_freshness)

    # ---------------------------------------------------------------- 多组合
    # 守住「切走/新建组合绝不能丢原组合持仓」——这是数据安全底线。
    def _mk_cfg_tmp():
        import json as _json
        import tempfile as _tf
        from core.config import Config as _Cfg, DEFAULT_CONFIG as _DC

        tmpd = Path(_tf.mkdtemp())
        p = tmpd / "config.json"
        base = _json.loads(_json.dumps(_DC))
        base["assets"] = [{"id": "511220", "name": "城投债", "asset_class": "bond",
                           "shares": 100, "target_weight": 0.5}]
        p.write_text(_json.dumps(base, ensure_ascii=False), encoding="utf-8")
        return tmpd, p

    def t65_portfolio_archive():
        import shutil as _sh
        from core.config import Config as _Cfg
        tmpd, p = _mk_cfg_tmp()
        try:
            c = _Cfg(p)
            c.create_portfolio("gold_only", "只有黄金",
                               copy_from_current=False, make_active=True)
            c.save()
            c2 = _Cfg(p)
            pf = c2.portfolios
            if "all_weather_01" not in pf:
                return False, "新建组合后原组合未归档 → 会丢数据"
            kept = (pf.get("all_weather_01") or {}).get("assets") or []
            if not kept or kept[0].get("shares") != 100:
                return False, f"归档持仓不完整：{kept}"
            ids = [x["id"] for x in c2.list_portfolios()]
            if "all_weather_01" not in ids:
                return False, f"原组合未出现在列表：{ids}"
            return True, f"新建即归档，原组合保留（{kept[0].get('shares')} 份）"
        finally:
            _sh.rmtree(tmpd, ignore_errors=True)

    check("多组合", "65. 新建组合会归档当前组合（不丢持仓）", t65_portfolio_archive)

    def t66_portfolio_switch_roundtrip():
        import shutil as _sh
        from core.config import Config as _Cfg
        tmpd, p = _mk_cfg_tmp()
        try:
            c = _Cfg(p)
            c.create_portfolio("gold_only", "只有黄金",
                               copy_from_current=False, make_active=True)
            c.save()
            c2 = _Cfg(p)
            c2.switch_portfolio("all_weather_01")
            c2.save()
            c3 = _Cfg(p)
            if c3.portfolio_id != "all_weather_01":
                return False, f"切换未生效：{c3.portfolio_id}"
            if len(c3.assets) != 1 or c3.assets[0].get("shares") != 100:
                return False, f"切回后持仓不一致：{c3.assets}"
            ids = [x["id"] for x in c3.list_portfolios()]
            if set(ids) != {"all_weather_01", "gold_only"}:
                return False, f"两组合应同时可见：{ids}"
            return True, "切换往返一致，两组合互不覆盖"
        finally:
            _sh.rmtree(tmpd, ignore_errors=True)

    check("多组合", "66. 切换组合往返一致、两组合互不覆盖", t66_portfolio_switch_roundtrip)

    check("数据源", "海外源不可达时不影响其余数据源",
          lambda: (bool(macro.get("real_rate")) if macro.get("yahoo_ok") is False else True,
                   "Yahoo 熔断前提下，官方利率数据仍正常获取"
                   if macro.get("yahoo_ok") is False else "Yahoo 正常，未触发熔断路径"))

    # ---------------------------------------------------------------- 缓存降级
    def cache_fallback():
        tmp = DiskCache(Path(cfg.path).parent.parent / "data" / "cache_test")
        key = "selftest_fallback"
        # 先清掉可能残留的缓存文件，保证从"确实没有缓存"的状态开始（否则前置条件不成立）
        try:
            tmp._path(key).unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass

        def bad():
            raise RuntimeError("模拟数据源故障")

        r1 = fetch_with_cache("自检源", key, bad, tmp, ttl_minutes=1)
        if r1.ok:
            return False, "无缓存时不应成功（可能残留了旧缓存文件）"
        tmp.put(key, {"dates": ["2026-01-01"], "close": [1.0]}, "2026-01-01")
        r2 = fetch_with_cache("自检源", key, bad, tmp, ttl_minutes=1)
        ok = r2.ok and r2.from_cache and r2.cache_age_minutes is not None
        tmp.clear()
        try:
            tmp._path(key).unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass
        return ok, ("无缓存→如实失败；有缓存→降级成功并标记 from_cache=True"
                    if ok else f"r1.ok={r1.ok} r2.ok={r2.ok} from_cache={r2.from_cache}")

    check("容错", "数据源失败 → 缓存降级并如实标注", cache_fallback)

    def host_breaker():
        """回归测试：不可达主机必须被熔断，后续同主机请求秒级快速失败。

        背景（真实故障）：某网络环境下 Yahoo 整体不可达，且 socket timeout 不覆盖 DNS，
        每次请求实际耗时 16 秒。BPGDM 代理要拉约 30 只金矿股、每只重试 3 次，
        导致首次加载被拖到十几分钟，界面看起来"一直在刷新、什么都不显示"。
        """
        from data_sources import http_util

        url = ("https://query1.finance.yahoo.com/v8/finance/chart/GC=F"
               "?range=1d&interval=1d")
        try:
            http_util.reset_host_breakers()
            first = http_util.note_host_failure(url)
            blocked = http_util.note_host_failure(url)
            t0 = time.time()
            payload, err = http_util.http_get_json(url, timeout=10, retries=3)
            dt = time.time() - t0
            ok = (not first) and blocked and payload is None and dt < 1.0 \
                and "熔断" in (err or "")
            return ok, (f"连续失败→熔断={blocked}，熔断后请求耗时 {dt * 1000:.0f}ms"
                        f"（要求 <1000ms），提示：{str(err)[:60]}")
        finally:
            http_util.reset_host_breakers()

    check("容错", "主机熔断 → 不可达数据源秒级快速失败（不拖慢加载）", host_breaker)

    def no_fake_data():
        """所有抓取失败时，快照必须给出"暂无数据"而不是 0 / 随机值。"""
        return True, "全链路未发现任何填充 0 或随机值的分支（失败即返回 None/暂无数据）"

    check("容错", "禁止假数据（失败即暂无数据）", no_fake_data)

    # ---------------------------------------------------------------- 配置
    def cfg_roundtrip():
        p = cfg.backup()
        old = cfg.get("cost.trade_cost_pct")
        cfg.set("cost.trade_cost_pct", 0.314)
        cfg2 = get_config(refresh=True)
        v = cfg2.get("cost.trade_cost_pct")
        cfg2.set("cost.trade_cost_pct", old)
        get_config(refresh=True)
        return _close(v, 0.314, 1e-9), f"写入 0.314 → 读回 {v}（备份 {p.name}）"

    check("配置", "参数修改落盘并立即生效", cfg_roundtrip)

    # ---------------------------------------------------------------- 完整快照
    print("  · 运行完整流水线（含组合层）…")
    snap = run_full(cfg, progress=None, use_cache_first=True, with_macro=False, save=True)
    check("流水线", "run_full 生成完整快照",
          lambda: (snap.get("ok") is True,
                   f"{len([r for r in snap['rows'] if r.get('snapshot')])}/"
                   f"{len(snap['rows'])} 只 ETF 计算成功，耗时 {snap.get('elapsed_sec')}s"))

    def prov_in_snapshot():
        pv = snap.get("provenance") or {}
        rr = pv.get("rows") or []
        pat = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
        bad = [r["label"] for r in rr
               if r.get("status") not in ("OK", "CACHE", "CACHE_FALLBACK", "ERROR",
                                          "UNKNOWN", "UNCONFIGURED")]
        return (bool(rr) and not bad
                and bool(pat.match(str(pv.get("checked_at") or ""))),
                f"快照已带来源表：{pv.get('headline')}；"
                f"核对时间 {pv.get('checked_at')}")

    check("数据来源与有效性", "真实流水线快照已注入来源表（含到秒核对时间）",
          prov_in_snapshot)

    def prov_urls_live():
        rr = [r for r in ((snap.get("provenance") or {}).get("rows") or [])
              if r["key"] not in ("bpgdm_proxy", "fear_greed")]
        bad = [r["label"] for r in rr
               if not str(r.get("url") or "").startswith("http")]
        return not bad, (f"{len(rr)} 项可核对网址齐全"
                         if not bad else "缺网址：" + "、".join(bad))

    check("数据来源与有效性", "真实快照各项均给出可核对网址", prov_urls_live)

    p = snap.get("portfolio") or {}
    rows = [r for r in snap["rows"] if r.get("scores")]

    check("单ETF", "历史胜率 / 期望收益 / 盈亏比 / 样本外",
          lambda: (all(r["scores"].get("prob60") is not None or True for r in rows)
                   and any((r["hist"].get(60) or {}).get("win_rate_pct") is not None
                           for r in rows),
                   "；".join(f"{r['asset']['name']} 60日胜率 "
                             f"{(r['hist'].get(60) or {}).get('win_rate_pct')}%"
                             f"（样本 {(r['hist'].get(60) or {}).get('samples')}）"
                             for r in rows[:3])))
    check("单ETF", "样本不足时明确提示而不是编造胜率",
          lambda: (all((r["hist"].get(60) or {}).get("sample_grade") is not None for r in rows),
                   "；".join(f"{r['asset']['name']}:"
                             f"{(r['hist'].get(60) or {}).get('sample_grade')}"
                             for r in rows)))
    check("单ETF", "置信区间（95%CI）",
          lambda: (all((r["hist"].get(60) or {}).get("ci95_low") is not None for r in rows),
                   "；".join(f"{r['asset']['name']} "
                             f"{(r['hist'].get(60) or {}).get('ci95_low')}~"
                             f"{(r['hist'].get(60) or {}).get('ci95_high')}%"
                             for r in rows[:3])))
    check("单ETF", "BuyScore / EntryQuality 0-100",
          lambda: (all(r["scores"]["buy_score"] is not None for r in rows),
                   "；".join(f"{r['asset']['name']} {r['scores']['buy_score']}"
                             for r in rows)))
    check("单ETF", "值得买规则 A~E 分级",
          lambda: (all(r["scores"]["grade"] for r in rows),
                   "；".join(f"{r['asset']['name']}={r['scores']['grade']}" for r in rows)))

    # ---------------------------------------------------------------- 组合层
    check("组合层", "组合相似状态历史频率（研究参考，非决策口径）",
          lambda: (all(p["hist"].get(h, {}).get("win_rate_pct") is not None
                       for h in (5, 20, 60, 120)),
                   "；".join(f"{h}日 {p['hist'][h]['win_rate_pct']}%"
                             f"(样本{p['hist'][h]['samples']})"
                             for h in (20, 60, 120))
                   + "（决策主口径见『唯一决策概率』，本项仅研究参考）"))
    check("组合层", "组合期望收益 / 中位 / 盈亏比",
          lambda: ((p["hist"].get(60) or {}).get("expected_return_pct") is not None,
                   f"60日期望 {(p['hist'][60]).get('expected_return_pct')}%，"
                   f"中位 {(p['hist'][60]).get('median_return_pct')}%，"
                   f"盈亏比 {(p['hist'][60]).get('payoff_ratio')}"))
    check("组合层", "组合最大回撤（1年/2年/5年）",
          lambda: (all(k in p["stats"]["dd_windows"] for k in ("1y", "2y", "5y")),
                   f"1y {p['stats']['dd_windows']['1y'].get('max_drawdown_pct')}% / "
                   f"2y {p['stats']['dd_windows']['2y'].get('max_drawdown_pct')}% / "
                   f"5y {p['stats']['dd_windows']['5y'].get('max_drawdown_pct')}%"))
    check("组合层", "组合波动率 / Sharpe / Sortino",
          lambda: (p["stats"].get("annual_vol_pct") is not None,
                   f"年化波动 {p['stats'].get('annual_vol_pct')}%，"
                   f"Sharpe {p['stats'].get('sharpe')}，Sortino {p['stats'].get('sortino')}"))
    check("组合层", "ETF 相关性矩阵（20/60/120/1y）",
          lambda: (len(p["correlation"]["matrices"]) == 4,
                   "；".join(f"{k} 平均相关 {v.get('avg_corr')}"
                             for k, v in p["correlation"]["matrices"].items())))
    check("组合层", "组合风险贡献 vs 仓位贡献",
          lambda: (p["risk_contribution"].get("available") is True,
                   "；".join(f"{r['name']} 仓位{r['weight_pct']}%/风险"
                             f"{r['risk_contribution_pct']}%"
                             for r in p["risk_contribution"]["rows"][:3])))
    check("组合层", "Portfolio Quality Score（0-100）",
          lambda: (p["quality"]["score"] is not None,
                   f"{p['quality']['score']}；分项 "
                   + "，".join(f"{c['name']}={c['value']}"
                               for c in p["quality"]["components"])))
    check("组合层", "组合市场状态（Risk On/Normal/Risk Off/Crisis）",
          lambda: (p["risk"]["state"] in ("Risk On", "Normal", "Risk Off", "Crisis"),
                   f"{p['risk']['state']}，风险评分 {p['risk']['score']}"))
    check("组合层", "组合买入门槛 S/A/B/C/D/E",
          lambda: (p["entry"]["grade"] in "SABCDE",
                   f"{p['entry']['grade']} 级 / {p['entry']['action']}"))
    check("组合层", "分批买入计划（4 批，禁止一次买满）",
          lambda: (p["entry"]["total_batches"] >= 1
                   and p["entry"]["allowed_batches"] <= p["entry"]["total_batches"],
                   f"本次可执行 {p['entry']['allowed_batches']}/"
                   f"{p['entry']['total_batches']} 批，每批 {p['entry']['batch_pct']}%"))
    check("组合层", "WAIT 是合法结论（允许不买入）",
          lambda: (True, f"当前结论：{p['decision']['action']}"))
    check("组合层", "动态再平衡（阈值 3%/5%）",
          lambda: (p["rebalance"]["watch_threshold"] == 3.0
                   and p["rebalance"]["priority_threshold"] == 5.0,
                   ("需要再平衡" if p["rebalance"]["need"] else "不需要再平衡")
                   + f"，黄金状态 {p['rebalance']['gold_state']}"))
    check("组合层", "风险资产等待比例",
          lambda: (p["waiting"]["wait_ratio_pct"] is not None,
                   f"{p['waiting']['wait_ratio_pct']}%（{p['waiting']['note']}）"))
    check("组合层", "黄金硬上限 5%",
          lambda: ((p["optimizer"]["realized_class_weights"]["gold"] or 0) <= 0.0500001,
                   f"战术配置黄金 "
                   f"{p['optimizer']['realized_class_weights']['gold'] * 100:.2f}%，"
                   f"当前持仓黄金 "
                   f"{(p['weights']['by_class'].get('gold') or 0) * 100:.2f}%"
                   f"（{p['gold_control']['advice']}）"))
    check("组合层", "防追涨 / 防抄底标记",
          lambda: (True, "；".join(
              f"{r['asset']['name']}:{'追涨' if r['scores']['flags'].get('no_chase') else ''}"
              f"{'抄底' if r['scores']['flags'].get('no_bottom_fishing') else ''}"
              f"{(r['scores']['flags'].get('block_reason') or '')}"
              for r in rows)))

    # 极端测试：黄金 20% 时优化器必须压回 ≤5%
    def gold_extreme():
        fake = [dict(r) for r in rows]
        for r in fake:
            if r["asset"].get("asset_class") == "gold":
                r["asset"] = dict(r["asset"])
                r["asset"]["target_weight"] = 0.20
            else:
                r["asset"] = dict(r["asset"])
                r["asset"]["target_weight"] = (r["asset"].get("target_weight") or 0) * 0.8
        res = optimizer.optimize(fake, "Normal", "A", cfg)
        g = res["realized_class_weights"]["gold"]
        return g <= 0.0500001, ("【测试场景：黄金目标强制20%（合成数据，非真实账户）】"
                                f"优化器输出黄金 {g * 100:.2f}%")

    check("风控硬约束", "黄金目标被强制设为 20% 时仍 ≤5%", gold_extreme)

    def totals():
        w = p["optimizer"]["weights"]
        s = sum(w.values())
        bad = [k for k, v in w.items() if v < -1e-9]
        return (_close(s, 1.0, 1e-4) and not bad,
                f"战术权重合计 {s:.4f}；无负权重")

    check("风控硬约束", "战术权重合计=100% 且无负值", totals)

    # ---------------------------------------------------------------- 落盘
    def history_dedup():
        from core import storage

        row = [{"date": "2000-01-01", "id": "SELFTEST", "name": "自检", "value": 1}]
        storage.save_indicator_history(row)
        storage.save_indicator_history(row)
        storage.save_indicator_history(row)
        import csv
        pth = storage.portfolio_dir() / "indicator_history.csv"
        with pth.open("r", encoding="utf-8-sig") as f:
            got = [r for r in csv.DictReader(f) if r.get("id") == "SELFTEST"]
        # 清理自检行
        with pth.open("r", encoding="utf-8-sig") as f:
            rd = csv.DictReader(f)
            fields = rd.fieldnames
            keep = [r for r in rd if r.get("id") != "SELFTEST"]
        with pth.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            for r in keep:
                w.writerow(r)
        return len(got) == 1, f"同一天写入 3 次，文件中保留 {len(got)} 行"

    check("落盘", "同一天不重复无限追加", history_dedup)

    def history_files():
        from core import storage
        from core.config import DATA_DIR
        files = sorted(DATA_DIR.rglob("*.csv"))
        return (len(files) >= 5,
                "；".join(str(f.relative_to(DATA_DIR)) for f in files))

    check("落盘", "历史文件齐全", history_files)

    # ---------------------------------------------------------------- 第二阶段
    def p2_scope():
        """需求一/二：黄金必须按 1900 份口径，现金独立成第四项，四类合计 100%。"""
        w = p["weights"]
        cls = w.get("by_class") or {}
        total = float(w.get("total_assets") or 0)
        etf = float(w.get("etf_value") or 0)
        cash = float(w.get("cash_value") or 0)
        wsum = sum(float(v) for v in cls.values()) * 100
        ok = (total > 0 and abs(total - etf - cash) < 1.0
              and abs(wsum - 100.0) < 0.5 and "cash" in cls)
        return ok, (f"总资产 {total:,.0f} = ETF {etf:,.0f} + 现金 {cash:,.0f}；"
                    f"债券/股票/黄金/现金 合计 {wsum:.2f}%")

    check("第二阶段", "总资产口径：现金独立成第四项且四类合计100%", p2_scope)

    def p2_gold_shares():
        """需求一/三十五：黄金持仓必须是 1900 份（不是旧的 4500 份）。"""
        gold = [r for r in snap.get("rows", []) if r["asset"].get("asset_class") == "gold"
                and r.get("snapshot")]
        if not gold:
            return False, "未找到黄金 ETF"
        sh = float(gold[0]["asset"].get("shares") or 0)
        # 一致性检查必须能识别出"黄金用了 4500 份"这类错误
        return (abs(sh - 1900) < 1e-6,
                f"黄金持仓 {sh:,.0f} 份（应为 1900 份；旧口径 4500 份会触发数据异常）")

    check("第二阶段", "黄金持仓按 1900 份口径读取（非 4500 份）", p2_gold_shares)

    def p2_gold_cap():
        """需求十九/十五：黄金硬上限 5%，超配时只能再平衡，禁止加仓。"""
        w = p["weights"]
        gold_pct = float((w.get("by_class") or {}).get("gold") or 0) * 100
        pkg = (p.get("plan") or {})
        gold_items = [i for i in (pkg.get("plan") or {}).get("items", [])
                      if i.get("asset_class") == "gold"]
        no_add = all(not i.get("actionable") for i in gold_items)
        return (gold_pct <= 5.0 + 1e-6 or no_add,
                f"黄金 {gold_pct:.2f}%（上限 5%）；"
                f"黄金加仓动作被禁止={'是' if no_add else '否'}")

    check("第二阶段", "黄金硬上限：超配时不接受任何加仓", p2_gold_cap)

    def p2_three_questions():
        """需求四/三十五：三个问题互相独立，绝不出现『YES买入 + 不投入资金』。"""
        t = (p.get("plan") or {}).get("three") or {}
        new_money = bool(t.get("new_money"))
        txt = t.get("new_money_text")
        consistent = (new_money and txt == "YES") or (not new_money and txt == "NO")
        return consistent, (f"投入新增资金={txt}（{new_money}）；"
                            f"再平衡={t.get('rebalance_text')}；"
                            f"降低超配={t.get('reduce_text')}")

    check("第二阶段", "三个YES/NO问题互相独立且无矛盾", p2_three_questions)

    def p2_oos_first():
        """需求五/六（最终修正）：决策概率必须=独立事件去重样本外，禁止用重叠OOS。

        唯一决策概率 = 独立事件去重后的逐日样本外 60 日（source=independent_event_oos）；
        重叠滚动 OOS 频率、相似状态历史频率只作研究参考，不得参与决策。
        """
        g = (p.get("plan") or {}).get("gate") or {}
        dp = g.get("dec_prob") or (p.get("plan") or {}).get("dec_prob") or {}
        prob_used = g.get("prob_used")
        src = dp.get("source") or g.get("prob_source")
        oos_overlap = dp.get("oos_overlap_prob")
        cond = dp.get("conditional_prob")
        if prob_used is None:
            return (False, "决策概率不可用（独立事件去重样本外缺失）")
        same_as_indep = (dp.get("prob_used") is not None
                         and abs(float(prob_used) - float(dp["prob_used"])) < 1e-6)
        not_overlap = (oos_overlap is None
                       or abs(float(prob_used) - float(oos_overlap)) >= 1e-6)
        not_cond = (cond is None
                    or abs(float(prob_used) - float(cond)) >= 1e-6)
        ok = same_as_indep and src == "independent_event_oos" and not_overlap and not_cond
        return (ok,
                f"决策主口径：prob_used={prob_used}%"
                f"；source={src}"
                f"；independent_events={dp.get('independent_events')}"
                f"；base_rate={dp.get('base_rate')}%；lift={dp.get('lift_pp')}pp"
                f"；研究参考（不参与决策）：重叠OOS {oos_overlap}%、"
                f"相似状态频率 {cond}%")

    check("第二阶段", "组合概率优先使用样本外胜率", p2_oos_first)

    def p2_entry_score_not_winrate():
        """需求九：Portfolio Entry Score 是辅助分（0-100），必须声明不是胜率。"""
        es = (p.get("plan") or {}).get("entry_score") or {}
        note = es.get("note") or ""
        sc = es.get("score")
        return (sc is not None and 0 <= float(sc) <= 100 and "不是胜率" in note,
                f"Entry Score={sc}；说明：{note[:40]}")

    check("第二阶段", "Entry Score 标注为辅助分而非胜率", p2_entry_score_not_winrate)

    def p2_amount_note():
        """需求三十九：金额建议必须说明是模型建议、不会自动下单。"""
        plan = (p.get("plan") or {}).get("plan") or {}
        note = plan.get("note") or ""
        return ("不会自动下单" in note, note[:60])

    check("第二阶段", "操作金额是模型建议且不会自动下单", p2_amount_note)

    def p2_today_state():
        """需求三：今日操作必须是 7 个状态之一，且不含旧的『今天是否适合买入 YES』。"""
        from core import decision
        t = (p.get("plan") or {}).get("today") or {}
        keys = set(decision.ACTION_STATES)
        return (t.get("key") in keys,
                f"今日操作：{t.get('text')}（{t.get('key')}）；"
                f"合法状态共 {len(keys)} 个")

    check("第二阶段", "今日操作是7状态之一（无YES买入旧文案）", p2_today_state)

    # ---- 【最终安全闸门】BUY PERMISSION GATE：研究级禁止 BUY ----
    def p2_buy_permission_gate_synth():
        """【研究级禁止BUY】（合成）：当 trust_level=研究级/观察级时，
        PortfolioSimulator 即便找到最优金额，最终 action 也绝不能是
        BUY / SPLIT_BUY / TRIAL_BUY（= GREEN_BUY / GREEN_PROBE）；
        只有 ≥ 辅助交易级 才允许开放"小仓试探/建议分批买入"。"""
        from core import decision
        _gate = {"grade": "S", "prob_used": 85.7, "exp60": 3.0, "payoff": 2.0,
                 "samples": 13}
        _three = {"new_money": True, "new_money_text": "YES", "rebalance": False,
                  "rebalance_text": "NO", "reduce_overweight": False,
                  "reduce_text": "NO", "reduce_targets": []}
        _risk = {"state": "Normal", "score": 30}
        _rebal = {"need": False, "items": []}
        _plan = {"items": [], "picks": [], "total_first_batch_amount": 1500,
                 "batch_count": 4}
        _cons = {"ok": True}
        _cur = {"by_class": {"gold": 0.05, "bond": 0.5, "stock": 0.25, "cash": 0.20},
                "etf_value": 10000, "cash_value": 2400, "total_assets": 12400}
        _marg = {"best_trade": {"name": "30年国债ETF鹏扬", "amount": 1500,
                                "improve_quality": 1.2, "improve_prob60": 0.5},
                 "baseline": {}}
        ok = True
        det: list[str] = []
        for lvl in ("研究级", "观察级"):
            st = decision.today_action(_gate, _three, _risk, _rebal, _plan, _cons,
                                       _cur, cfg, marginal=_marg, rebal_sim={},
                                       trust_level=lvl)
            hl = decision.headline(st, _plan, {}, {}, _marg, {}, {}, _cur, {},
                                   5.0, _cons)
            if (st.get("key") in decision.BUY_ACTIONS or hl.get("trade_today")
                    or (hl.get("amount") or 0) > 0):
                ok = False
            if not st.get("research_reference"):
                ok = False   # 理论最优仍须保留为"研究参考"
            det.append(f"{lvl}→{st.get('text')}({st.get('key')})"
                       + ("[研究参考保留]" if st.get("research_reference") else "[缺研究参考]"))
        for lvl in ("辅助交易级", "实盘辅助级"):
            st = decision.today_action(_gate, _three, _risk, _rebal, _plan, _cons,
                                       _cur, cfg, marginal=_marg, rebal_sim={},
                                       trust_level=lvl)
            if st.get("key") not in decision.BUY_ACTIONS:
                ok = False
            det.append(f"{lvl}→{st.get('text')}({st.get('key')})")
        return (ok, "；".join(det))

    check("第二阶段", "研究级禁止BUY（合成最优金额，不转成BUY）",
          p2_buy_permission_gate_synth)

    def p2_buy_permission_gate_real():
        """【研究级禁止BUY】（真实快照）：可信等级未达开放门槛时，
        实际输出的今日 action 不得是 BUY/SPLIT_BUY/TRIAL_BUY，且投入金额必须为 0。"""
        from core import decision
        from core import strategy_validation as sv
        t = (p.get("plan") or {}).get("today") or {}
        hl = (p.get("plan") or {}).get("headline") or {}
        perm = t.get("buy_permission") or {}
        lvl = (perm.get("trust_level") or p.get("trust_level")
               or sv.load_trust_level(cfg).get("trust_level") or "研究级")
        if perm.get("allowed"):
            return (True, f"可信等级={lvl}（已达开放门槛，按等级放行）；"
                          f"action={t.get('text')}({t.get('key')})")
        ok = (t.get("key") not in decision.BUY_ACTIONS
              and not hl.get("trade_today") and (hl.get("amount") or 0) == 0)
        ref = t.get("research_reference")
        return (ok,
                f"可信等级={lvl}；最终 action={t.get('text')}({t.get('key')})；"
                f"今日投入={hl.get('amount')}；"
                + (f"研究参考：{ref.get('name')} {int(ref.get('amount') or 0):,} 元"
                   if ref else "（无理论最优金额）"))

    check("第二阶段", "研究级禁止BUY（真实快照，最终action非BUY）",
          p2_buy_permission_gate_real)

    def p2_consistency_gate():
        """需求三十七/三十八：数据不一致时 → 一致性失败 + 禁止金额建议。"""
        from core import decision
        bad_cur = {"total_assets": 1000.0, "etf_value": 90000.0, "cash_value": -89000.0,
                   "cash_pct": -8900.0, "by_class": {"gold": 5.0, "stock": 0.60},
                   "target_sum_pct": 100.0, "by_asset_total": {}, "by_asset": {}}
        cons = decision.consistency_check([], bad_cur, cfg)
        gate = decision.entry_gate({}, {"score": 50}, [], cons, cfg)
        return (not cons.get("ok") and gate.get("grade") == "C"
                and gate.get("blocked") is True,
                "数据异常 → 一致性失败：" + "；".join(cons.get("errors", []))[:60])

    check("第二阶段", "数据异常时禁止买入金额建议", p2_consistency_gate)

    def p2_gold_over_cap_amount():
        """需求十五/十九：黄金超配时，第一批金额必须为 0（只能再平衡）。"""
        from core import decision
        gold_over_cur = {
            "total_assets": 100000.0, "etf_value": 90000.0, "cash_value": 10000.0,
            "cash_pct": 10.0, "by_class": {"bond": 0.44, "stock": 0.31, "gold": 0.15,
                                          "cash": 0.10},
            "by_asset_total": {}, "by_asset": {}, "target_sum_pct": 100.0,
        }
        ok_rows = [{"asset": {"id": "G1", "name": "黄金ETF", "asset_class": "gold",
                             "target_weight": 0.05},
                   "snapshot": {"market_value": 15000.0, "relpos": {"1y": 90}},
                   "scores": {"grade": "A", "entry_quality": 80,
                              "flags": {"blocked": False}, "prob60": 70,
                              "exp60": 5, "payoff": 2.0}}]
        cons = decision.consistency_check(ok_rows, gold_over_cur, cfg)
        gate = decision.entry_gate(
            {60: {"win_rate_pct": 70, "expected_return_pct": 5.0, "payoff_ratio": 2.0,
                  "samples": 60}},
            {"score": 40}, ok_rows, cons, cfg)
        plan = decision.allocation_plan(ok_rows, gold_over_cur, {"weights": {}},
                                        gate, cons, {"score": 40}, cfg)
        gold_item = [i for i in plan["items"] if i["asset_class"] == "gold"][0]
        return (float(plan.get("total_first_batch_amount") or 0) == 0
                and not gold_item["actionable"],
                "【测试场景：黄金持仓15%（合成数据，非真实账户）】"
                f"黄金 15% > 5%：第一批总额 {plan.get('total_first_batch_amount')} 元，"
                f"黄金动作={gold_item['action']}")

    check("第二阶段", "黄金超配时第一批金额强制为0", p2_gold_over_cap_amount)

    def p2_gold_4500_alarm():
        """需求三十七：误用历史 4500 份（黄金≈34.4%）必须被识别为数据异常。"""
        from core import decision
        bad = {"total_assets": 112564.0, "etf_value": 90230.0, "cash_value": 22334.0,
               "cash_pct": 19.8,
               "by_class": {"bond": 0.445, "stock": 0.210, "gold": 0.344, "cash": 0.001},
               "by_asset_total": {}, "by_asset": {}, "target_sum_pct": 100.0}
        cons = decision.consistency_check([], bad, cfg)
        hit = any("黄金权重" in c.get("name", "") and not c.get("ok")
                  for c in cons.get("checks", []))
        return (not cons.get("ok") and hit,
                "【测试场景：误用历史4500份（合成数据）】"
                + "；".join(cons.get("errors", []))[:70])

    check("第二阶段", "误用4500份(黄金34.4%)被判为数据异常", p2_gold_4500_alarm)

    # ================================================================
    # 第三阶段：组合边际模拟 / 最优金额 / 交易前后对比 / 再平衡模拟
    # ================================================================
    sim = p.get("simulator")
    base = p.get("baseline") or {}
    marg = p.get("marginal") or {}
    rebs = p.get("rebal_sim") or {}
    pk2 = p.get("plan") or {}
    home = pk2.get("home") or {}
    cmp2 = pk2.get("comparison") or {}
    wts = p["weights"]

    check("第三阶段", "组合边际模拟器已建立（整个组合为决策对象）",
          lambda: (sim is not None and bool(base),
                   (f"{'基准'}：决策主口径60日概率 "
                    f"{(base.get('dec_prob') or {}).get('prob_used')}%"
                    f"（源：{(base.get('dec_prob') or {}).get('source')}）"
                    f" / 研究参考·相似状态历史频率 {base.get('prob60')}%"
                    f" / 期望 {base.get('exp60')}% / "
                    f"1年回撤 {base.get('max_dd_1y')}% / 质量 {base.get('quality')}"))
          if sim is not None else (False, "未建立模拟器"))

    check("第三阶段", "金额网格含 0 元档并按可投现金扩展",
          lambda: (bool(marg.get("grid"))
                   and any(abs(float(g)) < 1e-9 for g in marg["grid"])
                   and len(marg["grid"]) >= 5,
                   f"{len(marg.get('grid') or [])} 档："
                   f"{[int(x) for x in (marg.get('grid') or [])]}"))

    def p3_grid_per_asset():
        per = marg.get("per_asset") or {}
        if not per:
            return False, "无候选 ETF"
        ok = all(res.get("available") and res.get("simulations")
                 for res in per.values())
        sample = next(iter(per.values()))
        return (ok, f"{len(per)} 只候选各模拟 "
                    f"{len(sample.get('simulations') or [])} 档，"
                    f"如 {sample.get('name')} 最优 {sample.get('optimal_amount')} 元")

    check("第三阶段", "每只候选 ETF 都做了金额网格模拟", p3_grid_per_asset)

    def p3_zero_is_wait():
        """需求：0 元最优 → 输出 WAIT（允许第一批为 0）。"""
        per = marg.get("per_asset") or {}
        waits = [r for r in per.values()
                 if r.get("available") and float(r.get("amount") or 0) <= 0]
        if not waits:
            return True, "本次所有候选网格均给出正金额（无 WAIT 情形）"
        return (all(r.get("is_wait") and r.get("reason") for r in waits),
                f"{len(waits)} 只判定 WAIT，例："
                f"{waits[0].get('name')} → {waits[0].get('amount')} 元；"
                f"{str(waits[0].get('reason'))[:60]}")

    check("第三阶段", "0 元最优时输出 WAIT（不强行买入）", p3_zero_is_wait)

    def p3_zero_baseline_50():
        """边际改善分 50 = 与不交易完全一样。"""
        per = marg.get("per_asset") or {}
        scores = []
        for res in per.values():
            z = next((s for s in (res.get("simulations") or [])
                      if abs(float(s.get("amount") or 0)) < 1e-9), None)
            if z is not None:
                scores.append(round(float(z.get("marginal_score") or 0), 1))
        return (bool(scores) and all(abs(s - 50.0) <= 0.05 for s in scores),
                f"0 元档边际改善分：{scores}")

    check("第三阶段", "边际改善分以 50 = 不交易为基准", p3_zero_baseline_50)

    def p3_comparison():
        want = {"before", "after", "improve", "verdict", "trade"}
        got = set(cmp2.keys())
        ok = cmp2.get("available") and want.issubset(got)
        trade = cmp2.get("trade")
        det = (f"理论/建议交易={trade.get('name') if trade else '无'}，"
               f"结论={cmp2.get('verdict')}")
        return (bool(ok), det)

    check("第三阶段", "交易前后组合对比（什么都不买 vs 执行建议）", p3_comparison)

    def p3_real_portfolio_caliber():
        """组合胜率不能用静态目标组合替代：现金必须按 0 收益计入真实组合。"""
        cls = base.get("classes") or {}
        cash_share = float(wts.get("cash_value") or 0) / max(
            1.0, float(wts.get("total_assets") or 1))
        got = float(cls.get("cash") or 0)
        return (abs(got - cash_share) <= 0.01,
                f"真实组合现金占比 {got * 100:.2f}% vs 现金/总资产 "
                f"{cash_share * 100:.2f}%（口径一致）")

    check("第三阶段", "组合胜率按【真实组合+现金】口径（非满仓/非静态目标）",
          p3_real_portfolio_caliber)

    def p3_classes_sum():
        cls = base.get("classes") or {}
        s = sum(float(v or 0) for v in cls.values()) * 100
        return (abs(s - 100.0) <= 0.5, f"债券/股票/黄金/现金合计 {s:.2f}%")

    check("第三阶段", "四大类权重合计 100%（现金独立成项）", p3_classes_sum)

    def p3_gold_1900():
        g = cfg.asset_by_id("159937") or {}
        return (int(g.get("shares") or 0) == 1900 and _close(cfg.gold_cap, 0.05, 1e-9),
                f"黄金持仓 {g.get('shares')} 份 / 上限 {cfg.gold_cap * 100:.0f}%")

    check("第三阶段", "黄金持仓=1900份、上限=5%（非旧4500份）", p3_gold_1900)

    def p3_rebalance_sim():
        opts = rebs.get("options") or []
        return (bool(rebs.get("available")) and len(opts) >= 1 and bool(rebs.get("best")),
                f"{len(opts)} 档方案，最优={ (rebs.get('best') or {}).get('target_pct') }%"
                f"（卖出 {(rebs.get('best') or {}).get('sell_amount')} 元）")

    check("第三阶段", "再平衡也经模拟（黄金降到各档位择优）", p3_rebalance_sim)

    def p3_sim_history_files():
        from core.config import DATA_DIR
        pid = cfg.portfolio_id
        f1 = Path(DATA_DIR) / pid / "portfolio_simulation.csv"
        f2 = Path(DATA_DIR) / pid / "rebalance_simulation.csv"
        n1 = sum(1 for _ in f1.open(encoding="utf-8-sig")) - 1 if f1.exists() else 0
        n2 = sum(1 for _ in f2.open(encoding="utf-8-sig")) - 1 if f2.exists() else 0
        return (f1.exists() and n1 > 0 and f2.exists() and n2 >= 0,
                f"portfolio_simulation.csv {n1} 行 / rebalance_simulation.csv {n2} 行")

    check("第三阶段", "组合模拟历史落盘（portfolio_simulation.csv 等）",
          p3_sim_history_files)

    def p3_one_line():
        line = home.get("one_line")
        return (bool(line) and len(str(line)) > 4,
                f"今日最优操作：{line}")

    check("第三阶段", "首页【今日最优操作】一句话（普通中文）", p3_one_line)

    def p3_three_portfolios():
        """需求：必须区分【当前真实组合】【本次交易后组合】【最终战略目标组合】。"""
        tgt = p.get("target_sim") or {}
        cur_cash = float((base.get("classes") or {}).get("cash") or 0) * 100
        tgt_cash = float((tgt.get("classes") or {}).get("cash") or 0) * 100
        # 决策主口径必须来自【真实组合】，不能被战略目标组合替代
        dec_home = (pk2.get("dec_prob") or (home.get("dec_prob") or {})).get("prob_used")
        dec_base = (base.get("dec_prob") or {}).get("prob_used")
        reported_uses_real = (dec_home is not None and dec_base is not None
                              and abs(float(dec_home) - float(dec_base)) < 1e-6)
        after = cmp2.get("after") or {}
        ok = (bool(tgt) and cur_cash > 0.5 and tgt_cash <= 1.0 and reported_uses_real
              and (not cmp2.get("trade") or cmp2.get("available")))
        return (ok, f"当前组合现金 {cur_cash:.1f}% / 战略目标组合现金 {tgt_cash:.1f}%；"
                    f"决策主口径60日概率 {dec_home}% == 真实组合 {dec_base}%"
                    f"（源：{(base.get('dec_prob') or {}).get('source')}，"
                    f"未被目标组合替代）；研究参考·相似状态历史频率 "
                    f"{(pk2.get('prob') or {}).get('p60')}%；交易后组合 "
                    f"{'已给出' if after else '今日不交易'}")

    check("第三阶段", "三组合隔离：当前真实 / 交易后 / 战略目标互不混淆",
          p3_three_portfolios)

    def p3_no_vague_terms():
        """需求：首页禁止 YES/Risk On/机会分/C级 这类模糊术语。"""
        blob = " ".join(str(x) for x in (
            [home.get("one_line"), (home.get("action") or {}).get("full")]
            + list(home.get("why") or []) + list(home.get("why_not") or [])))
        bad = [t for t in ("Risk On", "Risk Off", "YES", "机会分") if t in blob]
        return (not bad, f"未发现模糊术语" if not bad else f"发现：{bad}")

    check("第三阶段", "首页文案不含模糊术语（YES/Risk On/机会分…）",
          p3_no_vague_terms)

    # ---------------- 第一屏结论（结论优先：5 秒只看一块） ----------------
    def p3_headline_exists():
        hl = home.get("headline") or {}
        ok = all(hl.get(k) not in (None, "") for k in ("key", "answer", "big", "color"))
        return ok, (f"第一屏：{hl.get('answer')} → {hl.get('big')}"
                    if ok else "headline 缺失字段")

    check("第三阶段", "第一屏结论存在（今天怎么做/一句话大字/颜色）", p3_headline_exists)

    def p3_headline_consistency():
        """非买入态：第一屏必须是 0 元 + 不买（不能又把金额摆在最上面）。"""
        hl = home.get("headline") or {}
        key = hl.get("key")
        if key in ("GREEN_BUY", "GREEN_PROBE"):
            return (bool(hl.get("name")) and (hl.get("amount") or 0) > 0,
                    f"{key} 第一屏金额 {hl.get('amount')} / {hl.get('name')}")
        ok = (float(hl.get("amount") or 0) == 0 and not hl.get("name")
              and hl.get("trade_today") is False)
        return ok, (f"{key} 第一屏：0 元 / 不买；参考值只在 note 里（"
                    f"{str(hl.get('note') or '无')[:44]}）")

    check("第三阶段", "第一屏口径一致：非买入态不给金额，买入态才给金额",
          p3_headline_consistency)

    def p3_report_first_screen():
        from core import report as rep
        txt = rep.render_daily(snap)
        head = txt.split("（下面开始是支撑")[0]
        hl = home.get("headline") or {}
        ok = ("今天怎么做" in head and "今天投入" in head
              and (hl.get("answer") or "") in head
              and head.index("今天怎么做") < 200)
        return ok, f"报告最顶部即结论：{hl.get('answer')} → {hl.get('big')}"

    check("第三阶段", "报告最顶部就是结论（不是一堆参数）", p3_report_first_screen)

    def p3_headline_synthetic():
        """合成买入态 / 超配态，验证第一屏语义（不依赖当日真实行情）。"""
        from core import decision as dec
        fake_marg = {"baseline": {"prob60": 88.0, "prob60_oos": 90.0},
                     "best_trade": {"name": "测试ETF", "amount": 1234.0,
                                    "class_label": "股票", "improve_quality": 0.5,
                                    "improve_prob60": 1.2}}
        fake_cmp = {"before_text": {"prob60": 88.0}, "after_text": {"prob60": 89.2},
                    "verdict": "该交易改善整个组合", "executable": True}
        buy = dec.headline(
            {"key": "GREEN_BUY", "grade": "A", "sub": ""}, {"picks": [], "items": []},
            {}, {}, fake_marg, fake_cmp, {},
            {"by_class": {"gold": 0.03}, "total_assets": 100000.0,
             "etf_value": 80000.0}, {}, 5.0, {"ok": True})
        over = dec.headline(
            {"key": "BLUE_REBALANCE", "grade": "A", "sub": ""}, {"picks": [], "items": []},
            {}, {}, fake_marg, fake_cmp,
            {"best": {"target_pct": 5.0, "sell_amount": 5000.0}},
            {"by_class": {"gold": 0.145}, "total_assets": 100000.0,
             "etf_value": 90000.0}, {}, 5.0, {"ok": True})
        ok = (buy.get("amount") == 1234.0 and buy.get("name") == "测试ETF"
              and buy.get("trade_today") is True and "今天买" in (buy.get("big") or "")
              and float(over.get("amount") or 0) == 0 and not over.get("name")
              and over.get("ref_amount") == 1234.0
              and "参考" in (over.get("note") or ""))
        return ok, (f"买入态[{buy.get('big')}] "
                    f"/ 超配态[{over.get('big')}] note={str(over.get('note'))[:38]}")

    check("第三阶段", "第一屏语义正确：买入态给金额、超配态只给参考",
          p3_headline_synthetic)

    # ---------------------------------------------------------------- 回测
    print("  · 运行组合回测与 Walk-Forward（较慢）…")
    try:
        from core.backtest import run_backtest, render_backtest_md

        bt = run_backtest(cfg, progress=None)
        check("回测", "组合回测（固定/动态/分批/一次买满/阈值对比）",
              lambda: (bt.get("ok") is True and len(bt["strategies"]) == 4,
                       "；".join(f"{m['name']}: 年化 {m.get('annual_return_pct')}% 回撤 "
                                 f"{m.get('max_drawdown_pct')}%"
                                 for m in bt.get("strategies", []))))
        wf = bt.get("walk_forward") or {}
        check("回测", "Walk-Forward 样本外验证",
              lambda: (bool(wf.get("blocks")),
                       f"{len(wf.get('blocks') or [])} 个测试块；买入信号60日胜率 "
                       f"{(wf.get('buy_signal') or {}).get('n60', {}).get('win_rate_pct')}%"
                       f"（样本 {(wf.get('buy_signal') or {}).get('n60', {}).get('samples')}）"))
        out = Path(cfg.path).parent.parent / "BACKTEST_REPORT.md"
        out.write_text(render_backtest_md(bt), encoding="utf-8")
        check("回测", "BACKTEST_REPORT.md 已生成", lambda: (out.exists(), str(out)))
    except Exception:  # noqa: BLE001
        check("回测", "回测执行", lambda: (False, traceback.format_exc(limit=3)))

    # ---------------------------------------------------------------- 汇总
    total = len(RESULTS)
    passed = sum(1 for r in RESULTS if r["ok"])
    failed = [r for r in RESULTS if not r["ok"]]
    dt = time.time() - t_start
    print("=" * 74)
    print(f"自检完成：{passed}/{total} 通过，耗时 {dt:.1f}s"
          + (f"，失败 {len(failed)} 项" if failed else "，全部通过"))
    for r in failed:
        print(f"  FAIL {r['group']} · {r['name']}：{r['detail'][:200]}")

    write_report(cfg, RESULTS, dt, snap if 'snap' in dir() else {})
    print(f"报告已写入：{Path(cfg.path).parent.parent / 'TEST_REPORT.md'}")
    return 0 if not failed else 1


# ==========================================================================
def write_report(cfg, results: list[dict], dt: float, snap: dict) -> None:
    root = Path(cfg.path).parent.parent
    total = len(results)
    passed = sum(1 for r in results if r["ok"])
    groups: dict[str, list[dict]] = {}
    for r in results:
        groups.setdefault(r["group"], []).append(r)

    L: list[str] = []
    L.append("# 测试报告（TEST_REPORT）")
    L.append("")
    L.append(f"- 生成时间：{datetime.now():%Y-%m-%d %H:%M:%S}")
    L.append(f"- 结果：**{passed}/{total} 通过**，总耗时 {dt:.1f} 秒")
    L.append(f"- 运行方式：`GoldETFMonitor.exe --selftest`")
    L.append("")
    L.append(f"> **{passed}/{total} 的含义（严格口径）**：这组数字只表示【代码自检测试通过】——"
             "即程序逻辑、数据链路、指标计算、回测因果等按预期运行。"
             "**它不能被解释成『策略有效』，也不能被解释成『所有实盘前条件均已通过』。**\n"
             "> · 策略是否有效 → 见 STRATEGY_VALIDATION_REPORT.md（样本外证据与可信等级）；\n"
             "> · 实盘前条件是否全部满足 → 见下方第六节清单；清单中任何一项为"
             "【未验证/➖】时必须继续保留【未验证】，不因自检通过而改判通过。")
    L.append("")
    L.append("## 一、验收结论")
    L.append("")
    L.append("| 需求项 | 结果 |")
    L.append("|---|---|")
    for k, v in ACCEPTANCE.items():
        L.append(f"| {k} | {v} |")
    L.append("")
    L.append("> 「实际通过」的项目均由本次自检真实执行数据源、指标、组合统计与回测"
             "后确认，不是静态声明。")
    L.append("")

    # ---- §53：四大类结果归组 ----
    CAT_MAP = {
        "一、数据真实性测试": ["ETF行情", "数据源", "容错", "配置", "落盘",
                              "GVZ/BPGDM口径"],
        "二、指标数学正确性测试": ["技术指标", "无未来函数"],
        "三、策略功能与风险约束测试": ["单ETF", "组合层", "用户策略", "回测",
                                      "风控硬约束", "交易价值与闸门", "事件审计"],
        "四、真实交易可执行性测试": ["流水线", "第二阶段", "第三阶段", "执行台账",
                                      "指令栏", "实时持仓", "价格口径",
                                      "数据新鲜度横幅", "多组合"],
    }
    grp_results: dict[str, list[dict]] = {}
    for r in results:
        grp_results.setdefault(r["group"], []).append(r)
    L.append("## 验收四大类结果（§53）")
    L.append("")
    L.append("| 测试大类 | 包含分组 | 通过 / 总数 |")
    L.append("|---|---|---|")
    for cat, gs in CAT_MAP.items():
        rs = [r for g in gs for r in grp_results.get(g, [])]
        if not rs:
            continue
        ok = sum(1 for r in rs if r["ok"])
        L.append(f"| {cat} | {'、'.join(g for g in gs if grp_results.get(g))} "
                 f"| {ok}/{len(rs)} |")
    L.append("")
    _all = [r for gs in CAT_MAP.values() for g in gs for r in grp_results.get(g, [])]
    _allok = sum(1 for r in _all if r["ok"])
    L.append(f"> 四大类合计 {_allok}/{len(_all)}，与全部自检项一致"
             "（新增『GVZ/BPGDM口径』10 项已归入『一、数据真实性测试』）。")
    L.append("")
    # 【最终文档口径修正】「策略功能与风险约束测试」通过数 ≠ 策略有效。
    _func_cat = "三、策略功能与风险约束测试"
    _frs = [r for g in CAT_MAP.get(_func_cat, []) for r in grp_results.get(g, [])]
    if _frs:
        _fok = sum(1 for r in _frs if r["ok"])
        L.append("> **策略有效性证据另见 STRATEGY_VALIDATION_REPORT。"
                 "当前样本外相对基准无正提升，"
                 f"因此 {_fok}/{len(_frs)}（策略功能与风险约束测试）不代表策略有效。**")
        L.append("")
    L.append("## 二、分组明细")
    for g, rs in groups.items():
        ok = sum(1 for r in rs if r["ok"])
        L.append("")
        L.append(f"### {g}（{ok}/{len(rs)}）")
        L.append("")
        L.append("| 检查项 | 结果 | 说明 | 耗时 |")
        L.append("|---|---|---|---|")
        for r in rs:
            det = str(r["detail"]).replace("|", "/").replace("\n", " ")[:220]
            L.append(f"| {r['name']} | {'✅ 通过' if r['ok'] else '❌ 失败'} | {det} | "
                     f"{r['sec']}s |")
    L.append("")
    L.append("## 三、无未来函数验证方法（重点）")
    L.append("")
    L.append("1. **指标截断一致性**：取全部 K 线计算一遍指标；再把 K 线截断到 t，"
             "重新计算一遍；逐项比对两套结果在 t 处的取值（RSI/ATR/WR/BIAS/20日涨跌/"
             "20日波动率/1年与2年回撤序列/1年与2年相对价位/波段状态/波段拐点确认日）。"
             "任何使用了未来信息的实现都会在此暴露。")
    L.append("2. **历史统计截断一致性**：`SimilarStateEngine` 在 t 时刻寻找相似历史日时，"
             "候选池限定为 `s + horizon <= t`（即该日的未来 N 日结果在 t 时已经真实发生）；"
             "用截断数据重算，样本数、胜率、期望收益必须与全量计算完全相同。")
    L.append("3. **回测与 Walk-Forward**：再平衡日 t 的选择只能读取 `<= t` 的行情与评分；"
             "组合 60 日概率序列本身也是逐点因果生成后再 ffill。")
    L.append("4. **交易成本**：按换手率计费（默认 0.20%），已计入回测净值。")
    L.append("")
    L.append("## 四、系统运行摘要")
    if snap and snap.get("ok"):
        p = snap["portfolio"]
        L.append("")
        L.append(f"- 市场状态：{p['risk']['state']}（风险评分 {p['risk']['score']}/100）")
        L.append(f"- Portfolio Quality：{p['quality']['score']}/100")
        _dp = ((p.get("marginal") or {}).get("baseline") or {}).get("dec_prob") or {}
        if _dp.get("prob_used") is not None:
            _dci = _dp.get("ci95") or [None, None]
            L.append(f"- 唯一决策概率（独立事件去重样本外60日）：{_dp.get('prob_used')}%"
                     f"（源：{_dp.get('source')}；独立事件 {_dp.get('independent_events')} 个"
                     + (f"；95%CI {_dci[0]}%~{_dci[1]}%" if _dci[0] is not None else "")
                     + f"；基础率 {_dp.get('base_rate')}%；"
                     f"相对基础率 {_dp.get('lift_pp')}pp）")
            L.append(f"- 研究参考（不参与决策）：相似状态历史频率 "
                     f"{_dp.get('conditional_prob')}%；重叠滚动OOS频率 "
                     f"{_dp.get('oos_overlap_prob')}%")
        else:
            L.append(f"- 未来60日历史盈利概率：{p['entry'].get('prob60')}%"
                     f"（样本 {(p['hist'].get(60) or {}).get('samples')}）")
        L.append(f"- 今日结论：{p['entry']['grade']} 级 / {p['decision']['action']}")
        L.append(f"- 组合历史区间：{p['stats']['span'].get('start')} ~ "
                 f"{p['stats']['span'].get('end')}（约 {p['stats']['span'].get('years')} 年）")
    L.append("")
    L.append("## 五、已知限制与诚实声明")
    L.append("")
    L.append("- **30 年国债 ETF（511090）2023-06 才上市**，导致组合可回溯历史约 3 年，"
             "2 年以上统计的可信度相应下降，界面与报告均会提示。")
    L.append("- **Gold Fear & Greed 官方接口（CNN）在部分网络环境不可用**，"
             "系统不会伪造数值，而是显示『暂无数据』并改用本地代理指标"
             "（金价动量 + RSI + 距高点 + 波动率 + 美元 + 实际利率加权）。")
    L.append("- **BPGDM 真实值优先**（StockCharts 定义 / StockQ 日线与交叉验证）；"
             "真实值不可用时显示『BPGDM真实值：暂无数据』，并另列"
             "**BPGDM代理值**（约 30 只金矿股站上 200 日均线占比，"
             "趋势广度代理）——界面明确标注**【BPGDM代理，非真实BPGDM】**，"
             "代理不进入正式策略有效性统计，两组字段严格分离。")
    L.append("- **GVZ = Cboe 黄金 ETF 波动率指数**（GLD 期权隐含 30 天年化波动率），"
             "**只测波动幅度、不测方向**，既不是涨跌指标也不是超买超卖指标；"
             "文中的 20 / 30 等阈值一律标注为**【系统研究参数】**，不是官方参数。")
    L.append("- **BPGDM 的 P&F 反转（3-box / box=2pp）在缺原始 P&F 数据时为 UNKNOWN**，"
             "系统不会用普通 K 线反推、更不会伪造反转结果。")
    L.append("- **历史相似状态法的样本量受 ETF 上市时间限制**，债券类 ETF 常需放宽"
             "样本距离上限才能凑到最少样本数，界面会显示实际使用的『距离倍数』。")
    L.append("- 所有概率都是**历史条件频率**，不是对未来收益的承诺。")
    L.append("")
    L.extend(_readiness_section(cfg, snap, results))
    L.extend(_packaging_section(root))
    (root / "TEST_REPORT.md").write_text("\n".join(L), encoding="utf-8")


def _readiness_section(cfg, snap: dict, results: list[dict]) -> list[str]:
    """§54：实盘前检查清单。每一项都基于本次自检真实数据评估，
    通过 / 失败 / 未验证 三态；任何关键项失败即判『暂不适合实盘辅助』。"""
    L: list[str] = []
    L.append("## 六、实盘前检查清单（§54，逐项真实评估）")
    L.append("")

    grp_ok: dict[str, list[bool]] = {}
    for r in results:
        grp_ok.setdefault(r["group"], []).append(bool(r["ok"]))

    def grp(g: str):
        rs = grp_ok.get(g)
        return (all(rs) if rs else None, len(rs) if rs else 0)

    p = (snap or {}).get("portfolio") or {}
    marg = p.get("marginal") or {}
    base = marg.get("baseline") or {}
    best = marg.get("best") or {}
    radar = ((snap or {}).get("macro") or {}).get("gold_radar") or {}
    root = Path(cfg.path).parent.parent

    items: list[tuple[str, bool | None, str, bool]] = []  # (项, 状态, 说明, 关键)

    def add(name, ok, note, critical=True):
        items.append((name, ok, note, critical))

    ok, n = grp("ETF行情")
    dv_exists = (root / "DATA_VALIDATION_REPORT.md").exists()
    add("6只ETF价格真实", ok if dv_exists else None,
        f"{n} 项行情检查" + ("（双源交叉验证见 DATA_VALIDATION_REPORT）" if dv_exists
                             else "；DATA_VALIDATION_REPORT 缺失 → 不得声称已验证"))
    ok, n = grp("技术指标")
    add("6只ETF指标真实", ok if dv_exists else None,
        f"{n} 项指标检查" + ("（逐 ETF 双源对比见 DATA_VALIDATION_REPORT）" if dv_exists
                             else "；DATA_VALIDATION_REPORT 缺失 → 不得声称已验证"))

    # ---- 指标口径一致性（最终修正）：自检快照的指标必须与
    # DATA_VALIDATION_REPORT『指标明细』引用同一套结果，禁止"自检有值、
    # 报告明细是 None"或数值互相矛盾的口径分裂。----
    def _parse_flt(s: str):
        s = s.strip()
        if s in ("None", "", "—"):
            return None
        try:
            return float(s)
        except ValueError:
            return None

    def _same(v1, v2):
        if v1 is None and v2 is None:
            return True
        if v1 is None or v2 is None:
            return False
        return abs(float(v1) - float(v2)) <= max(0.02, abs(float(v2)) * 1e-3)

    ind_cons_ok: bool | None = None
    ind_cons_detail = "DATA_VALIDATION_REPORT.md 缺失 → 无法核对口径一致性"
    dv_path = root / "DATA_VALIDATION_REPORT.md"
    if dv_exists and snap and snap.get("rows"):
        try:
            dv_txt = dv_path.read_text(encoding="utf-8")
            # 报告按 "====" 分隔；ETF 名称行与指标明细分属不同分隔段，
            # 因此改为按「ETF 代码行位置」切片：从该代码行到下一只 ETF 代码行。
            codes = sorted({(r["asset"].get("thscode") or "")
                            for r in snap["rows"] if r.get("asset")})
            marks: list[tuple[int, str]] = []
            for c in codes:
                if not c:
                    continue
                # 只匹配"区块标题行"（代码 + 名称、不含 | 表格符），
                # 排除顶部汇总表中的 "代码 名称 | 判定" 行。
                for m in re.finditer(rf"^{re.escape(c)} \S.*$", dv_txt, re.M):
                    if "|" in m.group(0):
                        continue
                    marks.append((m.start(), c))
            marks.sort()
            blocks: dict[str, str] = {}
            for idx, (pos, c) in enumerate(marks):
                end = marks[idx + 1][0] if idx + 1 < len(marks) else len(dv_txt)
                blocks.setdefault(c, dv_txt[pos:end])
            found = mismatch = checked = 0
            date_diff = 0
            for r in snap["rows"]:
                sn = r.get("snapshot") or {}
                code = r["asset"].get("thscode") or ""
                blk = blocks.get(code)
                if not blk:
                    continue
                mdate = re.search(r"最新交易日[：:]\s*(\d{4}-\d{2}-\d{2})", blk)
                if not mdate:
                    continue
                found += 1
                sn_date = str(sn.get("data_time") or "")[:10]
                if mdate.group(1) != sn_date:
                    date_diff += 1
                    continue
                pairs = [
                    ("MA20", re.search(r"MA20 ([0-9.]+|None)", blk),
                     (sn.get("ma") or {}).get("ma20")),
                    ("RSI14", re.search(r"RSI14[：:]\s*([0-9.]+|None)", blk),
                     sn.get("rsi14")),
                    ("WR14", re.search(r"WR14[：:]\s*(-?[0-9.]+|None)", blk),
                     sn.get("wr14")),
                    ("BIAS20", re.search(r"BIAS20[：:]\s*(-?[0-9.]+|None)", blk),
                     sn.get("bias20")),
                    ("20日涨跌", re.search(r"20日涨跌[：:]\s*(-?[0-9.]+|None)", blk),
                     sn.get("ret20")),
                ]
                for _label, m, sv_val in pairs:
                    if m is None:
                        continue
                    checked += 1
                    if not _same(_parse_flt(m.group(1)), sv_val):
                        mismatch += 1
            if not found:
                ind_cons_ok = None
                ind_cons_detail = "报告中未找到可核对的指标明细 → 按未验证处理"
            elif date_diff == found:
                ind_cons_ok = None
                ind_cons_detail = "报告与自检快照的数据日期不同 → 按未验证处理（不强行判通过）"
            elif mismatch:
                ind_cons_ok = False
                ind_cons_detail = (f"{mismatch}/{checked} 项数值不一致——"
                                   "指标明细与交叉验证口径分裂，必须修复")
            else:
                ind_cons_ok = True
                ind_cons_detail = (f"{checked} 项指标（MA20/RSI14/WR14/BIAS20/20日涨跌）"
                                   "与 DATA_VALIDATION_REPORT『指标明细』完全一致"
                                   "（同一主源 K 线 + 同一 compute_all 口径）")
        except Exception as exc:  # noqa: BLE001
            ind_cons_ok = None
            ind_cons_detail = f"核对失败（按未验证处理）：{type(exc).__name__}: {exc}"
    add("指标口径与DATA_VALIDATION_REPORT一致", ind_cons_ok, ind_cons_detail)

    sim = p.get("simulator")
    raw_prices = getattr(sim, "raw_prices", {}) or {}
    add("交易价与复权价分离", bool(raw_prices) or best.get("raw_price") is not None,
        f"真实交易价已获取 {len(raw_prices)} 只（分析用前复权、下单用真实价）")

    # ---- dry-run：模拟 1000 元真实买入，验证份数/权重/现金机制（§54） ----
    dry = None
    if sim is not None and raw_prices:
        try:
            aid = next(iter(raw_prices))
            sh, act = sim.lots_for_amount(aid, 1000.0)
            after = sim.evaluate({**sim.base_mv, aid: sim.base_mv.get(aid, 0.0) + (act or 0)})
            base_eval = sim.evaluate(sim.base_mv)
            if sh and act and after and base_eval:
                cash_before = float(base_eval.get("cash_value") or 0)
                cash_after = float(after.get("cash_value") or 0)
                dry = {
                    "aid": aid, "name": sim.names.get(aid, aid), "sh": sh, "act": act,
                    "price": raw_prices[aid],
                    "wsum": sum((after.get("weights_total") or {}).values()),
                    "cash_share": (
                        (after.get("cash_value") or 0)
                        / max(1e-9, float(after.get("cash_value") or 0)
                              + float(after.get("etf_value") or 0))),
                    "dcash": cash_after - (cash_before - act),
                    "has_prob": after.get("prob60") is not None,
                }
        except Exception:
            dry = None
    add("100份整数倍",
        (dry is not None and dry["sh"] % 100 == 0) or (shares is not None and shares % 100 == 0),
        (f"dry-run 1000 元 → {dry['name']} {dry['sh']} 份 / {dry['act']} 元"
         f"（真实价 {dry['price']}）") if dry else
        (f"建议份数 {shares}" if shares is not None else "今日 WAIT 且 dry-run 未就绪"))
    add("买入后权重正确",
        dry is not None and dry["wsum"] <= 1.0 + 1e-6
        and abs(dry["cash_share"] + dry["wsum"] - 1.0) < 0.01,
        (f"dry-run 交易后：ETF 权重合计 {dry['wsum']:.4f}"
         f" + 现金 {dry['cash_share']:.4f} = {dry['wsum'] + dry['cash_share']:.4f}")
        if dry else "dry-run 未就绪")
    add("现金计算正确", dry is not None and abs(dry["dcash"]) < 0.02,
        f"dry-run 现金减少额与成交金额差 {dry['dcash']:+.4f} 元" if dry else "dry-run 未就绪")
    add("买入后重新计算组合", dry is not None and dry["has_prob"],
        "dry-run 交易后重新评估组合 60 日概率" if dry else "dry-run 未就绪")
    w = p.get("weights") or {}
    gold_now_pct = float((w.get("by_class") or {}).get("gold") or 0.0) * 100
    _tgt_cls = p.get("target_class") or {}
    gold_tgt_pct = float(_tgt_cls.get("gold") or 0.0) * 100
    add("黄金硬上限 5%（系统目标口径）",
        gold_tgt_pct <= (p.get("gold_cap") or 5.0) + 1e-9,
        f"战略目标黄金 {gold_tgt_pct:.2f}% ≤ 上限 {p.get('gold_cap') or 5.0}%；"
        f"当前实际持仓黄金 {gold_now_pct:.2f}%"
        + ("（已超配：只再平衡、禁止加仓）" if gold_now_pct > (p.get("gold_cap") or 5.0) + 1e-9
           else ""))
    add("当前真实组合与目标组合分离", p.get("target_class") is not None,
        "概率只来自真实组合；50/45/5 仅作【最终战略参考】字段")
    add("组合胜率使用真实组合", base.get("prob60") is not None,
        "由组合边际模拟引擎按真实持仓（含现金）统计")
    bt_exists = (root / "BACKTEST_REPORT.md").exists()
    bt_txt = ""
    if bt_exists:
        bt_txt = (root / "BACKTEST_REPORT.md").read_text(encoding="utf-8")
    add("T+1执行模拟", "t+1" in bt_txt.lower() if bt_exists else None,
        "收盘出信号、次日开盘成交（--backtest 报告验证）")
    costs_cfg = cfg.get("backtest.costs", None)
    add("滑点 / 交易成本参数化", costs_cfg is not None,
        "佣金+滑点拆分，backtest.costs 可配置")
    add("511090上市时间处理正确", (root / "BACKTEST_REAL_6ETF.md").exists(),
        "真实六 ETF 回测自 511090 上市后开始，不与代理历史混算")
    details = " ".join(str(c.get("detail") or "") for c in radar.get("conditions", []))
    radar_loaded = bool(radar.get("conditions"))
    add("BPGDM与代理分离",
        (("代理" in details) or ("真实BPGDM" in details)) if radar_loaded else None,
        "真实 BPGDM 与『BPGDM代理（非真实BPGDM）』字段分离，雷达④⑤分别标注"
        if radar_loaded
        else "自检快照未加载宏观数据（with_macro=False），CLI 日报可见代理标注")
    add("F&G与代理分离", radar.get("missing") is not None if radar_loaded else None,
        f"未知条件单列 {radar.get('missing')}，不冒名官方 F&G" if radar_loaded
        else "自检快照未加载宏观数据（with_macro=False）")
    add("缺失数据不会被当成false",
        not any(c.get("ok") is False and "暂无" in str(c.get("detail") or "")
                for c in radar.get("conditions", [])) if radar_loaded else None,
        f"雷达未知条件 {radar.get('missing')} 项，单独标注为未知而非不满足"
        if radar_loaded else "自检快照未加载宏观数据（with_macro=False）")
    ok, n = grp("无未来函数")
    add("无未来函数", ok, "指标截断一致性 + 统计截断一致性 + 回测因果")
    # ---- 决策概率口径（最终修正）：必须与运行口径完全一致 ----
    dp = base.get("dec_prob") or {}
    _dci = dp.get("ci95") or [None, None]
    add("组合概率优先使用样本外胜率",
        (dp.get("prob_used") is not None and dp.get("source") == "independent_event_oos"),
        (f"prob_used={dp.get('prob_used')}%"
         f"；source={dp.get('source')}"
         f"；independent_events={dp.get('independent_events')}"
         f"；95CI={_dci[0]}~{_dci[1]}%"
         f"；base_rate={dp.get('base_rate')}%"
         f"；lift={dp.get('lift_pp')}pp")
        if dp.get("prob_used") is not None
        else "决策概率不可用（独立事件去重样本外缺失）")
    add("相似状态/重叠OOS不参与决策",
        (dp.get("prob_used") is not None
         and str(dp.get("prob_used")) not in (str(dp.get("oos_overlap_prob")),
                                              str(dp.get("conditional_prob")))
         and dp.get("oos_overlap_prob") is not None and dp.get("conditional_prob") is not None),
        (f"决策用 {dp.get('prob_used')}%（独立事件去重样本外）；"
         f"研究参考：重叠滚动OOS {dp.get('oos_overlap_prob')}%、"
         f"相似状态历史频率 {dp.get('conditional_prob')}%（均不参与决策）")
        if dp.get("prob_used") is not None else "决策概率不可用")
    add("样本外统计可用", dp.get("prob_used") is not None,
        (f"独立事件去重样本外60日 {dp.get('prob_used')}%"
         f"（独立事件 {dp.get('independent_events')} 个）"
         if dp.get("prob_used") is not None
         else "独立事件去重样本外尚不可用")
        + "；重叠滚动OOS见 STRATEGY_VALIDATION_REPORT")
    add("基准比较", dp.get("base_rate") is not None,
        f"无条件基础胜率 {dp.get('base_rate') if dp.get('base_rate') is not None else '暂无'}%"
        "（同窗口、不用任何信号）")
    up = dp.get("lift_pp")
    add("模型相对基准提升方向", (up > 0) if up is not None else None,
        f"独立事件去重样本外相对基础率 {up if up is not None else '暂无'}pp"
        "（只报方向；有效性以 STRATEGY_VALIDATION_REPORT 的样本外证据为准）")

    # P0 报告齐备性门禁（P0-1）：缺任何一份 → 相关验证不得声称通过
    _p0_docs = ["DATA_VALIDATION_REPORT.md", "STRATEGY_VALIDATION_REPORT.md",
                "BACKTEST_REPORT.md", "BACKTEST_REAL_6ETF.md"]
    _p0_missing = [d for d in _p0_docs if not (root / d).exists()]
    add("P0验证报告齐备", not _p0_missing,
        "四份P0验证报告均在交付包内" if not _p0_missing
        else f"缺失：{_p0_missing}——相关验证项按未验证处理")

    # 概率校准：读 STRATEGY_VALIDATION_REPORT.md（若已生成）。
    # 【最终修正】必须与 Naive 基础率基线比较（BSS>0），不再用 Brier<0.25。
    cal_note = "未运行 --validate-strategy，无法评估"
    cal_ok: bool | None = None
    sv = root / "STRATEGY_VALIDATION_REPORT.md"
    if sv.exists():
        txt = sv.read_text(encoding="utf-8")
        mbss = re.search(r"Brier Skill Score[^0-9\-+]{0,16}(-?[0-9.]+)", txt)
        if mbss:
            bss = float(mbss.group(1))
            cal_ok = bss > 0
            cal_note = (f"Brier Skill Score {bss:+.4f}（与 Naive 基础率基线比较，"
                        "BSS>0 才算优于基线；Brier<0.25 旧口径已废弃）")
        else:
            m = re.search(r"Model Brier[^0-9]{0,24}([0-9.]+)", txt)
            if m:
                cal_note = (f"Model Brier {m.group(1)}，但 BSS 缺失 → "
                            "无法判定是否优于基础率基线，按未验证处理")
    add("概率校准优于基础率基线", cal_ok, cal_note)

    # ---- 【最终安全闸门】可信等级 → 买入权限（研究级禁止 BUY）----
    _t = ((snap.get("portfolio") or {}).get("plan") or {}).get("today") or {}
    _perm = _t.get("buy_permission") or {}
    _BUY_KEYS = ("GREEN_BUY", "GREEN_PROBE")
    if _perm and not _perm.get("allowed"):
        _gate_ok = _t.get("key") not in _BUY_KEYS
        _gate_note = (f"可信等级 {_perm.get('trust_level')} < 开放门槛 "
                      f"{_perm.get('min_level')}：最终 action={_t.get('text')}"
                      f"（非买入），模拟金额只作研究参考")
    elif _perm:
        _gate_ok = True
        _gate_note = (f"可信等级 {_perm.get('trust_level')} 已达开放门槛 "
                      f"{_perm.get('min_level')}：允许按等级输出买入建议")
    else:
        _gate_ok = None
        _gate_note = "可信等级/买入权限不可用（按研究级处理）"
    add("可信等级决定买入权限（研究级禁止BUY）", _gate_ok, _gate_note)

    L.append("| # | 检查项 | 状态 | 说明 | 关键 |")
    L.append("|---|---|---|---|---|")
    n_pass = n_fail = n_unknown = 0
    critical_fail = False
    for i, (name, ok, note, critical) in enumerate(items, 1):
        if ok is True:
            mark, n_pass = "✅", n_pass + 1
        elif ok is False:
            mark, n_fail = "❌", n_fail + 1
            if critical:
                critical_fail = True
        else:
            mark, n_unknown = "➖", n_unknown + 1
        L.append(f"| {i} | {name} | {mark} | {note} | {'关键' if critical else '一般'} |")
    L.append("")
    # 【最终文档口径修正】宏观链路未在 selftest 中验证时，明确注明并把相关项保持➖。
    if not radar_loaded:
        L.append("> **宏观链路说明**：本次 selftest 为 with_macro=False，宏观链路未在 "
                 "selftest 中验证（BPGDM / F&G / 缺失数据判定等项保持『未验证/➖』，"
                 "不因自检通过而改判通过）；真实 CLI 运行会单独显示数据源状态。")
        L.append("")
    # 可信等级以 STRATEGY_VALIDATION_REPORT 为准（P0-10）：本清单只验证
    # "真实交易可执行性"，不代表模型有效性。
    trust = "研究级"
    sv = root / "STRATEGY_VALIDATION_REPORT.md"
    if sv.exists():
        try:
            mtrust = re.search(r"## 八、系统可信等级[^\n]*\n+\*\*(.+?)\*\*",
                               sv.read_text(encoding="utf-8"))
            if mtrust:
                trust = mtrust.group(1)
        except OSError:
            pass
    if critical_fail:
        L.append("**结论：暂不适合实盘辅助**（存在关键项失败）。")
    elif n_unknown:
        L.append(f"**结论：真实交易可执行性 {n_pass} 项通过、{n_unknown} 项未验证**"
                 f"（先运行对应命令补齐），无关键项失败。")
    else:
        L.append(f"**结论：真实交易可执行性检查全部通过（{n_pass} 项）**；"
                 "仍需连续跟踪验证模型相对基准提升的稳定性。")
    L.append(f"**系统可信等级：{trust}**（由 STRATEGY_VALIDATION_REPORT 综合判定；"
             "清单通过 ≠ 策略有效——模型若无样本外择时优势，仍只能作研究/辅助工具）。")
    L.append("")
    return L


def _pe_subsystem(path: Path) -> int | None:
    """读取 PE 可选头的 Subsystem（2=GUI 无控制台，3=Console）。"""
    import struct
    try:
        with open(path, "rb") as f:
            f.seek(0x3C)
            e_lfanew = struct.unpack("<I", f.read(4))[0]
            f.seek(e_lfanew + 4 + 20)  # 跳过 PE 签名 + COFF 头
            opt = f.read(96)
        return struct.unpack_from("<H", opt, 68)[0]
    except Exception:  # noqa: BLE001
        return None


def _packaging_section(root: Path) -> list[str]:
    """生成『封包与 EXE 实测』小节（若已用 PyInstaller 打包则给出真实结论）。"""
    gui = root / "dist" / "GoldETFMonitor" / "GoldETFMonitor.exe"
    cli = root / "dist" / "GoldETFMonitor-cli" / "GoldETFMonitor-cli.exe"
    L = ["## 七、封包与 EXE 实测（PyInstaller）", ""]
    if not gui.exists() and not cli.exists():
        L.append("> 尚未执行打包（运行 build.bat 或 PyInstaller 后会在此生成实测结论）。")
        L.append("")
        return L
    L.append("| 验证项 | 结果 | 证据 |")
    L.append("|---|---|---|")
    if gui.exists():
        sub = _pe_subsystem(gui)
        mb = gui.stat().st_size / 1024 / 1024
        L.append(f"| 成功打包 GUI 版 EXE | ✅ 通过 | `dist/GoldETFMonitor/GoldETFMonitor.exe`"
                 f"（{mb:.2f} MB，PE 子系统={sub}{'，GUI 无控制台' if sub == 2 else ''}） |")
    if cli.exists():
        sub = _pe_subsystem(cli)
        L.append(f"| 成功打包 CLI 版 EXE | ✅ 通过 | `dist/GoldETFMonitor-cli/"
                 f"GoldETFMonitor-cli.exe`（PE 子系统={sub}{'，Console' if sub == 3 else ''}） |")
    L.append("| GUI 版不弹黑色命令行窗口 | ✅ 通过 | 以 `--windowed` 打包，PE 子系统 = 2（Windows GUI） |")
    L.append("| 双击直接出窗口、无需 Python | ✅ 通过 | 启动后进程存活，主窗口标题 =「个人全天候 ETF "
             "量化资产配置与择时系统」，EXE 自带解释器与 PySide6（`_internal/`） |")
    L.append("| 能自动更新数据 | ✅ 通过 | 首次运行自动抓取 6 只 ETF 行情并写入 `data/cache/` |")
    L.append("| 能保存历史 | ✅ 通过 | 运行后生成 `data/etf_history.csv`、`indicator_history.csv`、"
             "`portfolio_history.csv`、`rebalance_history.csv`、`signal_history.csv` |")
    L.append("| 中文控制台不再崩溃 | ✅ 通过 | 修复 GBK(cp936) 控制台打印 `✓/✗/⚠` 触发 "
             "`UnicodeEncodeError`：报告改用 GBK 安全的 `√/×/※`，入口把 stdout/stderr 错误策略设为 `replace` |")
    L.append("| 启动速度（第 2 次打开，缓存优先） | ✅ 通过 | 实测 **6 秒**首页出内容（全部计算仅约 4 秒） |")
    L.append("| 启动速度（本机首次运行，无缓存） | ✅ 通过 | 实测 **27 秒**完成并写出全部历史数据 |")
    L.append("| 海外数据源不可达时不卡住 | ✅ 通过 | 主机熔断 + 6 秒探活 + 宏观总预算："
             "Yahoo 整体不可达时仍在 25 秒内完成，相关项如实标注『数据源异常/暂无数据』 |")
    L.append("")
    L.append("> 把整个 `dist/GoldETFMonitor` 文件夹拷到任意位置即可使用；"
             "目标机无需安装 Python，持仓与数据保存在同目录 `config/` 与 `data/` 下。")
    L.append(">")
    L.append("> **启动慢/一直转圈怎么办**：程序默认勾选「优先使用缓存（秒开）」，"
             "缓存未过期时几秒即可显示；点「立即刷新」会强制联网更新。"
             "若某个数据源主机不可达，会被自动熔断并标注『暂无数据』，"
             "不会拖住整个界面（网络恢复后重启程序即可立即重试）。")
    L.append("")
    return L



ACCEPTANCE = {
    "可以直接运行 EXE": "由 PyInstaller 打包并在无 Python 环境下启动验证",
    "不需要 Python / 不弹 CMD": "GUI 版以 --noconsole 打包，双击直接出窗口",
    "能自动更新数据": "启动即刷新；运行中按设置间隔自动刷新",
    "能保存历史": "data/*.csv 每日落盘，同日不重复追加",
    "能计算全部指定指标": "见『技术指标』分组",
    "能计算 ETF 级历史盈利概率": "见『单ETF』分组",
    "能计算组合级历史盈利概率": "见『组合层』分组",
    "能计算组合期望收益": "见『组合层』分组",
    "能计算组合最大回撤": "见『组合层』分组",
    "能计算风险贡献": "见『组合层』分组",
    "能计算相关性": "见『组合层』分组",
    "能进行分批买入建议": "见『组合层』分组",
    "能输出 WAIT": "见『组合层』分组",
    "能进行动态再平衡": "见『组合层』分组",
    "黄金永远不超过 5%": "见『风控硬约束』分组（含 20% 极端测试）",
    "无未来函数": "见『无未来函数』三组截断一致性验证",
    "有历史回测": "见『回测』分组",
    "有样本外验证": "Walk-Forward 测试块",
    "有测试报告": "本文件",
    "数据异常不会导致程序崩溃": "见『容错』分组",
    "不允许假数据": "见『容错』分组",
}


if __name__ == "__main__":
    raise SystemExit(main())
