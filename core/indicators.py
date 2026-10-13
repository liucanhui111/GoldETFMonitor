"""技术指标引擎。

**严格无未来函数**：本模块所有函数都只使用截止当前 bar 的历史数据。
凡是需要"确认"的逻辑（波段顶底），都通过"事件发生后才回填"的方式实现：
pivot 在 t 时刻只有在 t 之后价格回撤超过阈值时才被标记为已确认，
且 confirm_idx 记录确认发生的 bar，保证回测时不会提前知道未来。

序列一律返回与输入等长的 list，前置不足处用 None 填充。
"""
from __future__ import annotations

import math
from typing import Iterable, Sequence

Num = float | None


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------
def _clean(vals: Sequence[Num]) -> list[float]:
    return [float(v) if v is not None else float("nan") for v in vals]


def sma(vals: Sequence[Num], window: int) -> list[Num]:
    out: list[Num] = [None] * len(vals)
    if window <= 0:
        return out
    acc = 0.0
    cnt = 0
    for i, v in enumerate(vals):
        if v is None:
            out[i] = out[i - 1] if i > 0 else None
            continue
        acc += float(v)
        cnt += 1
        if cnt > window:
            drop = vals[i - window]
            if drop is not None:
                acc -= float(drop)
            cnt = window
        if cnt == window:
            out[i] = acc / window
    return out


def ema(vals: Sequence[Num], span: int) -> list[Num]:
    out: list[Num] = [None] * len(vals)
    if span <= 0:
        return out
    k = 2.0 / (span + 1.0)
    prev: float | None = None
    for i, v in enumerate(vals):
        if v is None:
            out[i] = prev
            continue
        v = float(v)
        prev = v if prev is None else v * k + prev * (1 - k)
        out[i] = prev
    return out


def rolling_max(vals: Sequence[Num], window: int) -> list[Num]:
    out: list[Num] = [None] * len(vals)
    for i in range(len(vals)):
        start = max(0, i - window + 1)
        seg = [v for v in vals[start:i + 1] if v is not None]
        out[i] = max(seg) if len(seg) == i - start + 1 and seg else None
    return out


def rolling_min(vals: Sequence[Num], window: int) -> list[Num]:
    out: list[Num] = [None] * len(vals)
    for i in range(len(vals)):
        start = max(0, i - window + 1)
        seg = [v for v in vals[start:i + 1] if v is not None]
        out[i] = min(seg) if len(seg) == i - start + 1 and seg else None
    return out


def rolling_max_fast(vals: Sequence[Num], window: int) -> list[Num]:
    """单调队列滚动最大，O(n)。"""
    from collections import deque

    out: list[Num] = [None] * len(vals)
    dq: deque[int] = deque()
    for i, v in enumerate(vals):
        if v is None:
            continue
        while dq and float(vals[dq[-1]]) <= float(v):
            dq.pop()
        dq.append(i)
        while dq and dq[0] <= i - window:
            dq.popleft()
        if i + 1 >= window and dq:
            out[i] = float(vals[dq[0]])
    return out


def rolling_min_fast(vals: Sequence[Num], window: int) -> list[Num]:
    from collections import deque

    out: list[Num] = [None] * len(vals)
    dq: deque[int] = deque()
    for i, v in enumerate(vals):
        if v is None:
            continue
        while dq and float(vals[dq[-1]]) >= float(v):
            dq.pop()
        dq.append(i)
        while dq and dq[0] <= i - window:
            dq.popleft()
        if i + 1 >= window and dq:
            out[i] = float(vals[dq[0]])
    return out


def pct_change(vals: Sequence[Num], periods: int = 1) -> list[Num]:
    out: list[Num] = [None] * len(vals)
    for i in range(len(vals)):
        j = i - periods
        if j >= 0 and vals[i] is not None and vals[j]:
            out[i] = float(vals[i]) / float(vals[j]) - 1.0
    return out


