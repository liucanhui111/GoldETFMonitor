"""基准：测量一次完整流水线与"一次组合模拟"的耗时（第三阶段性能预算用）。"""
from __future__ import annotations

import os
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("GOLD_ETF_HOME", BASE)
sys.path.insert(0, BASE)


def main() -> int:
    from core.config import get_config
    from core.pipeline import run_full

    cfg = get_config(refresh=True)
    t0 = time.time()
    snap = run_full(cfg, progress=lambda m: print("  ·", m, flush=True),
                    use_cache_first=True, with_macro=False, save=False)
    dt = time.time() - t0
    print(f"[pipeline] ok={snap.get('ok')} 耗时={dt:.2f}s")
    if not snap.get("ok"):
        print(snap.get("message"))
        return 1

    p = snap["portfolio"]
    panel = p["panel"]
    print(f"[panel] bars={len(panel.dates)} assets={len(panel.assets)} "
          f"weights={ {k: round(v,4) for k,v in panel.actual_weights.items()} }")

    # ---- 单次组合统计耗时 ----
    from core import portfolio as pf
    from core.histstats import SimilarStateEngine

    datasets = {r["asset"]["id"]: {"dates": r["ind"]["dates"], "close": r["ind"]["close"]}
                for r in snap["rows"] if r.get("snapshot")}
    names = {r["asset"]["id"]: r["asset"].get("name") for r in snap["rows"] if r.get("snapshot")}
    asset_inds = {r["asset"]["id"]: r["ind"] for r in snap["rows"] if r.get("snapshot")}
    cur = p["weights"]
    cost = cfg.trade_cost()

    t1 = time.time()
    p2 = pf.Panel(datasets, cur["by_asset"], names)
    t_panel = time.time() - t1

    t1 = time.time()
    feats = pf.build_portfolio_features(p2, asset_inds, {}, cfg)
    t_feat = time.time() - t1

    t1 = time.time()
    eng = SimilarStateEngine(feats, cfg.get("hist.feature_weights", {}), cfg)
    h = eng.all_horizons(len(p2.dates) - 1, p2.nav, cost)
    t_eng = time.time() - t1

    print(f"[timing] Panel={t_panel*1000:.0f}ms  features={t_feat*1000:.0f}ms  "
          f"engine(all horizons)={t_eng*1000:.0f}ms")
    print(f"[hist] 60d win={h[60].get('win_rate_pct')} samples={h[60].get('samples')} "
          f"oos={h[60].get('oos_win_rate_pct')} exp={h[60].get('expected_return_pct')}")

    # ---- 估算 N 次模拟总耗时 ----
    n_sims = 20
    t1 = time.time()
    for i in range(n_sims):
        w = dict(cur["by_asset"])
        feats2 = pf.build_portfolio_features(p2, asset_inds, {}, cfg)
        eng2 = SimilarStateEngine(feats2, cfg.get("hist.feature_weights", {}), cfg)
        eng2.all_horizons(len(p2.dates) - 1, p2.nav, cost)
    per = (time.time() - t1) / n_sims
    print(f"[budget] 单次模拟≈{per*1000:.0f}ms → 20 次≈{per*n_sims:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
