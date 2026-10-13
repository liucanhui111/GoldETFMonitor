"""探针：为什么高概率却不买 —— 打印各 ETF 样本数/买入质量与组合门槛真实数字。"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core.config import get_config
from core.pipeline import run_full


def main() -> int:
    cfg = get_config(refresh=True)
    snap = run_full(cfg, progress=None, use_cache_first=True, with_macro=False,
                    save=False)
    p = snap.get("portfolio") or {}
    out: list[str] = []
    out.append(f"ok={snap.get('ok')}")
    dp = (p.get("decision") or {}).get("dec_prob") or {}
    out.append("--- 组合决策概率 ---")
    out.append(f"prob_used={dp.get('prob_used')} source={dp.get('source')} "
               f"indep={dp.get('independent_events')} base={dp.get('base_rate')} "
               f"lift={dp.get('lift_pp')}")
    g = (p.get("plan") or {}).get("gate") or {}
    out.append("--- gate ---")
    for k in ("grade", "prob_used", "prob_source", "samples", "oos_samples",
              "exp60", "payoff", "dd60", "use_oos", "credibility", "blocked"):
        out.append(f"  {k} = {g.get(k)}")
    out.append(f"  conds = {g.get('conds')}")
    out.append(f"  unmet = {g.get('unmet')}")
    out.append("--- 各 ETF（h=60）样本数 / 买入质量 ---")
    for r in (snap.get("rows") or []):
        a = r.get("asset") or {}
        h60 = ((r.get("hist") or {}).get(60) or {})
        sc = r.get("scores") or {}
        out.append(
            f"  {a.get('name'):<18} class={a.get('asset_class'):<6} "
            f"samples={h60.get('samples')} grade={h60.get('sample_grade')} "
            f"cred={h60.get('credibility')} mult={h60.get('distance_mult')} "
            f"win60={h60.get('win_rate_pct')} | 买入质量={sc.get('grade')} "
            f"entry_quality={sc.get('entry_quality')} buy_score={sc.get('buy_score')}")
    # 组合层：黄金占比 / 硬上限
    w = p.get("weights") or {}
    out.append("--- 组合层 ---")
    out.append(f"weights={w}")
    out.append(f"gold_cap={p.get('gold_cap')}")
    txt = "\n".join(str(x) for x in out)
    with open(os.path.join(ROOT, "_probe_gate_out.txt"), "w", encoding="utf-8") as fh:
        fh.write(txt)
    print("written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
