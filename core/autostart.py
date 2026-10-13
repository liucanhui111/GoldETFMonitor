"""Windows 任务计划程序（Task Scheduler）自动启动。

只为"当前用户、登录时运行"创建普通权限任务，不需要管理员，也不需要保存密码。
任务名统一前缀 GOLD_ETF_，方便查询与清理。
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

PREFIX = "GOLD_ETF_Monitor"


def _run(args: list[str]) -> tuple[bool, str]:
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=30,
                           shell=False, encoding="gbk", errors="ignore")
        out = (p.stdout or "") + (p.stderr or "")
        return p.returncode == 0, out.strip()
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def exe_path() -> str:
    """当前可执行文件路径（打包后为 exe，开发态为 python + main.py）。"""
    if getattr(sys, "frozen", False):
        return str(Path(sys.executable).resolve())
    root = Path(__file__).resolve().parent.parent
    return f'"{sys.executable}" "{root / "main.py"}"'


def _task_name(t: str) -> str:
    return f"{PREFIX}_{t.replace(':', '')}"


def list_tasks() -> list[dict]:
    ok, out = _run(["schtasks", "/Query", "/FO", "LIST", "/V"])
    if not ok:
        return []
    tasks: list[dict] = []
    cur: dict = {}
    for line in out.splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        k, v = k.strip(), v.strip()
        if k in ("任务名", "TaskName"):
            if cur.get("name", "").startswith(PREFIX):
                tasks.append(cur)
            cur = {"name": v}
        elif k in ("下次运行时间", "Next Run Time"):
            cur["next_run"] = v
        elif k in ("状态", "Status"):
            cur["status"] = v
        elif k in ("要运行的任务", "Task To Run"):
            cur["command"] = v
    if cur.get("name", "").startswith(PREFIX):
        tasks.append(cur)
    return tasks


def enable(times: list[str]) -> tuple[bool, str]:
    """按给定时间列表（HH:MM）创建每日任务。"""
    disable()
    msgs = []
    all_ok = True
    tr = exe_path()
    for t in times:
        t = t.strip()
        if not re.fullmatch(r"\d{1,2}:\d{2}", t):
            msgs.append(f"跳过非法时间：{t}")
            all_ok = False
            continue
        hh, mm = t.split(":")
        t = f"{int(hh):02d}:{mm}"
        ok, out = _run(["schtasks", "/Create", "/SC", "DAILY", "/ST", t,
                        "/TN", _task_name(t), "/TR", tr, "/RL", "LIMITED", "/F"])
        msgs.append(f"{t} → {'成功' if ok else '失败：' + out[:200]}")
        all_ok = all_ok and ok
    return all_ok, "\n".join(msgs)


def disable() -> tuple[bool, str]:
    ok, out = _run(["schtasks", "/Query", "/FO", "LIST"])
    names = re.findall(rf"{PREFIX}_\d+", out or "")
    if not names:
        return True, "当前没有本程序创建的计划任务"
    msgs = []
    for n in set(names):
        o, txt = _run(["schtasks", "/Delete", "/TN", n, "/F"])
        msgs.append(f"{n} → {'已删除' if o else '删除失败：' + txt[:120]}")
    return True, "\n".join(msgs)


def is_enabled() -> bool:
    return bool(list_tasks())
