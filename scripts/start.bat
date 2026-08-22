@echo off
setlocal
cd /d "%~dp0.."

rem Clear any stale instances first so a start always yields a working stack.
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" | Where-Object { $_.CommandLine -match '(?i)evo_(tray|ear|app)\.pyw' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" >nul 2>&1
del /q data\ear.lock data\tray.lock >nul 2>&1
timeout /t 2 /nobreak >nul

if not exist .venv (
  py -3 -m venv .venv || python -m venv .venv || goto :error
)

call .venv\Scripts\activate.bat
python -m pip install --quiet --disable-pip-version-check -r requirements.txt || goto :error

start "" .venv\Scripts\pythonw.exe evo_tray.pyw
start "" .venv\Scripts\pythonw.exe evo_ear.pyw
echo EVO is starting in the system tray (say "wake up evo" to get its attention)...
goto :eof

:error
echo Setup failed. Ensure Python 3.11+ is installed and on PATH.
pause
