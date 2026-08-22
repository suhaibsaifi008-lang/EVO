@echo off
setlocal
cd /d "%~dp0.."

if not exist .venv (
  py -3 -m venv .venv || python -m venv .venv || goto :error
)

call .venv\Scripts\activate.bat
python -m pip install --quiet --disable-pip-version-check -r requirements.txt || goto :error

start "" .venv\Scripts\pythonw.exe evo_tray.pyw
start "" .venv\Scripts\pythonw.exe evo_ear.pyw
echo EVO is starting in the system tray (ear is listening for the wake word)...
goto :eof

:error
echo Setup failed. Ensure Python 3.11+ is installed and on PATH.
pause
