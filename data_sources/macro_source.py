"""宏观数据汇总 + 黄金底部雷达。

统一入口 MacroHub.load()，返回：
{
  "gold": {...}, "gold_metrics": {...},
  "usd": {...}, "usd_metrics": {...},
  "real_rate": {...}, "real_rate_metrics": {...},
  "nominal_rate": {...}, "nominal_rate_metrics": {...},
  "fear_greed": {...}, "gold_sentiment_proxy": {...},
  "bpgdm": {...}, "bpgdm_metrics": {...},
  "gold_radar": {...},
  "warnings": [ ... ]
}
"""
from __future__ import annotations

import time

from .bpgdm_source import BPGDMSource, bpgdm_metrics
from .fear_greed_source import FearGreedSource, gold_sentiment_proxy
from .fred_source import RateSource, policy_metrics, rate_metrics
from .gold_source import GoldSource, gold_metrics
from .gvz_source import GVZSource, gvz_metrics
from .http_util import (STATUS, configure_breakers, ensure_host_available,
                        host_block_reason)
from .usd_source import USDSource, usd_metrics
# 底部雷达 8 条件 + 三级分级已迁移至 core/gold_macro.py（用户策略 2026-10 版）
from core.gold_macro import gold_bottom_radar

YAHOO_PROBE = "https://query1.finance.yahoo.com/v8/finance/chart/GC=F?range=1d&interval=1d"
YAHOO_LABEL = "雅虎财经（国际金价 / 美元指数 / 金矿股）"


