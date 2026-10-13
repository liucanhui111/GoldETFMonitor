@echo off
rem 双击本文件即可启动【全天候 ETF 组合系统】图形界面（看行情 / 组合决策）。
rem 启动的是已打包好的 EXE，不需要安装 Python。
chcp 65001 >nul
cd /d "%~dp0"

set "EXE=dist\GoldETFMonitor\GoldETFMonitor.exe"
if not exist "%EXE%" (
  echo [错误] 未找到 %EXE%
  echo 请先运行 tests\build_exe.py 打包，或确认 dist 目录完整。
  pause
  exit /b 1
)

start "" "%EXE%"
exit /b 0
