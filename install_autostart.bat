@echo off
setlocal
cd /d "%~dp0"

set "PYW=%CD%\.venv\Scripts\pythonw.exe"
if not exist "%PYW%" (
  echo Run start.bat once first so the virtual environment exists.
  pause
  exit /b 1
)

schtasks /Delete /F /TN "EVO Console" >nul 2>&1
schtasks /Create /F /SC ONLOGON /TN "EVO Console" /TR "\"%PYW%\" \"%CD%\evo_app.pyw\"" >nul || goto :error
schtasks /Create /F /SC ONLOGON /TN "EVO Ear"    /TR "\"%PYW%\" \"%CD%\evo_ear.pyw\"" >nul || goto :error

echo.
echo  EVO is now ALWAYS ON:
echo   - Console app opens when you log in
echo   - Wake-word ear listens from login onward
echo  (Remove anytime with remove_autostart.bat)
pause
exit /b 0

:error
echo Failed to create scheduled tasks ^(try running as Administrator^).
pause
