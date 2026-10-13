@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ============================================================
echo  个人全天候 ETF 量化资产配置与择时系统 - 安装脚本
echo ============================================================
echo.
echo 说明：
echo   如果你只是想使用本软件，请直接双击 GoldETFMonitor.exe，
echo   完全不需要运行本脚本，也不需要电脑里安装 Python。
echo.
echo   本脚本仅用于「从源码运行」或「重新打包 EXE」。
echo.

where python >nul 2>nul
if errorlevel 1 (
  echo [错误] 未检测到 Python。请先安装 Python 3.10 及以上版本。
  pause
  exit /b 1
)

if not exist ".venv" (
  echo [1/3] 创建虚拟环境 .venv ...
  python -m venv .venv
) else (
  echo [1/3] 已存在 .venv，跳过创建。
)

echo [2/3] 升级 pip ...
call ".venv\Scripts\python.exe" -m pip install --upgrade pip setuptools wheel

echo [3/3] 安装依赖（PySide6）...
call ".venv\Scripts\python.exe" -m pip install -r requirements.txt

echo.
echo 安装完成。
echo   · 从源码启动图形界面： run.bat
echo   · 重新打包 EXE：        build.bat
echo.
pause