def slope(vals: Sequence[Num], window: int) -> list[Num]:
    """最小二乘斜率（对 window 根做归一化，单位：%/bar）。"""
    out: list[Num] = [None] * len(vals)
    for i in range(len(vals)):
        if i + 1 < window:
            continue
        seg = vals[i - window + 1:i + 1]
        if any(v is None for v in seg):
            continue
        ys = [float(v) for v in seg]
        n = len(ys)
        xs = list(range(n))
        mx = sum(xs) / n
        my = sum(ys) / n
        den = sum((x - mx) ** 2 for x in xs)
        if den == 0 or my == 0:
            out[i] = 0.0
            continue
        num = sum((xs[k] - mx) * (ys[k] - my) for k in range(n))
        out[i] = (num / den) / my * 100.0
    return out


# --------------------------------------------------------------------------
# 经典指标
# --------------------------------------------------------------------------
def macd(close: Sequence[Num], fast: int = 12, slow: int = 26,
         signal: int = 9) -> dict[str, list[Num]]:
    ef = ema(close, fast)
    es = ema(close, slow)
    dif: list[Num] = [
        (ef[i] - es[i]) if (ef[i] is not None and es[i] is not None) else None
        for i in range(len(close))
    ]
    dea = ema(dif, signal)
    hist: list[Num] = [
        ((dif[i] - dea[i]) * 2) if (dif[i] is not None and dea[i] is not None) else None
        for i in range(len(close))
    ]
    return {"dif": dif, "dea": dea, "hist": hist}


def rsi(close: Sequence[Num], window: int = 14) -> list[Num]:
    """Wilder RSI。"""
    n = len(close)
    out: list[Num] = [None] * n
    if n <= window:
        return out
    gains, losses = 0.0, 0.0
    for i in range(1, window + 1):
        d = float(close[i]) - float(close[i - 1])
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    ag, al = gains / window, losses / window
    out[window] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(window + 1, n):
        d = float(close[i]) - float(close[i - 1])
        ag = (ag * (window - 1) + max(d, 0.0)) / window
        al = (al * (window - 1) + max(-d, 0.0)) / window
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def atr(high: Sequence[Num], low: Sequence[Num], close: Sequence[Num],
        window: int = 14) -> list[Num]:
    """Wilder ATR。"""
    n = len(close)
    out: list[Num] = [None] * n
    if n < 2:
        return out
    trs: list[float] = [0.0] * n
    for i in range(n):
        h = high[i] if high[i] is not None else close[i]
        l = low[i] if low[i] is not None else close[i]
        pc = close[i - 1] if i > 0 and close[i - 1] is not None else close[i]
        if h is None or l is None or pc is None:
            trs[i] = 0.0
            continue
        trs[i] = max(float(h) - float(l), abs(float(h) - float(pc)), abs(float(l) - float(pc)))
    if n <= window:
        return out
    first = sum(trs[1:window + 1]) / window
    out[window] = first
    prev = first
    for i in range(window + 1, n):
        prev = (prev * (window - 1) + trs[i]) / window
        out[i] = prev
    return out


def williams_r(high: Sequence[Num], low: Sequence[Num], close: Sequence[Num],
               window: int = 14) -> list[Num]:
    n = len(close)
    out: list[Num] = [None] * n
    for i in range(n):
        if i + 1 < window:
            continue
        hh, ll = None, None
        ok = True
        for j in range(i - window + 1, i + 1):
            if high[j] is None or low[j] is None:
                ok = False
                break
            hh = high[j] if hh is None else max(hh, high[j])
            ll = low[j] if ll is None else min(ll, low[j])
        if not ok or hh is None or ll is None or close[i] is None:
            continue
        rng = float(hh) - float(ll)
        out[i] = -50.0 if rng == 0 else (float(hh) - float(close[i])) / rng * -100.0
    return out


