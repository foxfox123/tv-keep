@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

if 1==2 (
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
exit /b 0
