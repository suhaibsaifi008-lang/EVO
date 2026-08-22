@echo off
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" | Where-Object { $_.CommandLine -match 'evo' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }; Get-NetTCPConnection -LocalPort 8420 -State Listen -ErrorAction SilentlyContinue | Select-Object -Unique OwningProcess | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }"
echo JARVIS stopped.
pause