def bias(close: Sequence[Num], window: int = 20) -> list[Num]:
    ma = sma(close, window)
    return [
        (float(close[i]) / ma[i] - 1.0) * 100.0
        if (close[i] is not None and ma[i]) else None
        for i in range(len(close))
    ]


# --------------------------------------------------------------------------
# 回撤 / 相对位置
# --------------------------------------------------------------------------
def drawdown_series(close: Sequence[Num]) -> list[Num]:
    """当前价相对历史运行最高收盘的回撤（负值）。"""
    out: list[Num] = [None] * len(close)
    peak: float | None = None
    for i, c in enumerate(close):
        if c is None:
            out[i] = None
            continue
        c = float(c)
        peak = c if peak is None else max(peak, c)
        out[i] = (c / peak - 1.0) * 100.0
    return out


def window_drawdown(close: Sequence[Num], window: int) -> dict:
    """指定窗口（交易日）内的最高/最低/最大回撤/当前回撤。"""
    seg = [v for v in close[-window:] if v is not None] if window > 0 else []
    if len(seg) < 2:
        return {"available": False}
    hi, lo = max(seg), min(seg)
    peak = seg[0]
    mdd = 0.0
    for v in seg:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1.0)
    return {
        "available": True,
        "highest": hi,
        "lowest": lo,
        "max_drawdown_pct": mdd * 100.0,
        "current_drawdown_pct": (seg[-1] / hi - 1.0) * 100.0,
        "bars": len(seg),
    }


def relative_position(close: Sequence[Num], window: int) -> Num:
    """(当前价 - 窗口最低) / (窗口最高 - 窗口最低) × 100，0~100。"""
    seg = [v for v in close[-window:] if v is not None]
    if len(seg) < 20:
        return None
    hi, lo = max(seg), min(seg)
    if hi - lo <= 0:
        return 50.0
    return (seg[-1] - lo) / (hi - lo) * 100.0


def max_drawdown_of(values: Sequence[Num]) -> float:
    peak = None
    mdd = 0.0
    for v in values:
        if v is None:
            continue
        v = float(v)
        peak = v if peak is None else max(peak, v)
        if peak:
            mdd = min(mdd, v / peak - 1.0)
    return mdd * 100.0


def _vol_series(close: Sequence[Num], window: int = 20) -> list[Num]:
    """逐 bar 年化波动率（%）。"""
    n = len(close)
    out: list[Num] = [None] * n
    for i in range(window + 1, n):
        rets = []
        ok = True
        for j in range(i - window + 1, i + 1):
            if close[j] is None or close[j - 1] is None or not close[j - 1]:
                ok = False
                break
            rets.append(float(close[j]) / float(close[j - 1]) - 1.0)
        if not ok or len(rets) < 5:
            continue
        mu = sum(rets) / len(rets)
        var = sum((x - mu) ** 2 for x in rets) / max(1, len(rets) - 1)
        out[i] = math.sqrt(var) * math.sqrt(252) * 100.0
    return out


