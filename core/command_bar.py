"""指令栏（沟通渠道）：把用户输入的一句「人话」解析成可执行动作并落地。

设计原则
--------
1. **只记录、不代下单**：本模块只改本地 ``config/config.json`` 与本地台账，
   绝不联网、绝不自动下单。用户是在「告知已成交」，不是让程序去交易。
2. **保守解析**：标的或数字不能确定时，返回 ERROR + 可用示例，绝不乱猜。
3. **可核对**：每次执行前都能给出一句中文预览（将把什么改成什么），供界面确认。
4. **冻结态安全**：台账写成走 ``execution_ledger``（其路径基于 project_root()），
   不会在 ``_internal`` 里落文件。

支持的指令（宽容解析：中英文标点、全角数字、万元、千分位都可）
--------------------------------------------------------------
卖出    卖黄金800@8.62 / 减仓 纳指 500份 价1.55 / 卖159937 800
买入    买纳指500@1.55 / 加仓 城投债 1000
现金    现金20万 / 现金 200000
总资产  总资产30万
上限    上限黄金6% / 黄金上限 6
目标    目标 债50 股45 金5
忽略    忽略 / 搁置 / 这条先不做 / 跳过
备注    备注 今天没空操作
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import execution_ledger as led

# 全角 -> 半角（数字、小数点、百分号、@、空格、逗号）
_FULL = str.maketrans("０１２３４５６７８９．％＠　，", "0123456789.%@ ,")

_SKIP_WORDS = ("忽略", "搁置", "先不做", "不做", "跳过", "以后再说", "取消", "暂不")
_LBL = {"bond": "债券", "stock": "股票", "gold": "黄金"}
_HELP = ("可用指令：卖黄金800@8.62 / 买纳指500 / 现金20万 / 总资产30万 / "
         "上限黄金6% / 目标 债50 股45 金5 / 忽略 / 备注 xxx")

ACTIONS = ("REBALANCE", "BUY", "SET_CASH", "SET_TOTAL", "SET_GOLD_CAP",
           "SET_TARGET", "SKIP", "NOTE", "ERROR", "EMPTY")


@dataclass
class Command:
    """一条解析后的指令。``action`` 见 ACTIONS。"""

    action: str
    text: str = ""
    asset_id: str | None = None
    name: str | None = None
    shares: int | None = None
    price: float | None = None
    amount: float | None = None
    key: str | None = None
    value: float | None = None
    targets: dict = field(default_factory=dict)
    note: str = ""
    error: str | None = None


# --------------------------------------------------------------------------
# 基础解析
# --------------------------------------------------------------------------
def _norm(s: str) -> str:
    return (s or "").translate(_FULL)


def _num(tok: str) -> float | None:
    """从文本里取第一个数值，支持「万/千」单位。"""
    m = re.search(r"(\d+(?:\.\d+)?)\s*(万|千)?", tok)
    if not m:
        return None
    v = float(m.group(1))
    if m.group(2) == "万":
        v *= 10000
    elif m.group(2) == "千":
        v *= 1000
    return v


def _assets(cfg) -> list[dict]:
    try:
        return list(cfg.assets or [])
    except Exception:  # noqa: BLE001
        return []


def _aliases(a: dict) -> list[str]:
    """一个标的的所有可识别写法：全名、简称（ETF 前半段）、id、代码。"""
    out = []
    for k in (a.get("name"), a.get("id"), a.get("ticker"), a.get("thscode")):
        if k:
            out.append(str(k).strip())
    nm = str(a.get("name") or "")
    for tag in ("ETF", "etf", "Etf"):
        if tag in nm:
            out.append(nm.split(tag)[0].strip())
            break
    return [x for x in out if x]


def _lcs_len(a: str, b: str) -> int:
    """两串最长公共子串长度（串很短，简单 DP 足够）。"""
    best = 0
    prev = [0] * (len(b) + 1)
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        for j in range(1, len(b) + 1):
            if a[i - 1] == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


def _has_cjk(s: str) -> bool:
    return any("\u4e00" <= c <= "\u9fa5" for c in s)


def _match_asset(s: str, assets: list[dict]):
    """返回 (asset, 命中关键词)；歧义时返回 (None, 类别词)；不认识返回 None。

    三级匹配：① 别名整体出现（含 id/代码）；② 中文块与别名的最长公共子串 ≥2
    （处理「纳指」→「纳指ETF广发」、「国债」→「30年国债ETF鹏扬」这类简称）；
    ③ 类别词（黄金/股/债），仅当该类唯一时才采用，否则报歧义。
    """
    best = None
    for a in assets:
        for k in _aliases(a):
            if k and k in s and (best is None or len(k) > len(best[1])):
                best = (a, k)
    if best is not None:
        return best

    chunks = re.findall(r"[\u4e00-\u9fa5]{2,}", s)
    hits: dict = {}
    for ch in chunks:
        for a in assets:
            score = 0
            for k in _aliases(a):
                if _has_cjk(k):
                    score = max(score, _lcs_len(ch, k))
            if score >= 2:
                cur = hits.get(str(a.get("id")))
                if cur is None or score > cur[1]:
                    hits[str(a.get("id"))] = (a, score)
    if len(hits) == 1:
        return next(iter(hits.values()))[0], (chunks[0] if chunks else "")
    if len(hits) > 1:
        return None, (chunks[0] if chunks else "")

    for word, cls in (("黄金", "gold"), ("金", "gold"), ("股", "stock"), ("债", "bond")):
        if word in s:
            cands = [a for a in assets if str(a.get("asset_class")) == cls]
            if len(cands) == 1:
                return (cands[0], word)
            if len(cands) > 1:
                return (None, word)
    return None


def parse(text: str, cfg) -> Command:
    """把一句话解析成 Command。绝不抛异常。"""
    raw = (text or "").strip()
    s = _norm(raw).replace(",", "").strip()
    if not s:
        return Command("EMPTY", text=raw, error="请输入一句话，例如：卖黄金800@8.62")

    # 备注
    m = re.search(r"^(?:备注|说明|记一笔)\s*[:：]?\s*(.+)$", s)
    if m:
        return Command("NOTE", text=raw, note=m.group(1).strip())

    # 忽略 / 搁置
    if any(w in s for w in _SKIP_WORDS):
        return Command("SKIP", text=raw, note=raw)

    # 上限：黄金上限6% / 上限 黄金 6
    if "上限" in s:
        v = re.search(r"上限\s*(?:黄金|金)?\s*(\d+(?:\.\d+)?)\s*%?", s)
        if v:
            return Command("SET_GOLD_CAP", text=raw, value=float(v.group(1)) / 100.0)
        return Command("ERROR", text=raw, error="上限要带数字，例如：上限黄金6%")

    # 目标配置
    if re.search(r"目标|配置", s):
        tg: dict = {}
        for word, key in (("黄金", "gold"), ("金", "gold"), ("债", "bond"), ("股", "stock")):
            mm = re.search(word + r"\s*(\d+(?:\.\d+)?)\s*%?", s)
            if mm:
                tg[key] = round(float(mm.group(1)) / 100.0, 4)
        if not tg:
            return Command("ERROR", text=raw, error="目标要带数字，例如：目标 债50 股45 金5")
        return Command("SET_TARGET", text=raw, targets=tg)

    # 现金 / 总资产（必须先于「买」判断，否则含数字会被误认）
    if "总资产" in s:
        v = _num(s)
        if v is None:
            return Command("ERROR", text=raw, error="总资产要带数字，例如：总资产30万")
        return Command("SET_TOTAL", text=raw, value=v)
    if "现金" in s:
        v = _num(s)
        if v is None:
            return Command("ERROR", text=raw, error="现金要带数字，例如：现金20万")
        return Command("SET_CASH", text=raw, value=v)

    # 买 / 卖
    side = None
    if re.search(r"卖|减仓|减|清", s):
        side = "REBALANCE"
    if side is None and re.search(r"买|加仓|加|补", s):
        side = "BUY"
    if side is None:
        return Command("ERROR", text=raw, error="没听懂。" + _HELP)

    # 价格（必须写 @，避免与份数混淆）
    m_price = re.search(r"@\s*(\d+(?:\.\d+)?)", s)
    price = float(m_price.group(1)) if m_price else None
    body = (s[:m_price.start()] + " " + s[m_price.end():]) if m_price else s

    hit = _match_asset(body, _assets(cfg))
    if hit is None:
        return Command("ERROR", text=raw,
                       error="没认出是哪个标的。请用名称或代码，例如：卖黄金800 / 买纳指500")
    asset, key = hit
    if asset is None:
        return Command("ERROR", text=raw,
                       error=f"「{key}」对应多个标的，请写具体名称，例如：卖城投债800")

    rest = body.replace(str(key), " ")
    m_sh = re.search(r"(\d+(?:\.\d+)?)\s*(?:份|股)?", rest)
    if not m_sh:
        return Command("ERROR", text=raw, error="要写份数，例如：卖黄金800@8.62")
    shares = int(float(m_sh.group(1)))
    if shares <= 0:
        return Command("ERROR", text=raw, error="份数必须大于 0")

    amt = round(shares * price, 2) if price else None
    return Command(side, text=raw, asset_id=str(asset.get("id")),
                   name=asset.get("name"), shares=shares, price=price, amount=amt)


# --------------------------------------------------------------------------
# 预览与执行
# --------------------------------------------------------------------------
def _find_asset(cfg, asset_id) -> dict | None:
    for a in _assets(cfg):
        if str(a.get("id")) == str(asset_id):
            return a
    return None


def preview(cmd: Command, cfg) -> str:
    """给确认框用的一句中文：将把什么改成什么。"""
    a = cmd.action
    if a == "EMPTY":
        return ""
    if a == "ERROR":
        return "没听懂：" + str(cmd.error)
    if a == "SKIP":
        return f"将记一条「暂不执行」台账（{cmd.note}），不改动持仓。"
    if a == "NOTE":
        return f"将记一条备注：「{cmd.note}」，不改动持仓。"
    if a == "SET_CASH":
        return f"将把现金登记为 {cmd.value:,.0f} 元（这会补全超配判定的分母）。"
    if a == "SET_TOTAL":
        return f"将把总资产登记为 {cmd.value:,.0f} 元（现金 = 总资产 − ETF 市值）。"
    if a == "SET_GOLD_CAP":
        now = float(cfg.get("portfolio.gold_max", 0.05) or 0.05) * 100
        return f"将把黄金上限从 {now:.1f}% 改为 {float(cmd.value) * 100:.1f}%。"
    if a == "SET_TARGET":
        now = cfg.get("portfolio.target_class", {}) or {}
        parts = [f"{_LBL[k]} {float(now.get(k, 0) or 0) * 100:.0f}%→{v * 100:.0f}%"
                 for k, v in cmd.targets.items()]
        return "将把目标配置改为：" + "、".join(parts) + "。"
    if a in ("REBALANCE", "BUY"):
        verb = "卖出" if a == "REBALANCE" else "买入"
        obj = _find_asset(cfg, cmd.asset_id) or {}
        have = int(float(obj.get("shares") or 0))
        after = have - int(cmd.shares) if a == "REBALANCE" else have + int(cmd.shares)
        if cmd.price:
            px = f"，成交价 {cmd.price}（约 {float(cmd.amount or 0):,.0f} 元）"
            cash = "现金按成交额增加" if a == "REBALANCE" else "现金按成交额减少"
        else:
            px = "，未给成交价"
            cash = "现金不变（可再说「现金 X」补上）"
        return (f"将登记：{verb} {cmd.name} {int(cmd.shares):,} 份"
                f"（份额 {have:,} → {after:,}）{px}；{cash}。")
    return ""


def _result_text(cmd: Command, entry: dict) -> str:
    if cmd.action == "REBALANCE":
        head = (f"已登记：卖出 {entry.get('name')} "
                f"{abs(int(entry.get('shares_delta') or 0)):,} 份，"
                f"份额 {entry.get('shares_before'):,} → {entry.get('shares_after'):,}")
    else:
        head = (f"已登记：买入 {entry.get('name')} "
                f"{int(entry.get('shares_delta') or 0):,} 份，"
                f"份额 {entry.get('shares_before'):,} → {entry.get('shares_after'):,}")
    if float(entry.get("amount") or 0) > 0:
        head += f"，现金 {entry.get('cash_before')} → {entry.get('cash_after')}"
    else:
        head += "（未给成交价，现金未登记）"
    return head + "。"


def apply(cfg, cmd: Command) -> dict:
    """执行指令。返回 ``{"ok": bool, "message": str, "entry": dict|None}``。

    任何失败都不会改动配置（execute_* 内部校验通过后才写盘）。
    """
    if cmd.action in ("EMPTY", "ERROR"):
        return {"ok": False, "message": cmd.error or "没有可执行的内容", "entry": None}

    if cmd.action == "SKIP":
        e = led.record_event("SKIP", note=cmd.note or "用户选择暂不执行")
        return {"ok": True, "message": "已记入台账：暂不执行（未改动持仓）。", "entry": e}

    if cmd.action == "NOTE":
        e = led.record_event("NOTE", note=cmd.note)
        return {"ok": True, "message": "已记入台账备注（未改动持仓）。", "entry": e}

    if cmd.action in ("SET_CASH", "SET_TOTAL", "SET_GOLD_CAP", "SET_TARGET"):
        try:
            cfg.backup()
        except Exception:  # noqa: BLE001
            pass
        if cmd.action == "SET_CASH":
            cfg.set("portfolio.cash", round(float(cmd.value), 2), autosave=False)
            msg = f"现金已登记为 {float(cmd.value):,.0f} 元。"
        elif cmd.action == "SET_TOTAL":
            cfg.set("portfolio.total_assets", round(float(cmd.value), 2), autosave=False)
            msg = f"总资产已登记为 {float(cmd.value):,.0f} 元。"
        elif cmd.action == "SET_GOLD_CAP":
            cfg.set("portfolio.gold_max", float(cmd.value), autosave=False)
            msg = f"黄金上限已改为 {float(cmd.value) * 100:.1f}%。"
        else:
            now = dict(cfg.get("portfolio.target_class", {}) or {})
            now.update(cmd.targets)
            cfg.set("portfolio.target_class", now, autosave=False)
            msg = "目标配置已更新。"
        cfg.save()
        e = led.record_event("SET", note=cmd.text, extra={"detail": msg})
        return {"ok": True, "message": msg, "entry": e}

    if cmd.action in ("REBALANCE", "BUY"):
        try:
            if cmd.action == "REBALANCE":
                e = led.execute_rebalance(
                    cfg, asset_id=cmd.asset_id, name=cmd.name,
                    sell_shares=int(cmd.shares), price=cmd.price,
                    amount=float(cmd.amount or 0.0), target_pct=None,
                    note="指令栏：" + cmd.text)
            else:
                e = led.execute_buy(
                    cfg, asset_id=cmd.asset_id, name=cmd.name,
                    buy_shares=int(cmd.shares), price=cmd.price,
                    amount=float(cmd.amount or 0.0),
                    note="指令栏：" + cmd.text)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": str(exc), "entry": None}
        return {"ok": True, "message": _result_text(cmd, e), "entry": e}

    return {"ok": False, "message": "不支持的动作", "entry": None}


def run(text: str, cfg) -> tuple[Command, str]:
    """便捷入口：解析 + 预览（不执行）。返回 (cmd, 预览文本)。"""
    cmd = parse(text, cfg)
    return cmd, preview(cmd, cfg)
