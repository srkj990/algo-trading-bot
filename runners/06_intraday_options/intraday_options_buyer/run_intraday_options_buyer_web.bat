@echo off
cd /d "%~dp0"
set INTRADAY_STATE_DIR=%~dp0state
set TRADE_STORE_DIR=%~dp0state\trade_store
..\..\..\venv\Scripts\python.exe intraday_options_buyer_web.py
pause