# --------------------------------------------------------------------------
# 波段顶底（因果 ZigZag + 状态机）
# --------------------------------------------------------------------------
def zigzag_pivots(high: Sequence[Num], low: Sequence[Num], close: Sequence[Num],
                  threshold: Sequence[Num]) -> tuple[list[dict], dict]:
    """因果波段拐点识别。

    返回 (pivots, live)：
      pivots: 已确认拐点 [{'type':'H'/'L','idx':int,'price':float,'confirm_idx':int}]
      live  : 当前尚未确认的极值 {'type','idx','price'}
    每个 pivot 只有在其后价格反向运行超过 threshold 时才被确认，
    confirm_idx 是确认 bar —— 回测时信号在 confirm_idx 才可用。
    """
    n = len(close)
    pivots: list[dict] = []
    if n < 3:
        return pivots, {"type": None, "idx": None, "price": None}

    def thr(i: int) -> float:
        t = threshold[i] if i < len(threshold) else None
        if t is None:
            for j in range(i - 1, -1, -1):
                if threshold[j] is not None:
                    return float(threshold[j])
            return 0.0
        return float(t)

    direction = 1 if (close[1] or 0) >= (close[0] or 0) else -1
    ext_idx = 0
    ext_price = float(high[0] if direction == 1 else low[0]) if high[0] is not None else float(close[0])

    for i in range(1, n):
        h, l = high[i], low[i]
        if h is None or l is None:
            continue
        t = thr(i)
        if direction == 1:
            if h > ext_price:
                ext_price, ext_idx = float(h), i
            elif ext_price - float(l) > t:
                pivots.append({"type": "H", "idx": ext_idx, "price": ext_price,
                               "confirm_idx": i})
                # 从 pivot bar 到确认 bar 之间寻找真正的最低点
                lo_idx, lo_val = i, float(l)
                for j in range(ext_idx, i + 1):
                    if low[j] is not None and float(low[j]) < lo_val:
                        lo_val, lo_idx = float(low[j]), j
                direction = -1
                ext_price, ext_idx = lo_val, lo_idx
        else:
            if l < ext_price:
                ext_price, ext_idx = float(l), i
            elif float(h) - ext_price > t:
                pivots.append({"type": "L", "idx": ext_idx, "price": ext_price,
                               "confirm_idx": i})
                hi_idx, hi_val = i, float(h)
                for j in range(ext_idx, i + 1):
                    if high[j] is not None and float(high[j]) > hi_val:
                        hi_val, hi_idx = float(high[j]), j
                direction = 1
                ext_price, ext_idx = hi_val, hi_idx

    live = {"type": "H" if direction == 1 else "L", "idx": ext_idx, "price": ext_price}
    return pivots, live


SWING_STATES = ["疑似底部", "底部观察", "底部确认", "波段中部",
                "顶部观察", "疑似顶部", "顶部确认"]


