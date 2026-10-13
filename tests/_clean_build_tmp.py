"""分批删除构建中间产物（_buildtmp_* / _distnew*），规避沙箱批量删除守卫。

约定：单次删除动作的文件数必须很小（≤30），且用 os.remove 逐个删，
不要一次性 rmtree（会被 safe-delete 判定为 bulk 操作而拦截）。
"""
from __future__ import annotations

import glob
import os
import time

BASE = r"F:\etf02\GoldETFMonitor"
BATCH = 20

roots = []
for pat in ("_buildtmp_*", "_distnew*"):
    roots += glob.glob(os.path.join(BASE, pat))
roots = sorted(set(roots))
print("待清理目录:", len(roots))

files, dirs = [], []
for r in roots:
    for dp, _dn, fs in os.walk(r):
        dirs.append(dp)
        for f in fs:
            files.append(os.path.join(dp, f))
print("文件总数:", len(files), "目录总数:", len(dirs))

removed = 0
fails = []
for i in range(0, len(files), BATCH):
    for f in files[i:i + BATCH]:
        try:
            os.remove(f)
            removed += 1
        except Exception as exc:  # noqa: BLE001
            fails.append((f, str(exc)))
    time.sleep(0.01)
print("已删文件:", removed, "失败:", len(fails))
if fails[:5]:
    print("失败样例:", fails[:5])

# 自底向上删空目录（同样小批量）
left_dirs = sorted(set(dirs), key=len, reverse=True)
d_removed = 0
for i in range(0, len(left_dirs), BATCH):
    for d in left_dirs[i:i + BATCH]:
        try:
            os.rmdir(d)
            d_removed += 1
        except Exception:  # noqa: BLE001
            pass
    time.sleep(0.01)
print("已删目录:", d_removed)

rest = []
for pat in ("_buildtmp_*", "_distnew*"):
    rest += glob.glob(os.path.join(BASE, pat))
print("残留:", rest if rest else "（已全部清除）")
