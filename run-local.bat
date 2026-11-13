@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

if /i "%~1"=="quiet" (
  if not exist "dist" mkdir dist
  call :run >>"dist\run.log" 2>&1
  exit /b 0
)

call :run
echo.
pause
exit /b 0

:run
echo.
echo ==== %date% %time% ====
echo ==========================================
echo   tv-keep 本地保活
echo ==========================================

rem Scheduled tasks have a different PATH than your shell,
rem so prefer a standalone Python install over whatever is first in PATH.
set "PY="
if exist "%LOCALAPPDATA%\Programs\Python\python310\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\python310\python.exe"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\python311\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\python311\python.exe"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\python312\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\python312\python.exe"
if not defined PY (
  python --version >nul 2>&1 && set "PY=python"
)
if not defined PY (
  echo [错误] 没找到 Python。
  echo 请装 Python 3.10 以上版本，安装时勾选 "Add python.exe to PATH"。
  echo 下载：https://www.python.org/downloads/windows/
  exit /b 1
)

echo 使用 Python: %PY%
echo.

echo [1/4] 直播侧（真测速，走你家宽带，比云端机器准得多）...
"%PY%" live\build_live.py --strict
if errorlevel 2 echo     ^(跌幅过大，已保留上次结果^)
if errorlevel 1 if not errorlevel 2 echo     ^(本次没产出，保留上次结果^)

echo.
echo [2/4] 移动网清单（无法在本机验证，只做归拢 + 接公网兜底）...
"%PY%" live\build_mobile.py
if errorlevel 1 echo     ^(生成失败，保留上次的 live-mobile.m3u^)

echo.
echo [3/4] 点播侧（严格模式，只留真能打开的站）...
"%PY%" vod\build_vod.py --strict
if errorlevel 2 echo     ^(跌幅过大，已保留上次结果^)
if errorlevel 1 if not errorlevel 2 echo     ^(本次没产出，保留上次结果^)

echo.
echo [4/4] 发布上线（推 GitHub + 清 jsDelivr 缓存）...
"%PY%" publish.py
if errorlevel 1 (
  echo.
  echo [提示] 发布没成功。本地产物已更新，电视上还是上一次的内容，
  echo        下次跑会自动补推，不影响你看电视。
)

echo.
echo 完成：%date% %time%
exit /b 0
