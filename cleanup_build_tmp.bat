@echo off
rem 清理 PyInstaller 打包过程留下的临时目录（_buildtmp_* / _distnew_*）。
rem 这些目录只是中间产物，删除不影响 dist\ 里已打包好的 EXE。
rem 由用户手动双击运行（沙箱环境下批量删除会被系统安全策略拦截）。
chcp 65001 >nul
cd /d "%~dp0"

echo 正在清理构建临时目录…
for /d %%d in (_buildtmp_*) do (
  if exist "%%d" (
    echo   删除 %%d
    rd /s /q "%%d"
  )
)
for /d %%d in (_distnew_*) do (
  if exist "%%d" (
    echo   删除 %%d
    rd /s /q "%%d"
  )
)

echo.
echo 完成。dist\ 内的 EXE 与文档不受影响。
pause
