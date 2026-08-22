@echo off
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" | Where-Object { $_.CommandLine -match '(?i)evo_(tray|ear|app)\.pyw' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }; Get-NetTCPConnection -LocalPort 8420 -State Listen -ErrorAction SilentlyContinue | Select-Object -Unique OwningProcess | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }"
echo EVO stopped.
pause
