@echo off
cd /d "%~dp0"
python -m venv "%~dp0.venv"
if errorlevel 1 goto failed
"%~dp0.venv\Scripts\python.exe" -m pip install -r "%~dp0requirements.txt"
if errorlevel 1 goto failed
where node.exe >nul 2>nul
if errorlevel 1 goto node_missing
call npm.cmd --prefix "%~dp0frontend" ci
if errorlevel 1 goto failed
call npm.cmd --prefix "%~dp0frontend" run build
if errorlevel 1 goto failed
echo Stompilot is ready. Open Launch Stompilot.cmd. Microsoft Edge WebView2 Runtime is required.
pause
exit /b 0
:node_missing
echo Source setup requires Node.js 20.19 or later to build the Svelte interface.
pause
exit /b 1
:failed
echo Setup failed. Check that Python 3.12 or later is installed.
pause
exit /b 1
