"""严格 Walk-Forward 切分（训练窗 / 测试窗 / PURGE / EMBARGO）。

纪律（本轮硬要求）
------------------
1. **禁止全样本调参**：参数只能在训练集选择；测试窗口内的数据在"参数被冻结"
   之前一律不可见；
2. 训练窗口 504 日；测试窗口按 20 / 60 / 120 分别评价；
3. **PURGE / EMBARGO**：训练段与测试段之间强制留出 embargo 天；
   并且任何"未来 h 日收益"标签都不能跨越 train/test 边界（purge = h）；
4. **独立事件去重**：h=60 的事件之间至少间隔 60 个交易日，h=120 至少 120，
   禁止用高度重叠的窗口冒充大量独立样本；
5. 未满足"训练样本 + 前瞻窗口可用"的折一律跳过，不制造样本。
"""
from __future__ import annotations


def purged_folds(n: int, *, train_days: int = 504, test_days: int = 60,
                 embargo: int = 10, min_train: int = 252,
                 horizon: int = 60) -> list[dict]:
    """生成"扩展窗口"的 purged/embargoed 折。

    每折：train = [0, train_end)，test = [test_start, test_end)；
          test_start = train_end + embargo（embargo 起隔离作用）；
          且要求 test_end + horizon <= n（测试段的前瞻收益必须真实存在）。
    """
    folds: list[dict] = []
    base = max(int(train_days), int(min_train))
    start = base + int(embargo)
    while True:
        test_start = start
        test_end = test_start + int(test_days)
        if test_end + int(horizon) > n:
            break
        folds.append({
            "fold": len(folds),
            "train_start": 0,
            "train_end": test_start - int(embargo),   # 训练段右开区间
            "embargo": int(embargo),
            "test_start": test_start,
            "test_end": test_end,
            "test_days": int(test_days),
            "horizon": int(horizon),
        })
        start = test_end
    return folds


def dedup_events(indices, min_gap: int) -> list[int]:
    """把事件索引按"至少间隔 min_gap"去重（保留最早出现的）。"""
    out: list[int] = []
    last = None
    for i in sorted(int(x) for x in indices):
        if last is None or (i - last) >= int(min_gap):
            out.append(i)
            last = i
    return out


def independent_events(n: int, *, horizon: int, start: int = 0,
                       end: int | None = None) -> list[int]:
    """在 [start, end) 内给出"互不重叠"的独立事件锚点（间隔 = horizon）。"""
    end = n if end is None else end
    idx = list(range(int(start), max(int(start), int(end))))
    return dedup_events(idx, int(horizon))


def purged_indices(train_end: int, horizon: int) -> list[int]:
    """训练可用索引上界（左闭右开的右端）：训练标签不能跨入 embargo。

    返回训练段最后一个"其未来 horizon 日标签完全落在 train_end 之前"的索引。
    用于测试中验证"参数不泄露到测试期 / purge 正确"。
    """
    return list(range(0, max(0, int(train_end) - int(horizon) + 1)))


def check_no_leak(fold: dict) -> dict:
    """自检：训练段与测试段是否被 embargo 正确隔离（无重叠、无跨越）。"""
    te = int(fold.get("train_end") or 0)
    ts = int(fold.get("test_start") or 0)
    emb = int(fold.get("embargo") or 0)
    ok_gap = ts >= te + emb
    ok_order = te > 0 and ts >= te
    return {
        "ok": bool(ok_gap and ok_order),
        "train_end": te, "test_start": ts, "embargo": emb,
        "detail": (f"train_end={te} + embargo={emb} <= test_start={ts}"
                   if ok_gap else f"隔离不足：train_end={te}, test_start={ts}, embargo={emb}"),
    }


def fold_summary(folds: list[dict]) -> dict:
    if not folds:
        return {"n_folds": 0, "detail": "无可用折（数据不足以完成 train+embargo+test+horizon）"}
    return {
        "n_folds": len(folds),
        "test_days": folds[0]["test_days"],
        "horizon": folds[0]["horizon"],
        "span": f"{folds[0]['test_start']}~{folds[-1]['test_end']}（索引）",
        "embargo": folds[0]["embargo"],
    }
