@echo off
rem Builds a standalone Windows executable: dist\EVO\EVO.exe
cd /d "%~dp0"
call .venv\Scripts\activate.bat

pyinstaller --noconfirm --clean --windowed --name EVO ^
  --add-data "static;static" ^
  --paths . ^
  --hidden-import uvicorn.logging ^
  --hidden-import uvicorn.loops.auto ^
  --hidden-import uvicorn.loops.asyncio ^
  --hidden-import uvicorn.protocols.http.auto ^
  --hidden-import uvicorn.protocols.http.h11_impl ^
  --hidden-import uvicorn.protocols.websockets.auto ^
  --hidden-import uvicorn.protocols.websockets.websockets_impl ^
  --hidden-import uvicorn.lifespan.on ^
  --hidden-import core.listener ^
  evo_app.pyw

echo.
if exist dist\EVO\EVO.exe (
  if exist .env copy /Y .env dist\EVO\.env >nul
  echo SUCCESS: dist\EVO\EVO.exe created.
  echo Your .env was copied next to it - data is stored beside the exe.
) else (
  echo Build failed - see output above.
)
pause
