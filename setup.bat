@echo off
cd /d "%~dp0"

echo.
echo  ------------------------------------------
echo    MochiBot Setup
echo  ------------------------------------------
echo.

REM Check Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo  [ERROR] Python not found.
    echo  Please install Python 3.11+ from https://www.python.org/downloads/
    echo  Make sure to check "Add Python to PATH" during install.
    pause
    exit /b 1
)

REM Check Python version
for /f "tokens=2 delims= " %%v in ('python --version 2^>^&1') do set PYVER=%%v
for /f "tokens=1,2 delims=." %%a in ("%PYVER%") do (
    if %%a lss 3 (
        echo  [ERROR] Python 3.11+ required, found %PYVER%
        pause
        exit /b 1
    )
    if %%a equ 3 if %%b lss 11 (
        echo  [ERROR] Python 3.11+ required, found %PYVER%
        pause
        exit /b 1
    )
)
echo  [OK] Python %PYVER%

REM Create venv
if exist .venv (
    echo  [OK] Virtual environment already exists, skipping creation.
) else (
    echo  Creating virtual environment...
    python -m venv .venv
)
call .venv\Scripts\activate.bat

REM Install dependencies
echo  Installing dependencies...
pip install -r requirements.txt fastapi uvicorn --quiet
if %errorlevel% neq 0 (
    echo.
    echo  [ERROR] Dependency install failed.
    echo  Check your internet connection and try again.
    pause
    exit /b 1
)
echo  [OK] Dependencies installed.

REM Launch
echo.
echo  ------------------------------------------
echo    Setup complete!
echo    Opening admin portal...
echo    http://127.0.0.1:8080
echo  ------------------------------------------
echo.
echo  Configure your API keys and bot token in the browser.
echo  When done, click "启动 Bot" in the admin portal to start the bot.
echo.

start http://127.0.0.1:8080
python -m mochi.admin
