@echo off
setlocal
cd /d "%~dp0"

REM Start the assistant with the project virtualenv (system Python lacks the deps).
REM ASCII only: Chinese Windows cmd is GBK.

if not exist ".venv\Scripts\python.exe" (
    echo .venv not found. Create it first:
    echo   python -m venv .venv
    echo   .venv\Scripts\pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
    pause
    exit /b 1
)
".venv\Scripts\python.exe" main.py
if errorlevel 1 pause
