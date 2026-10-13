"""记录 / 比对 core.portfolio 滚动统计的逐点输出（性能优化不改结果）。

用法：
    python tests/ref_rolling.py record   # 用当前实现生成基准
    python tests/ref_rolling.py check    # 与基准逐点比对
"""
from __future__ import annotations

import json
import math
import os
import random
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
REF = os.path.join(BASE, "tests", "_rolling_ref.json")


def make_data(seed: int, n: int):
    rnd = random.Random(seed)
    nav = [1.0]
    rets = []
    for _ in range(n - 1):
        r = rnd.gauss(0.0004, 0.011)
        rets.append(r)
        nav.append(nav[-1] * (1 + r))
    # 制造部分 None 收益（模拟停牌/缺失）
    for i in range(3, len(rets), 37):
        rets[i] = None
    return nav, rets


def compute():
    from core.portfolio import _rolling_dd, _rolling_relpos, _rolling_vol

    out = {}
    for seed in (1, 2, 3):
        nav, rets = make_data(seed, 400)
        for w in (20, 60, 252):
            dd = _rolling_dd(nav, w)
            rp = _rolling_relpos(nav, w)
            vol = _rolling_vol(rets, w)
            out[f"dd|{seed}|{w}"] = [None if v is None else round(v, 9) for v in dd]
            out[f"rp|{seed}|{w}"] = [None if v is None else round(v, 9) for v in rp]
            out[f"vol|{seed}|{w}"] = [None if v is None else round(v, 9) for v in vol]
    return out


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "check"
    cur = compute()
    if mode == "record":
        with open(REF, "w", encoding="utf-8") as f:
            json.dump(cur, f)
        print(f"recorded {len(cur)} series")
        return 0
    with open(REF, encoding="utf-8") as f:
        ref = json.load(f)
    bad = 0
    checked = 0
    for k, want in ref.items():
        got = cur.get(k)
        if got is None:
            print(f"  MISSING {k}")
            bad += 1
            continue
        if len(got) != len(want):
            print(f"  LEN {k}: {len(got)} vs {len(want)}")
            bad += 1
            continue
        for i, (a, b) in enumerate(zip(got, want)):
            if (a is None) != (b is None):
                print(f"  NONE-MISMATCH {k}[{i}]")
                bad += 1
                break
            if a is not None and not math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9):
                print(f"  VALUE {k}[{i}]: {a} vs {b}")
                bad += 1
                break
        checked += 1
    print(f"checked {checked} series, mismatches={bad}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