def swing_state(high: Sequence[Num], low: Sequence[Num], close: Sequence[Num],
                atr_series: Sequence[Num], thr_mult: float, confirm_mult: float,
                near_low_pct: float = 3.0, near_high_pct: float = 3.0) -> dict:
    """输出当前波段状态（因果）。"""
    n = len(close)
    if n < 30 or close[-1] is None:
        return {"state": "暂无数据", "detail": "数据不足", "pivots": [], "live": {}}

    thr = [
        (atr_series[i] * thr_mult) if atr_series[i] is not None else None
        for i in range(n)
    ]
    conf_thr = [
        (atr_series[i] * confirm_mult) if atr_series[i] is not None else None
        for i in range(n)
    ]
    pivots, live = zigzag_pivots(high, low, close, thr)
    c = float(close[-1])

    last_low = next((p for p in reversed(pivots) if p["type"] == "L"), None)
    last_high = next((p for p in reversed(pivots) if p["type"] == "H"), None)

    run_min = min([v for v in low[-30:] if v is not None] or [c])
    run_max = max([v for v in high[-30:] if v is not None] or [c])
    atr_now = next((v for v in reversed(atr_series) if v is not None), None) or (c * 0.02)
    confirm_move = float(atr_now) * confirm_mult

    state, detail = "波段中部", ""

    rise_from_low = None
    if last_low:
        rise_from_low = c - float(last_low["price"])
    fall_from_high = None
    if last_high:
        fall_from_high = float(last_high["price"]) - c

    new_low_made = last_low is not None and float(low[-1]) < float(last_low["price"])

    if last_low and rise_from_low is not None:
        if rise_from_low >= confirm_move and not new_low_made:
            state = "底部确认"
            detail = f"较波段低点 {last_low['price']:.3f} 已回升 {rise_from_low:.3f}（>{confirm_move:.3f}）"
        elif rise_from_low > 0 and rise_from_low >= confirm_move * 0.3:
            state = "底部观察"
            detail = f"低点 {last_low['price']:.3f} 后回升 {rise_from_low:.3f}，尚未确认"
        elif new_low_made:
            state = "波段中部"
            detail = "跌破前一波段低点，下降延续"

    if state == "波段中部" and last_high and fall_from_high is not None:
        if fall_from_high < 0:
            # 突破前高，趋势延续
            if c >= float(last_high["price"]) * (1 + near_high_pct / 100.0):
                state = "疑似顶部"
                detail = f"突破前高 {last_high['price']:.3f} 后继续上行，注意追高风险"
        else:
            if fall_from_high >= confirm_move:
                state = "顶部确认"
                detail = f"较波段高点 {last_high['price']:.3f} 已回落 {fall_from_high:.3f}（>{confirm_move:.3f}）"
            elif fall_from_high >= confirm_move * 0.3:
                state = "顶部观察"
                detail = f"高点 {last_high['price']:.3f} 后回落 {fall_from_high:.3f}，尚未确认"
            elif abs(c - float(last_high["price"])) / float(last_high["price"]) * 100 <= near_high_pct:
                state = "疑似顶部"
                detail = f"贴近波段高点 {last_high['price']:.3f}（{near_high_pct}% 以内）"

    if state == "波段中部":
        near_run_low = abs(c - run_min) / c * 100 <= near_low_pct
        if near_run_low and not new_low_made:
            state = "疑似底部"
            detail = f"贴近近30日低点 {run_min:.3f}，尚未止跌确认"

    # 最近一次确认拐点
    recent = pivots[-1] if pivots else None
    return {
        "state": state,
        "detail": detail,
        "pivots": pivots,
        "last_confirmed": recent,
        "last_low": last_low,
        "last_high": last_high,
        "live": live,
        "run_min_30": run_min,
        "run_max_30": run_max,
        "confirm_move": confirm_move,
        "rise_from_low": rise_from_low,
        "fall_from_high": fall_from_high,
        "new_low_made": new_low_made,
    }


def _classify_swing(c: float, last_low: dict | None, last_high: dict | None,
                    run_min: float, run_max: float, confirm_move: float,
                    near_low_pct: float, near_high_pct: float,
                    new_low_made: bool) -> tuple[str, str]:
    """把"当前 bar 的波段状态"判定抽出来，供实时与历史序列共用。"""
    state, detail = "波段中部", ""
    rise = (c - float(last_low["price"])) if last_low else None

    if last_low and rise is not None:
        if rise >= confirm_move and not new_low_made:
            state = "底部确认"
            detail = f"较波段低点 {last_low['price']:.3f} 回升 {rise:.3f}（>{confirm_move:.3f}）"
        elif rise > 0 and rise >= confirm_move * 0.3:
            state = "底部观察"
            detail = f"低点 {last_low['price']:.3f} 后回升 {rise:.3f}，尚未确认"
        elif new_low_made:
            state = "波段中部"
            detail = "跌破前一波段低点，下降延续"

    if state == "波段中部" and last_high:
        fall = float(last_high["price"]) - c
        if fall < 0:
            if c >= float(last_high["price"]) * (1 + near_high_pct / 100.0):
                state = "疑似顶部"
                detail = f"突破前高 {last_high['price']:.3f} 后继续上行"
        else:
            if fall >= confirm_move:
                state = "顶部确认"
                detail = f"较波段高点 {last_high['price']:.3f} 回落 {fall:.3f}（>{confirm_move:.3f}）"
            elif fall >= confirm_move * 0.3:
                state = "顶部观察"
                detail = f"高点 {last_high['price']:.3f} 后回落 {fall:.3f}，尚未确认"
            elif abs(c - float(last_high["price"])) / max(1e-9, float(last_high["price"])) * 100 <= near_high_pct:
                state = "疑似顶部"
                detail = f"贴近波段高点 {last_high['price']:.3f}"

    if state == "波段中部":
        if abs(c - run_min) / max(1e-9, c) * 100 <= near_low_pct and not new_low_made:
            state = "疑似底部"
            detail = f"贴近近30日低点 {run_min:.3f}，尚未止跌确认"
    return state, detail


