@echo off
chcp 936 >nul
cd /d "%~dp0"
title 黑盒语音客户端
set "PY=python"

for /f "delims=" %%i in ('%PY% -c "import json;print(json.load(open('config.json',encoding='utf-8')).get('client',{}).get('exe',''))" 2^>nul') do set "EXE=%%i"
if "%EXE%"=="" (
  echo 先把 config.json 里 client.exe 填成 HeyboxChat.exe 的完整路径，再跑这个脚本。
  pause
  exit /b 1
)

echo [1/3] 看调试端口 9222
powershell -NoProfile -Command "try{$c=New-Object Net.Sockets.TcpClient;$c.Connect('127.0.0.1',9222);$c.Close();exit 0}catch{exit 1}"
if not errorlevel 1 (
  echo       已经在线。窗口开着，机器人可以直接连。
  pause
  exit /b 0
)

echo [2/3] 看客户端在不在
tasklist /FI "IMAGENAME eq HeyboxChat.exe" /NH | find /I "HeyboxChat.exe" >nul
if errorlevel 1 goto LAUNCH

echo       窗口开着，但没带调试端口——机器人连不上它。
echo       要能连，必须带 --remote-debugging-port=9222 重开一次。
echo       注意：这会关掉你现在开着的这个窗口。
set "YN="
set /p "YN=       确认关掉重开？(y/N) "
if /I not "%YN%"=="y" (
  echo       那先这样。机器人这会儿连不上它。
  pause
  exit /b 1
)
taskkill /IM HeyboxChat.exe /F >nul 2>&1
timeout /t 3 >nul

:LAUNCH
echo [3/3] 用调试模式开窗口
start "" "%EXE%" --remote-debugging-port=9222
echo       等 12 秒让页面加载…
timeout /t 12 >nul
echo       完成。这个窗口现在归你自己管，机器人只是连上来。
pause
