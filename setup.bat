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

REM Check Python version >= 3.11
for /f "tokens=*" %%v in ('python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"') do set PY_VERSION=%%v
for /f "tokens=*" %%v in ('python -c "import sys; print(1 if sys.version_info >= (3, 11) else 0)"') do set PY_OK=%%v
if "%PY_OK%"=="0" (
    echo  [ERROR] Python 3.11+ required, found %PY_VERSION%
    pause
    exit /b 1
)
echo  [OK] Python %PY_VERSION%

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
.venv\Scripts\pip.exe install -r requirements.txt fastapi uvicorn cryptography sqlite-vec aiohttp --quiet
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
echo  ------------------------------------------
echo.
echo  Opening admin portal at http://127.0.0.1:8080
echo  Configure your API keys and bot token in the browser.
echo  When done, click the start button in the admin portal.
echo.
echo  Cloud server? Run this on your local machine:
echo    ssh -L 8080:localhost:8080 user@your-server-ip
echo  Then open http://localhost:8080?token=YOUR_TOKEN
echo.

.venv\Scripts\python.exe -m mochi.admin
pause
