"""评分与分级。

四个单 ETF 评分的方向定义（务必与 UI 文案一致）：
    TrendScore       高 = 趋势越强（上涨结构越完整）
    PositionScore    高 = 位置越高（越贵）；低 = 越便宜/越低位
    ShortTermScore   高 = 短线越超买/越热；低 = 越超跌
    SwingScore       高 = 越接近波段底部（波段机会越好）；低 = 越接近波段顶部

注意：低位置 ≠ 可以买。所有分数都必须与"历史胜率 / 期望收益 / 止跌确认"
一起看，绝不允许单指标下结论。
"""
from __future__ import annotations

from typing import Any


def clip(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, float(v)))


# --------------------------------------------------------------------------
# 分类
# --------------------------------------------------------------------------
def classify_return20(v: float | None, bands: dict) -> str:
    if v is None:
        return "暂无数据"
    if v >= bands.get("fast_up", 15):
        return "快速上涨"
    if v >= bands.get("up", 5):
        return "正常上涨"
    if v > bands.get("flat_down", -2):
        return "震荡"
    if v > bands.get("deep_down", -15):
        return "明显下跌"
    return "深度下跌"


def classify_bias20(v: float | None, bands: dict) -> str:
    if v is None:
        return "暂无数据"
    if v >= bands.get("very_high", 8):
        return "明显高位"
    if v >= bands.get("high", 4):
        return "偏高"
    if v > bands.get("low", -4):
        return "正常"
    if v > bands.get("very_low", -8):
        return "偏低"
    return "明显超跌"


def classify_relpos(v: float | None, bands: list) -> str:
    if v is None:
        return "暂无数据"
    lo1, lo2, hi1, hi2 = bands
    if v < lo1:
        return "极低位"
    if v < lo2:
        return "低位"
    if v < hi1:
        return "中位"
    if v < hi2:
        return "高位"
    return "极高位"


def classify_wr(v: float | None, bands: list) -> str:
    if v is None:
        return "暂无数据"
    ob, mid, os_ = bands
    if v > ob:
        return "超买"
    if v > mid:
        return "偏强"
    if v > os_:
        return "中性/偏弱"
    return "超卖"


def classify_trend(score: float | None) -> str:
    if score is None:
        return "暂无数据"
    if score >= 75:
        return "强势上涨"
    if score >= 60:
        return "上涨"
    if score >= 40:
        return "震荡"
    if score >= 25:
        return "偏弱"
    return "下降"


def classify_shortterm(score: float | None, atr_pct: float | None) -> str:
    if score is None:
        return "暂无数据"
    if atr_pct is not None and atr_pct >= 3.0:
        return "高波动"
    if score >= 75:
        return "超买"
    if score >= 58:
        return "偏强"
    if score > 42:
        return "正常"
    return "超跌"


# --------------------------------------------------------------------------
# 四个基础评分
# --------------------------------------------------------------------------
def trend_score(ind: dict, i: int = -1) -> float | None:
    close = ind["close"]
    if not close or close[i] is None:
        return None
    c = float(close[i])
    mas = ind["ma"]
    s = 0.0

    def above(w: int) -> bool | None:
        m = (mas.get(f"ma{w}") or [None])[i]
        return None if m is None else c > m

    def rising(w: int) -> bool | None:
        sl = (ind.get("ma_slope") or {}).get(f"ma{w}", [None])[i]
        return None if sl is None else sl > 0

    for w, pts in ((20, 14), (60, 14), (200, 12), (120, 6)):
        a = above(w)
        if a is not None:
            s += pts if a else 0

    m20, m60, m200 = (mas.get("ma20") or [None])[i], (mas.get("ma60") or [None])[i], \
                     (mas.get("ma200") or [None])[i]
    if m20 and m60 and m20 > m60:
        s += 12
    if m60 and m200 and m60 > m200:
        s += 10

    for w, pts in ((20, 8), (60, 8)):
        if rising(w):
            s += pts

    hist = (ind.get("macd") or {}).get("hist", [None])[i]
    if hist is not None:
        s += 8 if hist > 0 else 0
    dif = (ind.get("macd") or {}).get("dif", [None])[i]
    dea = (ind.get("macd") or {}).get("dea", [None])[i]
    if dif is not None and dea is not None and dif > dea:
        s += 4

    r = (ind.get("rsi14") or [None])[i]
    if r is not None:
        if 50 <= r < 70:
            s += 8
        elif r >= 70:
            s += 5
        elif 40 <= r < 50:
            s += 3
    return clip(s)


def position_score(ind: dict, bands: list, i: int = -1) -> float | None:
    rp1 = ((ind.get("relpos_series") or {}).get("1y") or [None])[i]
    rp2 = ((ind.get("relpos_series") or {}).get("2y") or [None])[i]
    dd2 = ((ind.get("curdd_series") or {}).get("2y") or [None])[i]
    if rp1 is None and rp2 is None:
        return None
    parts: list[tuple[float, float]] = []
    if rp1 is not None:
        parts.append((rp1, 0.40))
    if rp2 is not None:
        parts.append((rp2, 0.35))
    if dd2 is not None:
        # 回撤 -30% 及以上视为位置极低
        recovery = clip(100 + dd2 / 30.0 * 100, 0, 100)
        parts.append((recovery, 0.25))
    total_w = sum(w for _, w in parts)
    return clip(sum(v * w for v, w in parts) / total_w)


