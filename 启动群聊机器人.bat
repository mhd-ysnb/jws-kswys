@echo off
chcp 936 >nul
cd /d "%~dp0"
title 黑盒语音群聊机器人
set "PY=python"
set "PYTHONIOENCODING=gbk:replace"

echo [1/3] 检查客户端调试端口 9222
powershell -NoProfile -Command "try{$c=New-Object Net.Sockets.TcpClient;$c.Connect('127.0.0.1',9222);$c.Close();exit 0}catch{exit 1}"
if errorlevel 1 (
  echo       没连上客户端。先双击  黑盒客户端.bat  把窗口开起来。
  echo       客户端窗口是独立的，这个脚本不会替你关它。
  pause
  exit /b 1
)
echo       已在线

echo [2/3] 检查依赖
"%PY%" -c "import websocket" 2>nul
if errorlevel 1 (
  echo       缺 websocket-client，执行： pip install websocket-client
  pause
  exit /b 1
)
echo       OK

echo [3/4] 自检
"%PY%" "%~dp0bot.py" --check
echo.
echo [4/4] 启动机器人（Ctrl+C 停；停机器人用 停止机器人.bat）
"%PY%" "%~dp0bot.py"
pause