def swing_state_series(high: Sequence[Num], low: Sequence[Num], close: Sequence[Num],
                       atr_series: Sequence[Num], thr_mult: float, confirm_mult: float,
                       near_low_pct: float = 3.0,
                       near_high_pct: float = 3.0) -> list[str]:
    """逐 bar 的因果波段状态序列（用于历史相似状态匹配）。"""
    n = len(close)
    out = ["暂无数据"] * n
    if n < 30:
        return out
    thr = [(atr_series[i] * thr_mult) if atr_series[i] is not None else None
           for i in range(n)]
    pivots, _ = zigzag_pivots(high, low, close, thr)

    ptr = 0
    last_low: dict | None = None
    last_high: dict | None = None
    atr_fallback = 0.02

    for i in range(n):
        while ptr < len(pivots) and pivots[ptr]["confirm_idx"] <= i:
            p = pivots[ptr]
            if p["type"] == "L":
                last_low = p
            else:
                last_high = p
            ptr += 1
        if close[i] is None:
            continue
        c = float(close[i])
        seg_l = [v for v in low[max(0, i - 29):i + 1] if v is not None]
        seg_h = [v for v in high[max(0, i - 29):i + 1] if v is not None]
        run_min = min(seg_l) if seg_l else c
        run_max = max(seg_h) if seg_h else c
        atr_i = atr_series[i]
        atr_now = float(atr_i) if atr_i is not None else c * atr_fallback
        confirm_move = atr_now * confirm_mult
        new_low = bool(last_low and low[i] is not None
                       and float(low[i]) < float(last_low["price"]))
        state, _ = _classify_swing(c, last_low, last_high, run_min, run_max,
                                   confirm_move, near_low_pct, near_high_pct, new_low)
        out[i] = state
    return out


def wr_reversal_series(wr: Sequence[Num], close: Sequence[Num],
                       bias20: Sequence[Num], lookback: int = 20,
                       relpos1y: Sequence[Num] | None = None) -> list[dict]:
    """逐 bar 的 WR 超卖反转（严格因果）。

    "WR 曾 < -80 后重新上穿 -80" 只是必要条件之一；
    还必须叠加：价格不再创新低 + 20日跌幅改善 + BIAS20 回升，
    并且**当前位置必须处于中低位**（relpos1y < 55），
    否则强势上涨中的一次小回调会被误判成"超卖反转"。
    """
    n = len(close)
    out: list[dict] = [{"available": False}] * n
    for i in range(n):
        if i < lookback + 5:
            continue
        seg = [v for v in wr[i - lookback + 1:i + 1] if v is not None]
        now = wr[i]
        if now is None or not seg:
            continue
        was = any(v < -80 for v in seg[:-2])
        crossed = bool(was and now > -80)
        prev_vals = [float(v) for v in close[max(0, i - lookback):i] if v is not None]
        no_new_low = bool(prev_vals and float(close[i]) > min(prev_vals))
        r_now = (float(close[i]) / float(close[i - 20]) - 1) if (i >= 20 and close[i - 20]) else None
        r_prev = (float(close[i - 5]) / float(close[i - 25]) - 1) if (i >= 25 and close[i - 25]) else None
        improving = bool(r_now is not None and r_prev is not None and r_now > r_prev)
        b_now, b_prev = bias20[i], (bias20[i - 5] if i >= 5 else None)
        bias_rising = bool(b_now is not None and b_prev is not None and b_now > b_prev)
        low_pos = True
        if relpos1y is not None:
            rp = relpos1y[i]
            low_pos = bool(rp is None or rp < 55)
        score = sum([crossed, no_new_low, improving, bias_rising])
        out[i] = {
            "available": True, "was_oversold": was, "crossed_back": crossed,
            "no_new_low": no_new_low, "drop_improving": improving,
            "bias_rising": bias_rising, "low_position": low_pos, "score": score,
            "confirmed_reversal": bool(crossed and no_new_low and low_pos and score >= 3),
        }
    return out


