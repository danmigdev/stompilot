@echo off
setlocal
pushd "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  python -m venv .venv
  if errorlevel 1 goto failed
)
".venv\Scripts\python.exe" -m pip install -r requirements-build.txt
if errorlevel 1 goto failed
where node.exe >nul 2>nul
if errorlevel 1 goto node_missing
call npm.cmd --prefix frontend ci
if errorlevel 1 goto failed
call npm.cmd --prefix frontend run check
if errorlevel 1 goto failed
call npm.cmd --prefix frontend run build
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean Stompilot.spec
if errorlevel 1 goto failed
echo Stompilot.exe is ready in the dist folder.
popd
exit /b 0
:node_missing
echo Building the Svelte interface requires Node.js 20.19 or later.
popd
exit /b 1
:failed
echo Build failed. Review the messages above. Building requires Python 3.12 or later and Node.js 20.19 or later.
popd
exit /b 1
