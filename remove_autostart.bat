@echo off
schtasks /Delete /F /TN "EVO Console" >nul 2>&1
schtasks /Delete /F /TN "EVO Ear" >nul 2>&1
echo Autostart entries removed. EVO now starts only when you launch it.
pause