def wr_oversold_reversal(wr: Sequence[Num], close: Sequence[Num],
                         bias20: Sequence[Num], lookback: int = 20,
                         relpos1y: Num = None) -> dict:
    """WR 超卖反转识别（不是"WR<-80 就买"）。"""
    n = len(close)
    if n < lookback + 5:
        return {"available": False}
    seg = [v for v in wr[-lookback:] if v is not None]
    now = wr[-1]
    if now is None or not seg:
        return {"available": False}
    was = any(v < -80 for v in seg[:-2])
    crossed = bool(was and now > -80)
    no_new_low = bool(float(close[-1]) > min(
        [float(v) for v in close[-lookback:-1] if v is not None] or [float(close[-1])]))
    ret20_now = float(close[-1]) / float(close[-21]) - 1 if n > 21 else None
    ret20_prev = float(close[-6]) / float(close[-26]) - 1 if n > 26 else None
    improving = (ret20_now is not None and ret20_prev is not None
                 and ret20_now > ret20_prev)
    b_now = bias20[-1]
    b_prev = bias20[-6] if n > 6 else None
    bias_rising = (b_now is not None and b_prev is not None and b_now > b_prev)
    low_pos = bool(relpos1y is None or relpos1y < 55)
    score = sum([crossed, no_new_low, improving, bias_rising])
    confirmed = bool(crossed and no_new_low and low_pos and score >= 3)
    return {
        "available": True,
        "was_oversold": was,
        "crossed_back": crossed,
        "no_new_low": no_new_low,
        "drop_improving": improving,
        "bias_rising": bias_rising,
        "low_position": low_pos,
        "score": score,
        "confirmed_reversal": confirmed,
    }


