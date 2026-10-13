"""盘中实时报价（腾讯 qt.gtimg.cn）——**仅供界面参考显示**。

用途边界（硬约束，任何改动都不得突破）：
    · 只用于在界面上多显示一列「参考现价」，让人知道此刻大概什么价；
    · **绝不参与**权重、偏离、超配、再平衡、BUY 权限、信号等任何判断。
      所有判断一律沿用**日线收盘价**（core/signals.etf_snapshot 的 price），
      以保持与策略（20/60/120 日日线口径）一致。
    · 失败即返回空 dict，绝不抛出、绝不影响主流程（短超时 + 不重试）。

腾讯行情串（gbk，`v_sz159937="1~名称~代码~当前价~昨收~今开~...~时间~涨跌额~涨跌幅~..."`）
用到的下标：
    1 = 名称, 2 = 代码, 3 = 当前价, 4 = 昨收, 5 = 今开,
    30 = 行情时间(YYYYMMDDHHMMSS), 31 = 涨跌额, 32 = 涨跌幅(%)
"""
from __future__ import annotations

from .http_util import http_get_text

QUOTE_URL = "https://qt.gtimg.cn/q="
_TIMEOUT = 3
_RETRIES = 1


def _sym(thscode: str) -> str:
    """`159937.SZ` / `511220.SH` → `sz159937` / `sh511220`。"""
    s = str(thscode or "").strip()
    if not s:
        return ""
    if "." in s:
        code, ex = s.split(".", 1)
        return ("sh" if ex.strip().upper() == "SH" else "sz") + code.strip()
    return s


def _f(v) -> float | None:
    try:
        if v is None or str(v).strip() == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def fmt_quote_time(raw: str) -> str | None:
    """`20260930161446` → `2026-09-30 16:14`；不合法返回 None。"""
    s = str(raw or "").strip()
    if len(s) < 12 or not s[:12].isdigit():
        return None
    return f"{s[0:4]}-{s[4:6]}-{s[6:8]} {s[8:10]}:{s[10:12]}"


def parse_quotes(text: str) -> dict:
    """解析腾讯行情串 → {代码: {name, price, prev_close, change_pct, time, time_hhmm}}。

    纯函数（不联网），便于自检离线喂样例。解析不出的行直接跳过。
    """
    out: dict = {}
    for line in str(text or "").splitlines():
        if "=" not in line or "~" not in line:
            continue
        body = line.split('="', 1)[-1].rstrip('";').rstrip()
        f = body.split("~")
        if len(f) < 6:
            continue
        code = str(f[2]).strip()
        if not code:
            continue
        out[code] = {
            "name": f[1] or "",
            "code": code,
            "price": _f(f[3]),
            "prev_close": _f(f[4]),
            "open": _f(f[5]),
            "change": _f(f[31]) if len(f) > 31 else None,
            "change_pct": _f(f[32]) if len(f) > 32 else None,
            "time": str(f[30]).strip() if len(f) > 30 else "",
            "time_hhmm": fmt_quote_time(f[30]) if len(f) > 30 else None,
        }
    return out


def fetch_quotes(thscodes, timeout: int = _TIMEOUT, retries: int = _RETRIES) -> dict:
    """批量取盘中行情 → {代码: {...}}；任何失败都返回 {}（绝不影响主流程）。"""
    syms = [s for s in (_sym(t) for t in (thscodes or [])) if s]
    if not syms:
        return {}
    try:
        text, err = http_get_text(
            QUOTE_URL + ",".join(syms),
            headers={"Referer": "https://gu.qq.com/"},
            timeout=timeout,
            retries=retries,
            encoding="gbk",
        )
    except Exception:  # noqa: BLE001
        return {}
    if not text:
        return {}
    try:
        return parse_quotes(text)
    except Exception:  # noqa: BLE001
        return {}


def latest_time(quotes: dict) -> str | None:
    """取所有报价里最晚的行情时间（用于界面标注「行情时间」）。"""
    times = [q.get("time") for q in (quotes or {}).values() if q.get("time")]
    if not times:
        return None
    return fmt_quote_time(max(times))


__all__ = ["fetch_quotes", "parse_quotes", "fmt_quote_time", "latest_time"]
