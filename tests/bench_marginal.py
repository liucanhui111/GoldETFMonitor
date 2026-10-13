"""组合边际模拟引擎的功能与耗时验证（第三阶段）。"""
from __future__ import annotations

import os
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("GOLD_ETF_HOME", BASE)
sys.path.insert(0, BASE)


def main() -> int:
    from core.config import get_config
    from core.marginal import PortfolioSimulator
    from core.pipeline import run_full

    cfg = get_config(refresh=True)
    snap = run_full(cfg, use_cache_first=True, with_macro=False, save=False)
    if not snap.get("ok"):
        print("pipeline failed:", snap.get("message"))
        return 1
    rows = [r for r in snap["rows"] if r.get("snapshot")]
    cur = snap["portfolio"]["weights"]
    p = snap["portfolio"]

    datasets = {r["asset"]["id"]: {"dates": r["ind"]["dates"], "close": r["ind"]["close"]}
                for r in rows}
    names = {r["asset"]["id"]: r["asset"].get("name") for r in rows}
    inds = {r["asset"]["id"]: r["ind"] for r in rows}
    class_of = {r["asset"]["id"]: r["asset"]["asset_class"] for r in rows}
    base_mv = {r["asset"]["id"]: float(r["snapshot"].get("market_value") or 0) for r in rows}

    sim = PortfolioSimulator(
        datasets, names, inds, class_of, cfg,
        base_mv=base_mv,
        total_assets=float(cur.get("total_assets") or 0),
        cost=cfg.trade_cost(),
        targets_class=p["target_class"],
    )
    print(f"[baseline] total={sim.total_assets:,.0f} etf={sim.etf_value:,.0f} "
          f"cash={sim.cash:,.0f}")
    base = sim.evaluate(sim.base_mv)
    if base is None:
        print("baseline evaluate failed")
        return 1
    print(f"[baseline classes] {{k: round(v*100,2) for k,v in base['classes'].items()}}"
          .replace("{k: round(v*100,2) for k,v in base['classes'].items()}",
                   str({k: round(v * 100, 2) for k, v in base["classes"].items()})))
    print(f"[baseline] prob20={base['prob20']} prob60={base['prob60']} "
          f"oos60={base['prob60_oos']} exp60={base['exp60']} "
          f"dd1y={base['max_dd_1y']} vol={base['vol_annual']} Q={base['quality']}")

    print(f"[grid] {sim.amount_grid()}")

    t0 = time.time()
    res = sim.optimize_all(list(base_mv.keys()), top_n=3)
    dt = time.time() - t0
    print(f"[optimize_all] {dt:.2f}s, evals={res['n_eval']}")
    for aid, r in res["per_asset"].items():
        if not r.get("available"):
            print(f"  {names.get(aid)}: 不可用 {r.get('message')}")
            continue
        print(f"  {r['name']:<16} 最优={int(r['amount']):>6,} 元 "
              f"WAIT={r['is_wait']} 上限={int(r['hard_cap']):,} | {r['reason'][:44]}")
        for s in r["simulations"]:
            print(f"      金额 {int(s['amount']):>6,} → 改善分 {s['marginal_score']:>5} "
                  f"Q {s['quality']:>5} (Δ{s['improve_quality']:>+5}) "
                  f"prob60 {s['prob60']:>5} (Δ{s['improve_prob60']:>+4}) "
                  f"exp60 {s['exp60']:>6} dd1y {s['max_dd_1y']:>7}")

    bt = res.get("best_trade")
    if bt:
        print(f"[best] {bt['name']} {int(bt['amount']):,} 元 "
              f"改善分 {bt['marginal_score']} 质量 {bt['quality_before']} → {bt['quality']} "
              f"(Δ{bt['improve_quality']:+}) prob60 改善 {bt['improve_prob60']:+}pp")
    else:
        print("[best] 今天没有任何买入能改善组合质量 → WAIT")

    gold = next((r["asset"]["id"] for r in rows if r["asset"]["asset_class"] == "gold"), None)
    if gold and base["classes"].get("gold", 0) > cfg.get("portfolio.gold_max", 0.05):
        rb = sim.simulate_rebalance(gold, cfg.get("simulation.rebalance_levels_pct", [10, 8, 5]))
        print(f"[rebalance] 黄金当前 {rb['current_pct']}% 上限 {rb['cap_pct']}%")
        for o in rb.get("options", []):
            print(f"      降到 {o['target_pct']}% → 卖出 {int(o['sell_amount']):,} 元, "
                  f"Q {o['quality']} (改善 {o['improve_quality']:+}), prob60 {o['prob60']}")
        if rb.get("best"):
            print(f"      → 建议 {rb['best']['target_pct']}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
