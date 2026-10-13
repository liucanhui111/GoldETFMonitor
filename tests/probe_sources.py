"""诊断脚本：逐个数据源计时，找出拖慢首次加载的源头。只读，不写任何缓存。"""
from __future__ import annotations

import os
import sys
import time
import urllib.request

os.environ.setdefault("GOLD_ETF_HOME", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

UA = {"User-Agent": "Mozilla/5.0"}
TO = 8


def probe(name, url, timeout=TO):
    t0 = time.time()
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            n = len(r.read(4096))
        dt = time.time() - t0
        print(f"  OK   {name:<28} {dt:6.2f}s  bytes>={n}")
        return True
    except Exception as exc:  # noqa: BLE001
        dt = time.time() - t0
        print(f"  FAIL {name:<28} {dt:6.2f}s  {type(exc).__name__}: {str(exc)[:70]}")
        return False


def main() -> int:
    print("== 数据源连通性与耗时（单次，无重试）==")
    t0 = time.time()
    probe("Yahoo 金价 GC=F",
          "https://query1.finance.yahoo.com/v8/finance/chart/GC=F?range=5d&interval=1d")
    probe("Yahoo DXY DX-Y.NYB",
          "https://query1.finance.yahoo.com/v8/finance/chart/DX-Y.NYB?range=5d&interval=1d")
    probe("Yahoo 金矿 NEM",
          "https://query1.finance.yahoo.com/v8/finance/chart/NEM?range=5d&interval=1d")
    probe("CNN Fear&Greed",
          "https://production.dataviz.cnn.io/index/fearandgreed/graphdata")
    probe("美国财政部 10Y实际利率CSV",
          "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
          "daily-treasury-rates.csv/2025/all?type=daily_treasury_real_yield_curve"
          "&field_tdr_date_value=2025&page&_format=csv")
    probe("FRED DFII10",
          "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DFII10")
    probe("同花顺扶摇 API",
          "https://fuyao.aicubes.cn/api/fund/market/historical"
          "?thscode=159937.SZ&interval=1d&start=1756000000000&end=1759000000000",
          timeout=12)
    print(f"-- 合计 {time.time() - t0:.1f}s --")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
