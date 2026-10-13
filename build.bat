@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ============================================================
echo  打包为 Windows 可执行文件（无需 Python 即可运行）
echo ============================================================
echo.

set PY=python
if exist ".venv\Scripts\python.exe" set PY=.venv\Scripts\python.exe

%PY% -c "import PyInstaller" 2>nul
if errorlevel 1 (
  echo [提示] 未安装 PyInstaller，正在安装 ...
  %PY% -m pip install pyinstaller
)

%PY% -c "import PySide6" 2>nul
if errorlevel 1 (
  echo [提示] 未安装 PySide6，正在按 requirements.txt 安装 ...
  %PY% -m pip install -r requirements.txt
)

if exist "build" rmdir /s /q "build"
if exist "dist" rmdir /s /q "dist"

echo [1/3] 打包图形界面版本（无控制台窗口）...
%PY% -m PyInstaller --noconfirm --clean --windowed ^
  --name GoldETFMonitor ^
  --paths . ^
  --add-data "config/config.json;config" ^
  --exclude-module tkinter --exclude-module matplotlib ^
  --exclude-module numpy --exclude-module pandas ^
  --exclude-module PIL --exclude-module scipy ^
  main.py
if errorlevel 1 goto :fail

echo [2/3] 打包命令行诊断版本（保留控制台，便于排查问题）...
%PY% -m PyInstaller --noconfirm --clean --console ^
  --name GoldETFMonitor-cli ^
  --paths . ^
  --add-data "config/config.json;config" ^
  --exclude-module tkinter --exclude-module matplotlib ^
  --exclude-module numpy --exclude-module pandas ^
  --exclude-module PIL --exclude-module scipy ^
  main.py
if errorlevel 1 goto :fail

echo [3/3] 拷贝初始配置与说明文件 ...
if not exist "dist\GoldETFMonitor\config" mkdir "dist\GoldETFMonitor\config"
if not exist "dist\GoldETFMonitor\reports" mkdir "dist\GoldETFMonitor\reports"
copy /y "config\config.json" "dist\GoldETFMonitor\config\config.json" >nul
rem 入口文档 + 程序运行时读取的 P0 证据报告（放包根目录，保持清爽）
if exist "README.txt" copy /y "README.txt" "dist\GoldETFMonitor\README.txt" >nul
if exist "TEST_REPORT.md" copy /y "TEST_REPORT.md" "dist\GoldETFMonitor\TEST_REPORT.md" >nul
if exist "DATA_SOURCE.md" copy /y "DATA_SOURCE.md" "dist\GoldETFMonitor\DATA_SOURCE.md" >nul
if exist "BACKTEST_REPORT.md" copy /y "BACKTEST_REPORT.md" "dist\GoldETFMonitor\BACKTEST_REPORT.md" >nul
if exist "DATA_VALIDATION_REPORT.md" copy /y "DATA_VALIDATION_REPORT.md" "dist\GoldETFMonitor\DATA_VALIDATION_REPORT.md" >nul
if exist "STRATEGY_VALIDATION_REPORT.md" copy /y "STRATEGY_VALIDATION_REPORT.md" "dist\GoldETFMonitor\STRATEGY_VALIDATION_REPORT.md" >nul
if exist "BACKTEST_REAL_6ETF.md" copy /y "BACKTEST_REAL_6ETF.md" "dist\GoldETFMonitor\BACKTEST_REAL_6ETF.md" >nul
rem 纯证据类研究/审计/验证报告统一归入 reports/ 子目录（程序不读取）
for %%F in (STRATEGY_VALIDATION_V2.md TRADE_POLICY_VALIDATION.md SELL_VALIDATION_REPORT.md PARAMETER_SENSITIVITY_REPORT.md BACKTEST_REAL_6ETF_V2.md SHADOW_TRADING_REPORT.md EVENT_FUNNEL_REPORT.md SAMPLE_ADEQUACY_REPORT.md TRADE_EVENT_AUDIT.md STRATEGY_VALIDATION_V3.md) do if exist "%%F" copy /y "%%F" "dist\GoldETFMonitor\reports\%%F" >nul

echo.
echo ============================================================
echo  打包完成
echo ============================================================
echo   图形界面： dist\GoldETFMonitor\GoldETFMonitor.exe
echo   命令行版： dist\GoldETFMonitor\GoldETFMonitor-cli.exe
echo.
echo   把整个 dist\GoldETFMonitor 文件夹拷贝到任意位置即可使用，
echo   目标电脑无需安装 Python。持仓与数据保存在同目录 config/ 与 data/ 下。
echo.
pause
exit /b 0

:fail
echo.
echo [失败] 打包过程出错，请查看上方日志。
pause
exit /b 1
