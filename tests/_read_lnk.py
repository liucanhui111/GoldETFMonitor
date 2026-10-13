"""读取 .lnk 快捷方式中的目标路径（不依赖 COM）。"""
from __future__ import annotations

import re
import sys

for path in sys.argv[1:]:
    try:
        raw = open(path, "rb").read()
    except Exception as exc:  # noqa: BLE001
        print(path, "读取失败", exc)
        continue
    print("=" * 60)
    print("LNK:", path, "size", len(raw))
    # UTF-16LE 字符串（快捷方式里路径通常是宽字符）
    u16 = raw.decode("utf-16-le", errors="ignore")
    for m in re.findall(r"[A-Za-z]:\\[^\x00-\x1f\"<>|]{3,200}", u16):
        print("  U16 ->", m)
    ascii_like = raw.decode("latin-1", errors="ignore")
    for m in re.findall(r"[A-Za-z]:\\[^\x00-\x1f\"<>|]{3,200}", ascii_like):
        print("  ASC ->", m)
    for m in re.findall(r"[\u4e00-\u9fff]{2,20}", u16):
        if m.strip():
            print("  CN  ->", m)