# --------------------------------------------------------------------------
# 全套指标
# --------------------------------------------------------------------------
def compute_all(dates: Sequence[str], open_: Sequence[Num], high: Sequence[Num],
                low: Sequence[Num], close: Sequence[Num],
                cfg, asset_class: str = "stock") -> dict:
    """按配置计算一只 ETF 的全部技术指标。"""
    ma_windows = cfg.get("indicators.ma_windows", [5, 10, 20, 30, 50, 60, 120, 200])
    macd_cfg = cfg.get("indicators.macd", {"fast": 12, "slow": 26, "signal": 9})
    rsi_w = int(cfg.get("indicators.rsi_window", 14))
    atr_w = int(cfg.get("indicators.atr_window", 14))
    wr_w = int(cfg.get("indicators.wr_window", 14))
    bias_w = int(cfg.get("indicators.bias_window", 20))
    ret_w = int(cfg.get("indicators.return_window", 20))
    dd_windows = cfg.get("indicators.drawdown_windows",
                         {"1y": 252, "2y": 504, "5y": 1260})
    rp_windows = cfg.get("indicators.relpos_windows", {"1y": 252, "2y": 504})
    hl_windows = cfg.get("indicators.high_low_windows",
                         {"60d": 60, "120d": 120, "52w": 252})

    out: dict = {
        "dates": list(dates), "open": list(open_), "high": list(high),
        "low": list(low), "close": list(close), "n": len(close),
    }
    mas = {f"ma{w}": sma(close, w) for w in ma_windows}
    out["ma"] = mas
    out["macd"] = macd(close, macd_cfg.get("fast", 12), macd_cfg.get("slow", 26),
                       macd_cfg.get("signal", 9))
    out["rsi14"] = rsi(close, rsi_w)
    out["atr14"] = atr(high, low, close, atr_w)
    out["wr14"] = williams_r(high, low, close, wr_w)
    out["bias20"] = bias(close, bias_w)
    out["ret20"] = [
        (v * 100.0) if v is not None else None for v in pct_change(close, ret_w)
    ]
    out["atr_pct"] = [
        (out["atr14"][i] / close[i] * 100.0)
        if (out["atr14"][i] is not None and close[i]) else None
        for i in range(len(close))
    ]
    out["drawdown"] = drawdown_series(close)

    dd = {}
    for label, w in dd_windows.items():
        dd[label] = window_drawdown(close, int(w))
    out["dd_windows"] = dd

    rp = {}
    for label, w in rp_windows.items():
        rp[label] = relative_position(close, int(w))
    out["relpos"] = rp

    hl = {}
    for label, w in hl_windows.items():
        w = int(w)
        seg = [v for v in close[-w:] if v is not None]
        hl[label] = {"high": max(seg) if seg else None,
                     "low": min(seg) if seg else None}
    out["high_low"] = hl

    # 均线方向
    out["ma_slope"] = {f"ma{w}": slope(mas[f"ma{w}"], 20) for w in ma_windows}

    # ---- 逐 bar 序列（严格因果，供历史相似状态匹配与反转判定使用）----
    rp_series: dict[str, list[Num]] = {}
    dd_series: dict[str, list[Num]] = {}
    for label, w in rp_windows.items():
        w = int(w)
        mx = rolling_max_fast(close, w)
        mn = rolling_min_fast(close, w)
        rp_series[label] = [
            ((float(close[i]) - mn[i]) / (mx[i] - mn[i]) * 100.0)
            if (close[i] is not None and mx[i] is not None and mn[i] is not None
                and mx[i] - mn[i] > 0) else None
            for i in range(len(close))
        ]
        dd_series[label] = [
            (float(close[i]) / mx[i] - 1.0) * 100.0
            if (close[i] is not None and mx[i]) else None
            for i in range(len(close))
        ]
    out["relpos_series"] = rp_series
    out["curdd_series"] = dd_series
    rp1y = rp_series.get("1y")

    # 波段
    swing_cfg = cfg.get("swing", {})
    thr_mult = float((swing_cfg.get("atr_multiplier") or {}).get(asset_class, 2.0))
    conf_mult = float((swing_cfg.get("confirm_atr_multiplier") or {}).get(asset_class, 3.0))
    out["swing"] = swing_state(
        high, low, close, out["atr14"], thr_mult, conf_mult,
        float(swing_cfg.get("near_low_pct", 3.0)),
        float(swing_cfg.get("near_high_pct", 3.0)),
    )
    out["wr_reversal"] = wr_oversold_reversal(
        out["wr14"], close, out["bias20"], int(cfg.get("indicators.wr_window", 14)) + 6,
        rp1y[-1] if rp1y else None)

    out["swing_states"] = swing_state_series(
        high, low, close, out["atr14"], thr_mult, conf_mult,
        float(swing_cfg.get("near_low_pct", 3.0)),
        float(swing_cfg.get("near_high_pct", 3.0)),
    )
    out["wr_reversal_series"] = wr_reversal_series(
        out["wr14"], close, out["bias20"],
        int(cfg.get("indicators.wr_window", 14)) + 6, rp1y
    )
    out["vol20_series"] = _vol_series(close, 20)

    # 20 日年化波动率
    n = len(close)
    if n > 21:
        rets = [float(close[i]) / float(close[i - 1]) - 1 for i in range(n - 20, n)]
        mu = sum(rets) / len(rets)
        var = sum((x - mu) ** 2 for x in rets) / max(1, len(rets) - 1)
        out["vol20_annual_pct"] = math.sqrt(var) * math.sqrt(252) * 100
        out["ret20_std_pct"] = math.sqrt(var) * 100
    else:
        out["vol20_annual_pct"] = None
        out["ret20_std_pct"] = None
    return out
