"""概率校准（Calibration）——与 Naive Base-rate 基线比较。

口径纪律（继承自 STRATEGY_VALIDATION 的最终修正）
------------------------------------------------
· Brier 只作"概率误差"展示，**禁止**用 `Brier < 0.25` 当"校准良好"判据；
· 必须与同测试集的 **Naive Base-rate 基线**（永远预测基础胜率）比较：
      Brier Skill Score = 1 − Brier_model / Brier_naive
  BSS > 0 才算模型校准优于"什么都不算"的基线；BSS ≤ 0 → 概率不可当可靠预测。
· 概率校准是升级可信等级的必要条件之一，不达标一律保持【研究级】。
"""
from __future__ import annotations


def brier_score(probs, outcomes) -> float | None:
    """probs/outcomes 为等长序列；prob 为 0~1，outcome 为 0/1。"""
    ps = []
    ys = []
    for p, y in zip(probs, outcomes):
        if p is None or y is None:
            continue
        ps.append(float(p))
        ys.append(float(y))
    if not ps:
        return None
    return sum((p - y) ** 2 for p, y in zip(ps, ys)) / len(ps)


def naive_brier(base_rate_pct: float | None, outcomes) -> float | None:
    ys = [float(y) for y in outcomes if y is not None]
    if not ys or base_rate_pct is None:
        return None
    b = max(0.0, min(1.0, float(base_rate_pct) / 100.0))
    return sum((b - y) ** 2 for y in ys) / len(ys)


def brier_skill_score(model_brier: float | None,
                      naive: float | None) -> float | None:
    if model_brier is None or naive is None or naive <= 1e-12:
        return None
    return 1.0 - float(model_brier) / float(naive)


def calibration_buckets(probs, outcomes,
                        edges=(0.0, 0.4, 0.55, 0.7, 0.85, 1.01)) -> list[dict]:
    """分桶看"预测 X% 的那批，实际是否约 X%"。"""
    rows = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        ps = [(float(p), float(y)) for p, y in zip(probs, outcomes)
              if p is not None and y is not None and lo <= float(p) < hi]
        if not ps:
            rows.append({"bucket": f"{lo * 100:.0f}~{hi * 100:.0f}%", "samples": 0,
                         "avg_pred_pct": None, "realized_win_rate_pct": None})
            continue
        ap = sum(p for p, _ in ps) / len(ps) * 100
        rw = sum(y for _, y in ps) / len(ps) * 100
        rows.append({"bucket": f"{lo * 100:.0f}~{hi * 100:.0f}%",
                     "samples": len(ps), "avg_pred_pct": round(ap, 1),
                     "realized_win_rate_pct": round(rw, 1)})
    return rows


def calibration_report(probs, outcomes, base_rate_pct: float | None) -> dict:
    """一次性给出 model/naive Brier、BSS、分桶与结论。"""
    mb = brier_score(probs, outcomes)
    nb = naive_brier(base_rate_pct, outcomes)
    bss = brier_skill_score(mb, nb)
    n = sum(1 for p in probs if p is not None)
    if mb is None or nb is None:
        verdict = "不可用（概率或基线缺失）"
    elif bss is not None and bss > 0:
        verdict = "模型校准优于基础率基线"
    else:
        verdict = "模型校准未优于基础率基线（概率展示不可当可靠预测）"
    return {
        "n": n,
        "brier": round(mb, 4) if mb is not None else None,
        "naive_brier": round(nb, 4) if nb is not None else None,
        "brier_skill_score": round(bss, 4) if bss is not None else None,
        "buckets": calibration_buckets(probs, outcomes),
        "verdict": verdict,
        # 明确写死旧口径已废弃，防止文档回退
        "note": ("Brier 仅作概率误差展示；校准必须与同测试集 Naive Base-rate "
                 "基线比较，禁止用 Brier<0.25 作为校准良好判据。"),
    }
