@echo off
chcp 65001 >nul
title DouK Studio
cd /d "%~dp0"
python server.py
pause
