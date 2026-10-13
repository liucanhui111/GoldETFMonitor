@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ============================================================
echo  卸载 / 清理
echo ============================================================
echo.
echo 本脚本只会删除「程序自身生成」的构建产物与开发虚拟环境，
echo 不会删除你的持仓配置、历史数据和日志，除非你额外确认。
echo.

if exist ".venv" (
  echo 删除开发虚拟环境 .venv ...
  rmdir /s /q ".venv"
)
if exist "build" (
  echo 删除打包中间目录 build ...
  rmdir /s /q "build"
)
if exist "dist" (
  echo 删除打包输出目录 dist ...
  rmdir /s /q "dist"
)
for /d %%d in (*.spec.__pycache__) do rmdir /s /q "%%d" 2>nul

echo.
echo 顺手清理 Windows 计划任务（如果之前启用了自动启动）：
schtasks /Query /FO LIST 2>nul | findstr /C:"GOLD_ETF_Monitor" >nul
if errorlevel 1 (
  echo   未发现本程序创建的计划任务。
) else (
  echo   发现计划任务，正在删除 ...
  for /f "tokens=*" %%t in ('schtasks /Query /FO LIST ^| findstr /C:"GOLD_ETF_Monitor"') do (
    for /f "tokens=2 delims=:" %%n in ("%%t") do (
      schtasks /Delete /TN "%%n" /F >nul 2>nul
    )
  )
  echo   已删除。
)

echo.
echo 以下内容被保留（重要数据）：
echo   config\       持仓与全部参数配置
echo   data\         历史数据与缓存
echo   logs\         运行日志
echo.
echo 如果确定要彻底删除这些数据，请手动删除上述目录。
echo.
pause
