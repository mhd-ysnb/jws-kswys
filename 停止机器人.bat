@echo off
chcp 936 >nul
cd /d "%~dp0"
title Í£Ö¹»úÆ÷ÈËÓë¼àÌıÒ³
set "PY=python"
set "PYTHONIOENCODING=gbk:replace"
"%PY%" "%~dp0stop.py"
pause
