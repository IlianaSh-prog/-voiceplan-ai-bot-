@echo off
rem VoicePlan bot launcher: creates venv on first run, then starts the bot.
rem Close this window to stop the bot.
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo First run: creating virtual environment...
    python -m venv .venv
    call .venv\Scripts\activate.bat
    python -m pip install --upgrade pip
    python -m pip install -r requirements.txt
) else (
    call .venv\Scripts\activate.bat
)

rem Load environment variables from .env (local run; on Render they are set in the dashboard).
if exist ".env" (
    for /f "usebackq eol=# tokens=1,* delims==" %%A in (".env") do set "%%A=%%B"
)
if not defined TELEGRAM_BOT_TOKEN (
    echo ERROR: TELEGRAM_BOT_TOKEN not found in .env
    pause
    exit /b 1
)

echo.
echo Starting VoicePlan bot. Do not close this window while the bot must run.
echo.
python bot.py
pause