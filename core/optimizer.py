"""动态资产配置（战术层）。

战略约束永远不变：
    债券 ≈ 50%、股票 ≈ 45%、黄金 ≤ 5%（合计 100%）
战术层只做两件事：
    1. 根据市场状态在配置区间内决定"股票/债券/黄金"三类的目标；
    2. 在类内决定买哪几只、买多少（仍然受 min/max 权重约束）。

关键规则（不可绕过）：
    · 买入信号等级越低 → 股票目标越靠近区间下沿（用提高债券比例实现"等待"）；
    · 黄金目标绝不超过 gold_max（默认 5%）；
    · 任何单只 ETF 不得超出其 max_weight / min_weight；
    · 分配只使用"当天已知"的评分与当前仓位，不使用未来数据。
"""
from __future__ import annotations

from .signals import _map, _num

# 信号等级 → 风险资产投入强度
_GRADE_INTENSITY = {"S": 1.0, "A": 0.8, "B": 0.5, "C": 0.15, "D": 0.0, "E": 0.0}


def dynamic_targets(risk_state: str, entry_grade: str, cfg) -> dict:
    """返回 (大类目标权重, 说明)。"""
    ranges = cfg.get("dynamic_allocation", {})
    key = {
        "Risk On": "risk_on", "Normal": "normal",
        "Risk Off": "risk_off", "Crisis": "crisis",
    }.get(risk_state, "normal")
    rng = ranges.get(key) or ranges.get("normal") or {}
    bond_lo, bond_hi = rng.get("bond", [0.50, 0.50])
    stock_lo, stock_hi = rng.get("stock", [0.45, 0.45])

    intensity = _GRADE_INTENSITY.get(entry_grade, 0.15)
    stock = stock_lo + (stock_hi - stock_lo) * intensity
    bond = bond_lo + (bond_hi - bond_lo) * (1.0 - intensity)

    gold_cap = float(cfg.get("portfolio.gold_max", 0.05))
    gold_target_cfg = float(cfg.get("portfolio.target_class.gold", 0.05))
    gold = min(gold_cap, gold_target_cfg)

    # 归一（黄金先扣，剩下按债券:股票比例分）
    rest = 1.0 - gold
    tot = bond + stock
    if tot <= 0:
        bond, stock = 0.50, 0.45
        tot = 0.95
    bond = bond / tot * rest
    stock = stock / tot * rest

    note = (f"市场状态 {risk_state}；买入信号 {entry_grade} → 风险资产投入强度 "
            f"{int(intensity * 100)}%；黄金固定 {gold * 100:.1f}%（硬上限 {gold_cap * 100:.0f}%）")
    return {"bond": round(bond, 4), "stock": round(stock, 4), "gold": round(gold, 4),
            "note": note, "range_key": key, "intensity": intensity}


def allocate_within_class(rows: list[dict], class_budget: float, cfg,
                          class_name: str) -> dict:
    """类内分配：返回 {asset_id: weight}。"""
    if not rows or class_budget <= 0:
        return {}
    cfg_assets = {a["id"]: a for a in cfg.assets}

    if class_name == "bond":
        return _allocate_bond(rows, class_budget, cfg_assets)

    # 股票 / 黄金：按"买入质量分"在基准目标权重上做倾斜
    base: dict[str, float] = {}
    score: dict[str, float] = {}
    lo: dict[str, float] = {}
    hi: dict[str, float] = {}
    for r in rows:
        aid = r["asset"]["id"]
        base[aid] = float(r["asset"].get("target_weight") or 0.0)
        lo[aid] = float(r["asset"].get("min_weight") or 0.0)
        hi[aid] = float(r["asset"].get("max_weight") or 0.0)
        s = r["scores"]
        parts = []
        if s.get("buy_score") is not None:
            parts.append((s["buy_score"], 0.35))
        if s.get("entry_quality") is not None:
            parts.append((s["entry_quality"], 0.25))
        if s.get("prob60") is not None:
            parts.append((_map(s["prob60"], 35, 75), 0.25))
        if s.get("position") is not None:
            parts.append((100 - s["position"], 0.15))
        if parts:
            tot = sum(w for _, w in parts)
            v = sum(x * w for x, w in parts) / tot
        else:
            v = 50.0
        if s["flags"].get("no_chase"):
            v = min(v, 40.0)
        if s["flags"].get("no_bottom_fishing"):
            v = min(v, 35.0)
        score[aid] = v

    base_tot = sum(base.values())
    if base_tot <= 0:
        base = {k: 1.0 / len(base) for k in base}
    else:
        base = {k: v / base_tot for k, v in base.items()}

    mean_score = sum(score.values()) / len(score)
    raw = {aid: base[aid] * (score[aid] / mean_score if mean_score > 0 else 1.0)
           for aid in base}

    # 迭代裁剪到 [min, max]（相对于总组合的绝对值）
    weights = _clip_and_normalize(raw, class_budget, lo, hi)
    return weights


