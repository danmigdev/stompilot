@echo off
cd /d "%~dp0"
python -m venv "%~dp0.venv"
if errorlevel 1 goto failed
"%~dp0.venv\Scripts\python.exe" -m pip install -r "%~dp0requirements.txt"
if errorlevel 1 goto failed
echo Stompilot is ready. Open Launch Stompilot.cmd.
pause
exit /b 0
:failed
echo Setup failed. Check that Python 3.12 or later is installed.
pause
exit /b 1
