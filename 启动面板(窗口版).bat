@echo off
chcp 936 >nul
cd /d "%~dp0"
title 黑盒语音面板
set "PY=python"
set "PYW=pythonw"

"%PY%" -c "import webview, pystray, PIL" 2>nul
if errorlevel 1 (
  echo 缺依赖，正在装 pywebview pystray pillow …
  "%PY%" -m pip install pywebview pystray pillow
)

start "" "%PYW%" "%~dp0desktop.py"
exit /b 0
