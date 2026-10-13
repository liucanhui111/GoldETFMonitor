# -*- coding: utf-8 -*-
"""应用版本标识（单一事实来源）。

窗口标题栏与设置页都会显示：APP_VERSION + BUILD_STAMP，
用于肉眼区分"运行的是哪一版 EXE"（2026-10-09 用户反馈分不清新旧界面）。

BUILD_STAMP 由 tests/build_exe.py 在每次打包前自动写入构建时刻；
源码态直接运行时若从未打包过则为空串，界面显示"源码态"。
"""

APP_VERSION = "2026.10.09"
BUILD_STAMP = "2026-10-09 21:23"  # 形如 "2026-10-09 00:14"；打包时由 build_exe.py 自动覆盖