def _allocate_bond(rows: list[dict], budget: float, cfg_assets: dict) -> dict:
    """债券内部：以目标权重为基准，用趋势/位置做小幅倾斜（±5pp 以内）。"""
    base = {}
    for r in rows:
        aid = r["asset"]["id"]
        base[aid] = float(r["asset"].get("target_weight") or 0.25)
    tot = sum(base.values()) or 1.0
    base = {k: v / tot for k, v in base.items()}

    tilts: dict[str, float] = {}
    for r in rows:
        aid = r["asset"]["id"]
        s = r["scores"]
        t = 0.0
        if s.get("trend") is not None:
            t += (s["trend"] - 50) / 50 * 0.5     # 趋势强 → 略多
        if s.get("position") is not None:
            t += (50 - s["position"]) / 50 * 0.5  # 位置低 → 略多
        if s.get("shortterm") is not None:
            t += (50 - s["shortterm"]) / 50 * 0.3
        tilts[aid] = max(-1.0, min(1.0, t))
    mean_t = sum(tilts.values()) / len(tilts) if tilts else 0.0
    raw = {}
    for aid in base:
        adjust = 1.0 + 0.10 * (tilts[aid] - mean_t)   # 最多 ±10% 相对调整
        raw[aid] = base[aid] * adjust
    lo = {r["asset"]["id"]: 0.0 for r in rows}
    hi = {r["asset"]["id"]: float(cfg_assets.get(r["asset"]["id"], {}).get("max_weight") or 0.30)
          for r in rows}
    return _clip_and_normalize(raw, budget, lo, hi)


def _clip_and_normalize(raw: dict[str, float], budget: float,
                        lo: dict[str, float], hi: dict[str, float],
                        iters: int = 60) -> dict:
    """把类内相对比例 × 类预算 → 绝对权重，并把绝对权重裁剪到 [min,max]。

    水量填充法：违反上下限的先固定，剩余资产按原始比例重新分配剩余预算。
    """
    keys = list(raw)
    if not keys:
        return {}
    tot = sum(raw.values())
    w = {k: (v / tot * budget if tot > 0 else budget / len(keys)) for k, v in raw.items()}
    fixed: set[str] = set()
    for _ in range(iters):
        changed = False
        for k in keys:
            if k in fixed:
                continue
            if w[k] < lo.get(k, 0.0) - 1e-12:
                w[k] = lo.get(k, 0.0)
                fixed.add(k)
                changed = True
            elif w[k] > hi.get(k, 9.0) + 1e-12:
                w[k] = hi.get(k, 9.0)
                fixed.add(k)
                changed = True
        if not changed:
            break
        free = [k for k in keys if k not in fixed]
        if not free:
            break
        residual = budget - sum(w[k] for k in fixed)
        if residual <= 1e-12:
            for k in free:
                w[k] = 0.0
            break
        free_raw = sum(max(0.0, raw[k]) for k in free)
        for k in free:
            w[k] = (max(0.0, raw[k]) / free_raw * residual
                    if free_raw > 0 else residual / len(free))
    return {k: round(v, 5) for k, v in w.items()}


def optimize(rows: list[dict], risk_state: str, entry_grade: str, cfg) -> dict:
    """完整战术配置：大类目标 → 类内分配 → 绝对权重。"""
    targets = dynamic_targets(risk_state, entry_grade, cfg)
    cls_of = {r["asset"]["id"]: (r["asset"].get("asset_class") or "other") for r in rows}

    out: dict[str, float] = {}
    detail: dict = {}
    for cls in ("bond", "stock", "gold"):
        sub = [r for r in rows if (r["asset"].get("asset_class") or "other") == cls]
        w = allocate_within_class(sub, targets.get(cls, 0.0), cfg, cls)
        out.update(w)
        detail[cls] = w

    gold_max = float(cfg.get("portfolio.gold_max", 0.05))
    gold_sum = sum(v for k, v in out.items() if cls_of.get(k) == "gold")
    # 黄金硬上限：超出部分按比例压缩，释放的权重让给债券（防守）
    if gold_sum > gold_max + 1e-9 and gold_sum > 0:
        scale = gold_max / gold_sum
        for k in list(out):
            if cls_of.get(k) == "gold":
                out[k] = out[k] * scale
        freed = gold_sum - gold_max
        bond_ids = [k for k in out if cls_of.get(k) == "bond"]
        if bond_ids:
            per = freed / len(bond_ids)
            for k in bond_ids:
                out[k] += per

    # 债券+股票 归一到 (1 - 黄金实际)，保证总和=1 且黄金不被放大
    gold_now = sum(v for k, v in out.items() if cls_of.get(k) == "gold")
    block = {k: v for k, v in out.items() if cls_of.get(k) != "gold"}
    block_tot = sum(block.values())
    want = 1.0 - gold_now
    if block_tot > 0:
        for k in block:
            out[k] = block[k] / block_tot * want
    tot = sum(out.values())
    if tot > 0:
        out = {k: round(v / tot, 5) for k, v in out.items()}

    stock_sum = sum(v for k, v in out.items() if cls_of.get(k) == "stock")
    stock_max = float(cfg.get("portfolio.stock_max", 0.50))
    gold_now = sum(v for k, v in out.items() if cls_of.get(k) == "gold")
    return {
        "targets": targets,
        "weights": out,
        "by_class": detail,
        "class_weights": {
            "bond": round(sum(detail.get("bond", {}).values()), 4),
            "stock": round(sum(detail.get("stock", {}).values()), 4),
            "gold": round(sum(detail.get("gold", {}).values()), 4),
        },
        "realized_class_weights": {
            "bond": round(sum(v for k, v in out.items() if cls_of.get(k) == "bond"), 4),
            "stock": round(stock_sum, 4),
            "gold": round(gold_now, 4),
        },
        "constraints_ok": bool(gold_now <= gold_max + 1e-9),
        "notes": targets["note"],
    }