def short_term_score(ind: dict, i: int = -1) -> float | None:
    r20 = (ind.get("ret20") or [None])[i]
    b20 = (ind.get("bias20") or [None])[i]
    wr = (ind.get("wr14") or [None])[i]
    rsi_ = (ind.get("rsi14") or [None])[i]
    if r20 is None and b20 is None and wr is None:
        return None
    parts: list[tuple[float, float]] = []
    if r20 is not None:
        parts.append((clip(50 + r20 * 1.6), 0.28))
    if b20 is not None:
        parts.append((clip(50 + b20 * 3.2), 0.26))
    if wr is not None:
        parts.append((clip(wr + 100), 0.24))
    if rsi_ is not None:
        parts.append((clip(rsi_), 0.22))
    total_w = sum(w for _, w in parts)
    return clip(sum(v * w for v, w in parts) / total_w)


def swing_score(ind: dict, i: int = -1) -> float | None:
    """高 = 接近/确认波段底部；低 = 接近/确认波段顶部。"""
    states = ind.get("swing_states") or []
    state = states[i] if (states and -len(states) <= i < len(states)) else None
    base = {
        "底部确认": 88, "疑似底部": 72, "底部观察": 62,
        "波段中部": 50, "顶部观察": 34, "疑似顶部": 22, "顶部确认": 12,
    }.get(state)
    if base is None:
        return None
    b20 = (ind.get("bias20") or [None])[i]
    r20 = (ind.get("ret20") or [None])[i]
    wr = (ind.get("wr14") or [None])[i]
    adj = 0.0
    if b20 is not None:
        adj += clip(-b20 * 1.2, -12, 12)
    if r20 is not None:
        adj += clip(-r20 * 0.5, -10, 10)
    if wr is not None:
        adj += clip((-50 - wr) / 50 * 6, -6, 6)
    revs = ind.get("wr_reversal_series") or []
    if revs and -len(revs) <= i < len(revs) and revs[i] and revs[i].get("confirmed_reversal"):
        adj += 6
    return clip(base + adj)


# --------------------------------------------------------------------------
# 组合评分
# --------------------------------------------------------------------------
def buy_score(factors: dict, weights: dict) -> float:
    """factors 里每项都是 0-100，越高越有利。"""
    total_w = sum(float(w) for w in weights.values()) or 1.0
    acc = 0.0
    for k, w in weights.items():
        v = factors.get(k)
        if v is None:
            continue
        acc += clip(v, 0, 100) * float(w)
    return clip(acc / total_w)


def entry_quality(factors: dict, weights: dict) -> float:
    return buy_score(factors, weights)


def sample_grade(n: int, bands: dict) -> tuple[str, str]:
    """返回 (样本数量评级, 样本可信度)。"""
    if n < bands.get("severe", 20):
        return "严重不足", "低"
    if n < bands.get("low", 50):
        return "偏少", "低"
    if n < bands.get("fair", 100):
        return "一般", "中"
    if n < bands.get("good", 200):
        return "较好", "较高"
    return "充分", "高"


def confidence_interval(p: float, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wald 95% 置信区间（百分数）。"""
    if n <= 0:
        return 0.0, 100.0
    p_ = max(0.0, min(1.0, p / 100.0))
    se = (p_ * (1 - p_) / n) ** 0.5
    lo = max(0.0, (p_ - z * se)) * 100
    hi = min(1.0, (p_ + z * se)) * 100
    return round(lo, 1), round(hi, 1)


def wilson_ci(p_pct: float, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score 95% 置信区间（百分数入/出）。

    小样本（n<50）下比 Wald 更可靠，不会出现负下界或超上界；
    用于独立事件胜率的置信区间（P0-4/P0-9）。
    """
    if n <= 0:
        return 0.0, 100.0
    p_ = max(0.0, min(1.0, p_pct / 100.0))
    denom = 1.0 + z * z / n
    center = (p_ + z * z / (2 * n)) / denom
    half = z * ((p_ * (1 - p_) / n + z * z / (4 * n * n)) ** 0.5) / denom
    return round(max(0.0, center - half) * 100, 1), \
        round(min(1.0, center + half) * 100, 1)


def etf_grade(prob60: float | None, exp_ret60: float | None, payoff: float | None,
              samples: int, trend_state: str | None,
              bands: dict) -> tuple[str, str]:
    """单 ETF 值得买规则 A~E。返回 (等级, 说明)。"""
    if prob60 is None or samples < int(bands.get("A", {}).get("min_samples", 50)):
        return "样本不足", "历史相似样本不足，无法给出等级；请勿仅凭单指标决策"

    A = bands.get("A", {})
    if (prob60 >= A.get("prob_60", 65) and (exp_ret60 or 0) > A.get("exp_ret_60", 2.0)
            and (payoff or 0) >= A.get("payoff", 1.5)
            and trend_state not in ("下降",)):
        return "A", "值得分批配置"
    B = bands.get("B", {})
    if prob60 >= B.get("prob_60", 58) or (exp_ret60 or -1) > B.get("exp_ret_60", 0.0):
        return "B", "小仓试探"
    C = bands.get("C", {})
    if prob60 >= C.get("prob_60", 50) or (exp_ret60 or -1) > -0.5:
        return "C", "WAIT（等待更好条件）"
    if prob60 < 50 and (exp_ret60 or 0) <= 0:
        if trend_state == "下降" and (exp_ret60 or 0) < -3:
            return "E", "明显下降趋势 + 期望收益为负，禁止抄底"
        return "D", "不建议配置"
    return "D", "不建议配置"
