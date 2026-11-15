@echo off
setlocal
rem Builds the Android app: the interface, then the APK, copied to dist\Stompilot.apk.
rem Needs Node.js, a JDK 17 or later (JAVA_HOME) and the Android SDK (ANDROID_HOME or android\local.properties).
set "ROOT=%~dp0"

call npm --prefix "%ROOT%frontend" run build
if errorlevel 1 (echo Interface build failed. & exit /b 1)

if not exist "%ROOT%android\local.properties" if defined ANDROID_HOME (
  >"%ROOT%android\local.properties" echo sdk.dir=%ANDROID_HOME:\=/%
)

call "%ROOT%android\gradlew.bat" -p "%ROOT%android" assembleDebug
if errorlevel 1 (echo Android build failed. & exit /b 1)

if not exist "%ROOT%dist" mkdir "%ROOT%dist"
copy /y "%ROOT%android\app\build\outputs\apk\debug\app-debug.apk" "%ROOT%dist\Stompilot.apk" >nul
echo Built dist\Stompilot.apk
