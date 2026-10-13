"""验证新的 _rolling_avg_corr（前缀和）与旧实现（逐窗口 _corr）逐点一致。"""
from __future__ import annotations

import math
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("GOLD_ETF_HOME", BASE)
sys.path.insert(0, BASE)

from core import portfolio as pf  # noqa: E402


def old_avg_corr(panel: pf.Panel, window: int) -> list:
    n = len(panel.dates)
    out: list = [None] * n
    ids = panel.assets
    last = None
    for i in range(window, n):
        if (i - window) % 5 != 0:
            out[i] = last
            continue
        pairs = []
        for x in range(len(ids)):
            for y in range(x + 1, len(ids)):
                a = [r for r in panel.ret[ids[x]][i - window + 1:i + 1] if r is not None]
                b = [r for r in panel.ret[ids[y]][i - window + 1:i + 1] if r is not None]
                m = min(len(a), len(b))
                if m < window // 2:
                    continue
                c = pf._corr(a[-m:], b[-m:])
                if c is not None:
                    pairs.append(c)
        if pairs:
            last = pf._mean(pairs) * 100.0
            out[i] = last
        else:
            out[i] = last
    return out


def main() -> int:
    from core.config import get_config
    from core.pipeline import run_full

    cfg = get_config(refresh=True)
    snap = run_full(cfg, use_cache_first=True, with_macro=False, save=False)
    datasets = {r["asset"]["id"]: {"dates": r["ind"]["dates"], "close": r["ind"]["close"]}
                for r in snap["rows"] if r.get("snapshot")}
    names = {r["asset"]["id"]: r["asset"].get("name")
             for r in snap["rows"] if r.get("snapshot")}
    panel = pf.Panel(datasets, snap["portfolio"]["weights"]["by_asset"], names)

    bad = 0
    for w in (20, 60, 120, 252):
        a = old_avg_corr(panel, w)
        b = pf._rolling_avg_corr(panel, w)
        if len(a) != len(b):
            print(f"  LEN {w}")
            bad += 1
            continue
        diff = 0
        for i, (x, y) in enumerate(zip(a, b)):
            if (x is None) != (y is None):
                print(f"  NONE {w}[{i}]: {x} vs {y}")
                diff += 1
            elif x is not None and not math.isclose(x, y, rel_tol=1e-9, abs_tol=1e-9):
                if diff < 2:
                    print(f"  VAL {w}[{i}]: {x} vs {y}")
                diff += 1
        print(f"window={w}: mismatches={diff}")
        bad += diff
    print("TOTAL MISMATCH =", bad)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
