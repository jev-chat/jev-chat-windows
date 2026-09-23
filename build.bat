@echo off
setlocal
cd /d "%~dp0"

REM One-click local build. ASCII only: Chinese Windows cmd is GBK.
REM Output: dist\jev-chat-windows\jev-chat-windows.exe

if not exist ".venv\Scripts\python.exe" (
    echo Creating virtualenv .venv ...
    python -m venv .venv || goto :fail
)
call ".venv\Scripts\activate.bat" || goto :fail

echo Installing dependencies ...
REM Tsinghua PyPI mirror: much faster from mainland China; drop -i if you do not want it.
python -m pip install -r requirements.txt pyinstaller -i https://pypi.tuna.tsinghua.edu.cn/simple || goto :fail

echo Building ...
pyinstaller --noconfirm --clean jev.spec || goto :fail

echo.
echo Build OK.
echo   %cd%\dist\jev-chat-windows\jev-chat-windows.exe
echo Ship the whole dist\jev-chat-windows folder: the exe needs the files next to it.
pause
exit /b 0

:fail
echo.
echo Build FAILED. Scroll up for the error.
pause
exit /b 1
