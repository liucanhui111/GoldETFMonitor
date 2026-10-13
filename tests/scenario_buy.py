"""场景验证：把黄金降到上限以内，确认【买入路径】确实能算出一个非零的第一批金额。

用法：
    python tests/scenario_buy.py            # 用内存配置，不落盘
    python tests/scenario_buy.py --save     # 落盘（不要用真实配置做这个）
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import pipeline  # noqa: E402
from core.config import get_config  # noqa: E402


def main() -> int:
    cfg = get_config(refresh=True)
    # 内存里把黄金持仓降到 500 份（≈3.8% < 5% 上限），不动磁盘上的真实配置
    assets = json.loads(json.dumps(cfg.data.get("assets") or []))
    for a in assets:
        if a.get("asset_class") == "gold":
            a["shares"] = 500
    cfg.data["assets"] = assets

    do_save = "--save" in sys.argv
    snap = pipeline.run_full(cfg=cfg, use_cache_first=True, with_macro=False, save=do_save)

    p = snap["portfolio"]
    w = p["weights"]
    plan = p["plan"]
    marg = p.get("marginal") or {}
    comp = plan.get("comparison") or {}
    bt = marg.get("best_trade")

    print("=" * 62)
    print("总资产", w["total_assets"], " ETF", w["etf_value"], " 现金", w["cash_value"])
    print("大类权重", {k: round((v or 0) * 100, 2) for k, v in w["by_class"].items()})
    print("-" * 62)
    print("今日动作：", plan["today"]["text"])
    print("今日动作 key：", plan["today"]["key"])
    print("第一批合计：", plan["plan"]["total_first_batch_amount"], "元")
    print("picks：", [(i["name"], i["first_batch_amount"]) for i in plan["plan"]["picks"]])
    print("-" * 62)
    if bt:
        print("边际最优交易：", bt.get("name"), bt.get("amount"), "元",
              " 边际分", bt.get("marginal_score"),
              " 质量改善", bt.get("improve_quality"),
              " Δ60日概率", bt.get("improve_prob60"))
    else:
        print("边际最优交易：无（WAIT）")
    print("-" * 62)
    print("交易前后对比 verdict：", comp.get("verdict"))
    print("  ", comp.get("verdict_detail"))
    before = comp.get("before") or {}
    after = comp.get("after") or {}
    if before and after:
        for k in ("prob60", "exp60", "max_dd_1y", "quality"):
            print(f"   {k}: {before.get(k)} -> {after.get(k)}")
    print("-" * 62)
    lad = plan.get("ladder") or {}
    for b in (lad.get("batches") or []):
        print(f"   第{b.get('batch')}批 {b.get('amount')} 元  状态={b.get('status')}  {b.get('reason')}")
    print("=" * 62)

    # ---- 第一屏结论（headline）：买入态下必须显示真实金额与标的 ----
    from core import report as _report
    hl = ((plan.get("home") or {}).get("headline")) or {}
    txt = _report.render_daily(snap)
    top = txt.split("（下面开始是支撑")[0]
    print("---- 报告第一屏 ----")
    print(top)
    print("-" * 62)
    print("headline:", {k: hl.get(k) for k in
                        ("key", "answer", "big", "amount", "name",
                         "trade_today", "ref_name", "note")})
    ok_hl = True
    if plan["today"]["key"] in ("GREEN_BUY", "GREEN_PROBE"):
        if not (hl.get("amount") or 0) > 0 or not hl.get("name"):
            print("!! 断言失败：买入态下第一屏没有给出金额/标的")
            ok_hl = False
        if "0 元" in top.split("为什么")[0]:
            print("!! 断言失败：买入态下第一屏仍显示『0 元』")
            ok_hl = False
    else:
        # 非买入态：第一屏必须是 0 元 + 不买，且参考值只能出现在"参考"行里
        if (hl.get("amount") or 0) > 0 or hl.get("name"):
            print("!! 断言失败：非买入态下第一屏不该给出买入金额")
            ok_hl = False
    print("第一屏一致性:", "OK" if ok_hl else "FAIL")
    print("=" * 62)

    # 断言：黄金没超配时，如果边际引擎找到可执行交易，第一批金额必须 > 0
    gold_pct = (w["by_class"].get("gold") or 0) * 100
    first = float(plan["plan"]["total_first_batch_amount"] or 0)
    ok = ok_hl
    if gold_pct <= 5.0 and bt and float(bt.get("amount") or 0) > 0:
        if first <= 0:
            print("!! 断言失败：边际引擎给出正金额，但第一批合计为 0")
            ok = False
        else:
            print("OK：黄金未超配 → 买入路径连通，第一批金额 =", first)
    else:
        print("说明：本次黄金未超配但边际引擎未给出正金额（WAIT 合法）")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
