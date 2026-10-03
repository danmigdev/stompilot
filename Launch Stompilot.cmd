@echo off
cd /d "%~dp0"
if exist "%~dp0dist\Stompilot.exe" (
  start "" "%~dp0dist\Stompilot.exe"
) else if exist "%~dp0.venv\Scripts\pythonw.exe" (
  "%~dp0.venv\Scripts\pythonw.exe" "%~dp0midi_configurator\app.py"
) else (
  pythonw.exe "%~dp0midi_configurator\app.py"
)
