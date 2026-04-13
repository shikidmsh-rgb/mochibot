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
echo  [OK] Python found

REM Create venv (recreate if broken)
if exist .venv\Scripts\activate.bat (
    echo  [OK] Virtual environment exists
) else (
    if exist .venv (
        echo  [WARN] Virtual environment is broken, recreating...
        rd /s /q .venv
    ) else (
        echo  Creating virtual environment...
    )
    python -m venv .venv
    if %errorlevel% neq 0 (
        echo.
        echo  [ERROR] Failed to create virtual environment.
        pause
        exit /b 1
    )
)

call .venv\Scripts\activate.bat

REM Install dependencies
echo  Installing dependencies...
.venv\Scripts\pip.exe install -r requirements.txt fastapi uvicorn cryptography sqlite-vec --quiet
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
echo  When done, click the start button in the admin portal.
echo.

.venv\Scripts\python.exe -m mochi.admin
pause
