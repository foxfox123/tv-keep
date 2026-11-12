@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo.
echo ==========================================
echo   tv-keep 本地保活
echo ==========================================
echo.

python --version >nul 2>&1
if errorlevel 1 (
  echo [错误] 没找到 python 命令。
  echo 请先装 Python 3.9 以上版本，安装时勾选 "Add python.exe to PATH"。
  echo 下载：https://www.python.org/downloads/windows/
  pause
  exit /b 1
)

echo [1/3] 直播侧（真测速，走的是你家宽带，比云端的机器准得多）...
python live\build_live.py --strict
if errorlevel 2 echo     ^(跌幅过大，已保留上次结果^)
if errorlevel 1 if not errorlevel 2 echo     ^(本次没产出，保留上次结果^)

echo.
echo [2/3] 点播侧（严格模式，只留真能打开的站）...
python vod\build_vod.py --strict
if errorlevel 2 echo     ^(跌幅过大，已保留上次结果^)
if errorlevel 1 if not errorlevel 2 echo     ^(本次没产出，保留上次结果^)

echo.
echo [3/3] 发布上线（推 GitHub + 清 jsDelivr 缓存）...
python publish.py
if errorlevel 1 (
  echo.
  echo [提示] 发布没成功。本地产物已更新，电视上还是上一次的内容，
  echo        下次跑会自动补推，不影响你看电视。
)

echo.
pause
