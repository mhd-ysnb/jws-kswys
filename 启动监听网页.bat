@echo off
chcp 936 >nul
cd /d "%~dp0"
title 黑盒语音监听页
set "PY=python"
set "PYTHONIOENCODING=gbk:replace"
echo 启动控制面板（浏览器版），浏览器会自动打开；地址以本窗口打印的为准。
echo 保持此窗口开启，关闭即停止监听。
"%PY%" "%~dp0monitor.py"
pause
