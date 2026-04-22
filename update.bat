@echo off
cd /d "%~dp0"

echo.
echo  ------------------------------------------
echo    MochiBot Update
echo  ------------------------------------------
echo.

REM 0. Check this is a git clone install
if not exist .git (
    echo  [ERROR] Not a git clone install - no .git found.
    echo  This update script only works on git clone installs.
    echo  Fix: delete this folder and reinstall with:
    echo      git clone https://github.com/shikidmsh-rgb/mochibot.git
    pause
    exit /b 1
)

REM 1. Check git available
git --version >nul 2>&1
if %errorlevel% neq 0 (
    echo  [ERROR] git not found. Install Git for Windows: https://git-scm.com/download/win
    pause
    exit /b 1
)

REM 2. Check .venv exists
if not exist .venv\Scripts\python.exe (
    echo  [ERROR] Virtual environment .venv not found.
    echo  Please run setup.bat first to complete initial install.
    pause
    exit /b 1
)

REM 3. Check bot is not running (port 8080 occupied)
netstat -ano | findstr ":8080 " | findstr "LISTENING" >nul
if %errorlevel% equ 0 (
    echo  [ERROR] MochiBot appears to be running - port 8080 in use.
    echo  Please close the setup.bat window first, then re-run update.bat.
    pause
    exit /b 1
)

REM 4. Record old commit
for /f %%i in ('git rev-parse --short HEAD') do set OLD_COMMIT=%%i
echo  Current version: %OLD_COMMIT%
echo.

REM 5. git pull (no auto conflict resolution)
echo  Pulling latest code...
git pull
if %errorlevel% neq 0 (
    echo.
    echo  [ERROR] git pull failed.
    echo  Likely cause: you have local edits causing a merge conflict.
    echo  See: docs/getting-started.md section "If you hit a conflict"
    pause
    exit /b 1
)

for /f %%i in ('git rev-parse --short HEAD') do set NEW_COMMIT=%%i
echo  New version: %NEW_COMMIT%

REM 6. Install/update dependencies
echo.
echo  Updating dependencies...
.venv\Scripts\pip.exe install -r requirements.txt
if %errorlevel% neq 0 (
    echo.
    echo  [ERROR] Dependency install failed.
    echo  Common causes: network issues, or bot still running (files locked).
    echo  Check the error above and retry.
    pause
    exit /b 1
)

REM 7. Notify if .env.example changed
git diff %OLD_COMMIT% HEAD --name-only | findstr ".env.example" >nul
if %errorlevel% equ 0 (
    echo.
    echo  ------------------------------------------
    echo  [INFO] .env.example was updated! New config options may be available.
    echo  Compare .env.example with your .env and add any new entries you need.
    echo  ------------------------------------------
)

REM 8. Clean stale __pycache__ to avoid loading old .pyc
for /d /r mochi %%d in (__pycache__) do @if exist "%%d" rd /s /q "%%d"

echo.
echo  ------------------------------------------
echo    Update complete! %OLD_COMMIT% -^> %NEW_COMMIT%
echo    Launching MochiBot in a new window...
echo  ------------------------------------------
echo.

REM 9. Launch setup.bat in independent window, then exit
start "MochiBot" cmd /c setup.bat

timeout /t 3 >nul
exit /b 0
