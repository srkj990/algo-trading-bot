@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
set "PYTHON_EXE=%SCRIPT_DIR%venv\Scripts\python.exe"

if not exist "%PYTHON_EXE%" (
    echo [ERROR] Virtual environment python not found:
    echo %PYTHON_EXE%
    exit /b 1
)

cd /d "%SCRIPT_DIR%"
"%PYTHON_EXE%" main.py

endlocal
