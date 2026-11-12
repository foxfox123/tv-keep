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
echo [3/3] 推送上线...
if exist "cf\.token" (
  python cf\push.py
  if errorlevel 1 (
    echo     直连不通，改用 Cloudflare API 通道再试一次...
    python cf\push.py --via wrangler
    if errorlevel 1 echo     ^(两条通道都没成功；本地产物已更新，下次跑会自动补推^)
  )
  goto :done
)

if exist ".git" (
  git add dist
  git diff --staged --quiet
  if not errorlevel 1 (
    echo 内容没有变化，不用推送。
    goto :done
  )
  git -c user.name="tv-keep" -c user.email="tv-keep@local" commit -m "chore: local refresh %date% %time%"
  if errorlevel 1 (
    echo [提示] 提交失败，通常是本机 git 还没配置身份。
    goto :done
  )
  git push
  if errorlevel 1 (
    echo.
    echo [提示] 推送失败。第一次用请先执行：
    echo     git remote add origin https://github.com/你的用户名/tv-keep.git
    echo     git push -u origin main
  ) else (
    echo 已推送，电视上的地址不变。
  )
  goto :done
)

echo 还没配推送目标，二选一：
echo   用 Cloudflare：按 README 的「Cloudflare 部署」做完，把 token 写进 cf\.token
echo   用 GitHub    ：把整个目录推送到你的仓库，再跑一次本脚本

:done
echo.
pause