def current_weights(rows: list[dict], cfg=None) -> dict:
    """按【总资产】口径计算当前实际权重（债券/股票/黄金/现金 四项合计 100%）。

    资金口径（真实、不虚构）：
        现金/待投资资金 = 总资产 - ETF 市值
        总资产优先取用户在配置里填写的 total_assets；
        未填写时退回 portfolio.cash；两者都没有则现金按 0 处理并明确提示。

    返回的 by_asset 是【占 ETF 市值】的权重（和为 1，供历史回测对齐用）；
    by_asset_total / by_class 是【占总资产】的权重（含现金）。
    """
    mv = {}
    for r in rows:
        v = (r["snapshot"].get("market_value") or 0.0)
        mv[r["asset"]["id"]] = float(v)
    etf_value = sum(mv.values())

    # 目标权重总和（用于一致性校验）
    target_sum = sum(float(r["asset"].get("target_weight") or 0.0) for r in rows)

    if etf_value <= 0:
        return {
            "total_assets": 0.0, "etf_value": 0.0, "cash_value": 0.0,
            "cash_pct": 0.0, "by_asset": {}, "by_asset_total": {}, "by_class": {},
            "source": "持仓未填写", "target_sum_pct": round(target_sum * 100, 2),
        }

    total_assets = 0.0
    cash = None
    source = "按持仓市值计算"
    if cfg is not None:
        declared = float(cfg.declared_total_assets or 0.0)
        declared_cash = cfg.declared_cash
        if declared > 0:
            total_assets = declared
            cash = declared - etf_value        # 可能为负 → 由一致性检查报警
            source = "按『总资产 - ETF市值』计算（设置页填写的总资产）"
        elif declared_cash is not None:
            cash = declared_cash
            total_assets = etf_value + declared_cash
            source = "按『ETF市值 + 填写的现金』计算"
    if cash is None:
        total_assets = etf_value
        cash = 0.0
        source = "按持仓市值计算（未填写总资产/现金，现金按 0 处理）"

    if total_assets <= 0:
        total_assets = etf_value + max(0.0, cash)
    denom = total_assets if total_assets > 0 else etf_value

    by_asset = {k: v / etf_value for k, v in mv.items()}
    by_asset_total = {k: v / denom for k, v in mv.items()}
    by_class: dict[str, float] = {}
    for r in rows:
        cls = r["asset"].get("asset_class") or "other"
        by_class[cls] = by_class.get(cls, 0.0) + by_asset_total[r["asset"]["id"]]
    by_class["cash"] = max(0.0, cash) / denom

    return {
        "total_assets": round(total_assets, 2),
        "etf_value": round(etf_value, 2),
        "cash_value": round(cash, 2),
        "cash_pct": round(max(0.0, cash) / denom * 100, 2),
        "by_asset": by_asset,
        "by_asset_total": by_asset_total,
        "by_class": by_class,
        "source": source,
        "target_sum_pct": round(target_sum * 100, 2),
        "portfolio_id": (cfg.portfolio_id if cfg is not None else ""),
    }


def format_weights(weights: dict[str, float], rows: list[dict]) -> str:
    names = {r["asset"]["id"]: r["asset"].get("name") for r in rows}
    return "、".join(f"{names.get(k, k)} {v * 100:.1f}%" for k, v in
                     sorted(weights.items(), key=lambda x: -x[1]))


__all__ = ["optimize", "dynamic_targets", "allocate_within_class",
           "current_weights", "format_weights", "_num"]
