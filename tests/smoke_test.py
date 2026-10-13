"""冒烟测试：真实数据 → 指标 → 特征 → 历史统计。

用法：
    python tests/smoke_test.py
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import indicators, scoring                     # noqa: E402
from core.config import get_config                        # noqa: E402
from core.histstats import SimilarStateEngine, build_feature_series  # noqa: E402
from data_sources.etf_source import ETFDataSource          # noqa: E402
from data_sources.http_util import STATUS                  # noqa: E402


def main() -> int:
    cfg = get_config(refresh=True)
    ds = ETFDataSource(cfg)
    cost = cfg.trade_cost()
    ok = 0
    for asset in cfg.enabled_assets():
        name = asset["name"]
        t0 = time.time()
        raw = ds.fetch(asset)
        if not raw:
            print(f"[FAIL] {name} 无数据")
            continue
        ind = indicators.compute_all(
            raw["dates"], raw["open"], raw["high"], raw["low"], raw["close"],
            cfg, asset.get("asset_class", "stock"),
        )
        feats = build_feature_series(ind, cfg)
        engine = SimilarStateEngine(feats, cfg.get("hist.feature_weights", {}), cfg)
        t = ind["n"] - 1
        stats = engine.all_horizons(t, ind["close"], cost)

        bands = cfg.get("classification.relpos_bands", [20, 35, 65, 80])
        ts_ = scoring.trend_score(ind)
        ps = scoring.position_score(ind, bands)
        ss = scoring.short_term_score(ind)
        sws = scoring.swing_score(ind)
        s60 = stats.get(60, {})
        s20 = stats.get(20, {})
        ok += 1
        print("=" * 96)
        print(f"{name} ({asset['thscode']})  源={raw.get('source')}  "
              f"缓存={raw.get('from_cache')}  数据日={raw.get('data_time')}  "
              f"bars={ind['n']}  耗时={time.time()-t0:.1f}s")
        print(f"  收盘={ind['close'][-1]:.3f}  MA20={ind['ma'].get('ma20',[None])[-1]}  "
              f"MA60={ind['ma'].get('ma60',[None])[-1]}  "
              f"MA200={ind['ma'].get('ma200',[None])[-1]}")
        print(f"  RSI={_r(ind['rsi14'][-1])}  WR={_r(ind['wr14'][-1])}  "
              f"ATR%={_r(ind['atr_pct'][-1])}  BIAS20={_r(ind['bias20'][-1])}  "
              f"RET20={_r(ind['ret20'][-1])}")
        print(f"  1yDD={_r((ind['dd_windows'].get('1y') or {}).get('max_drawdown_pct'))}  "
              f"2yDD={_r((ind['dd_windows'].get('2y') or {}).get('max_drawdown_pct'))}  "
              f"relpos1y={_r(ind['relpos'].get('1y'))}  relpos2y={_r(ind['relpos'].get('2y'))}")
        print(f"  波段={ind['swing']['state']}  {ind['swing']['detail']}")
        print(f"  Score: Trend={_r(ts_)}  Position={_r(ps)}  ShortTerm={_r(ss)}  Swing={_r(sws)}")
        print(f"  [20日] 样本={s20.get('samples')} 胜率={s20.get('win_rate_pct')}% "
              f"期望={s20.get('expected_return_pct')}%")
        print(f"  [60日] 样本={s60.get('samples')}/{s60.get('pool_size')} "
              f"({s60.get('sample_grade')}/{s60.get('credibility')}) "
              f"胜率={s60.get('win_rate_pct')}% CI={s60.get('ci95_low')}-{s60.get('ci95_high')}% "
              f"期望={s60.get('expected_return_pct')}% 盈亏比={s60.get('payoff_ratio')} "
              f"最大回撤={s60.get('max_drawdown_pct')}% 样本外胜率={s60.get('oos_win_rate_pct')}%")
    print("=" * 96)
    print(f"成功 {ok}/{len(cfg.enabled_assets())} 只")
    print("数据源状态：", STATUS.summary_text())
    return 0 if ok == len(cfg.enabled_assets()) else 1


def _r(v, nd=2):
    return "None" if v is None else round(float(v), nd)


if __name__ == "__main__":
    raise SystemExit(main())