class MacroHub:
    def __init__(self, cfg) -> None:
        self.cfg = cfg
        configure_breakers(cfg)
        self.gold = GoldSource(cfg)
        self.usd = USDSource(cfg)
        self.rates = RateSource(cfg)
        self.fg = FearGreedSource(cfg)
        self.bpgdm = BPGDMSource(cfg)
        self.gvz = GVZSource(cfg)

    def load(self, use_cache_first: bool = False, progress=None) -> dict:
        def step(msg: str):
            if progress:
                progress(msg)

        out: dict = {"warnings": [], "skipped": []}
        budget = float(self.cfg.get("app.macro_budget_seconds", 45) or 45)
        deadline = time.monotonic() + max(10.0, budget)

        def out_of_time() -> bool:
            return time.monotonic() > deadline

        def skip(label: str) -> None:
            out["skipped"].append(label)
            out["warnings"].append(
                f"{label}：加载超过总预算 {budget:.0f} 秒，已跳过（显示暂无数据）")

        # ---- 0) 海外主机可达性一次性探测 -------------------------------------
        # Yahoo 承载金价 / DXY / 金矿股广度三块数据，且每个标的都要单独请求。
        # 若网络受限导致整台主机不可达，先熔断它，避免"13 只金矿股 × 重试 3 次 × 超时"
        # 把首次加载拖到十几分钟（界面会看起来像一直在刷新）。
        if bool(self.cfg.get("data_sources.yahoo_enabled", True)):
            step("探测海外数据源连通性…")
            ok, msg = ensure_host_available(
                YAHOO_PROBE, float(self.cfg.get("app.host_probe_timeout", 6) or 6))
            out["yahoo_ok"] = ok
            out["yahoo_probe"] = msg
            if not ok:
                out["warnings"].append(
                    f"{YAHOO_LABEL}当前不可达：{msg}。"
                    "国际金价 / 美元指数 / BPGDM 将显示『暂无数据』，其余数据不受影响"
                    "（网络恢复后重启程序即可立即重试）。")

        # 注意：FRED 只作为利率 / GVZ 的**备源**（主源分别是美国财政部官方 CSV 与
        # Cboe 官方），因此这里**不做**一次性探测——探测失败会弹出"利率/GVZ 将暂无数据"
        # 的假警报，而实际上主源一直是好的。取数通道的降级交给 http_util 惰性处理：
        # 真要用到 FRED 时，浏览器 UA 失败会立刻自动换「诚实 UA / 系统 curl」。

        # ---- 1) 国际金价 ------------------------------------------------------
        if out_of_time():
            skip("国际金价")
            out["gold"] = None
        else:
            step("获取国际金价…")
            try:
                out["gold"] = self.gold.fetch(use_cache_first)
            except Exception as exc:  # noqa: BLE001
                out["gold"] = None
                out["warnings"].append(f"国际金价获取失败：{exc}")
        out["gold_metrics"] = gold_metrics(out.get("gold"))

        # ---- 2) 美元指数 ------------------------------------------------------
        if out_of_time():
            skip("美元指数")
            out["usd"] = None
        else:
            step("获取美元指数…")
            try:
                out["usd"] = self.usd.fetch(use_cache_first)
            except Exception as exc:  # noqa: BLE001
                out["usd"] = None
                out["warnings"].append(f"美元指数获取失败：{exc}")
        out["usd_metrics"] = usd_metrics(out.get("usd"))

        # ---- 3) 美国实际利率与 10 年期收益率（速度快、可靠性高，优先保障）----
        if out_of_time():
            skip("美国实际利率与10年期收益率")
            out["real_rate"] = None
            out["nominal_rate"] = None
        else:
            step("获取美国实际利率与10年期收益率…")
            try:
                out["real_rate"] = self.rates.fetch_real(use_cache_first)
            except Exception as exc:  # noqa: BLE001
                out["real_rate"] = None
                out["warnings"].append(f"10年期实际利率获取失败：{exc}")
            try:
                out["nominal_rate"] = self.rates.fetch_nominal(use_cache_first)
            except Exception as exc:  # noqa: BLE001
                out["nominal_rate"] = None
                out["warnings"].append(f"10年期国债收益率获取失败：{exc}")
        out["real_rate_metrics"] = rate_metrics(out.get("real_rate"), "美国10年期实际利率")
        out["nominal_rate_metrics"] = rate_metrics(out.get("nominal_rate"), "美国10年期国债收益率")

        # ---- 3.5) Fed 利率预期（2Y-3M 政策预期代理，同一财政部 CSV）----
        if out_of_time():
            skip("Fed利率预期")
            out["policy"] = None
        else:
            step("获取 Fed 利率预期…")
            try:
                out["policy"] = self.rates.fetch_policy(use_cache_first)
            except Exception as exc:  # noqa: BLE001
                out["policy"] = None
                out["warnings"].append(f"Fed利率预期获取失败：{exc}")
        out["policy_metrics"] = policy_metrics(out.get("policy"))

        # ---- 4) 黄金情绪指标 --------------------------------------------------
        if out_of_time():
            skip("黄金情绪指标")
            out["fear_greed"] = None
        else:
            step("获取黄金情绪指标…")
            try:
                out["fear_greed"] = self.fg.fetch_official(use_cache_first)
            except Exception as exc:  # noqa: BLE001
                out["fear_greed"] = None
                out["warnings"].append(f"Gold Fear & Greed 获取失败：{exc}")
        out["gold_sentiment_proxy"] = gold_sentiment_proxy(
            out.get("gold"), out.get("usd"), out.get("real_rate")
        )

        # ---- 4.5) 黄金波动率指数 GVZ ----
        if out_of_time():
            skip("黄金波动率指数GVZ")
            out["gvz"] = None
        else:
            step("获取黄金波动率指数 GVZ…")
            try:
                out["gvz"] = self.gvz.fetch(use_cache_first)
            except Exception as exc:  # noqa: BLE001
                out["gvz"] = None
                out["warnings"].append(f"GVZ 获取失败：{exc}")
        out["gvz_metrics"] = gvz_metrics(out.get("gvz"))

        # ---- 5) BPGDM 金矿股广度 ---------------------------------------------
        bpgdm_err = ""
        if out_of_time():
            skip("BPGDM 金矿股广度")
            out["bpgdm"] = None
            # 原因要具体：如果海外主机已被熔断（如 Yahoo 被 403 拦截），
            # 那"预算用尽"只是表象，真正原因是主机不可用——优先报后者。
            blocked = host_block_reason(YAHOO_PROBE)
            bpgdm_err = (
                f"海外行情主机不可用（{blocked}），BPGDM 真实源与本地代理均无法取数"
                if blocked else
                "本次时间预算已用尽，未及尝试 BPGDM（可稍后刷新或增大时间预算）")
        else:
            step("获取 BPGDM 金矿股广度…")
            try:
                out["bpgdm"] = self.bpgdm.fetch(use_cache_first, deadline=deadline)
            except Exception as exc:  # noqa: BLE001
                out["bpgdm"] = None
                bpgdm_err = str(exc)
                out["warnings"].append(f"BPGDM 获取失败：{exc}")
            if out["bpgdm"] is None and not bpgdm_err:
                # fetch() 失败时不抛异常而返回 None：原因记在实例上，取回来显示。
                bpgdm_err = getattr(self.bpgdm, "last_error", "") or ""
        # 把真实失败原因传进去，界面才能显示"连不上哪个源、为什么（如 HTTP 403）"
        out["bpgdm_metrics"] = bpgdm_metrics(out.get("bpgdm"), error=bpgdm_err)

        out["gold_radar"] = gold_bottom_radar(out, self.cfg)
        return out


# --------------------------------------------------------------------------
# 黄金底部雷达：8 条件 + 三级分级 → 已迁移至 core/gold_macro.py
# （恐慌区 / 底部观察区 / 趋势反转确认区，按用户 2026-10 策略规则）
# 这里仅保留 re-export 以兼容旧调用点。
# --------------------------------------------------------------------------
