@echo off
REM ============================================================
REM  One-click launcher for Server+Client (running on the same PC)
REM   Step 1: BMP HTTP API (background cmd window)
REM   Step 2: Bridge GUI (player authenticator)
REM ============================================================
setlocal enabledelayedexpansion
chcp 936 >nul
cd /d "%~dp0"

echo.
echo ============================================================
echo   BMP Suite:  HTTP-API (background)  +  HWID-Bridge (GUI)
echo ============================================================
echo.

REM --- 1) start API in minimized cmd (closing that window = stop API) ---
echo [1/2] Start BMP HTTP API (minimized)...
if not exist "BMP_HTTP_API.py" (
    echo [WARN ] BMP_HTTP_API.py not found. Skip API launch.
    goto :AFTER_API
)
if not exist "start_BMP_API.bat" (
    echo [WARN ] start_BMP_API.bat not found. Skip API launch.
    goto :AFTER_API
)
start "BMP-HTTP-API :12124 (close this = stop API)" /MIN cmd /k ""%~dp0start_BMP_API.bat""
timeout /t 5 /nobreak >nul

REM Check listener
set ALIVE=0
for /F "tokens=5" %%P in ('netstat -ano 2^>nul ^| findstr /R /C:":12124 .* LISTENING"') do (
    echo [OK   ] API port 12124 LISTENING (PID %%P)
    set ALIVE=1
)
if "!ALIVE!"=="0" (
    echo [WARN ] :12124 not listening yet (check "BMP-HTTP-API" cmd window for errors). Continuing anyway.
)

:AFTER_API
echo.

REM --- 2) start Bridge GUI ---
echo [2/2] Start Bridge GUI ...
if exist "start_Bridge.bat" (
    call "%~dp0start_Bridge.bat"
) else (
    start_Bridge.bat not found - cannot launch Bridge.
)

echo.
echo ============================================================
echo  STARTUP COMPLETE
echo  - Set Bridge Tab3 API URL to:  http://127.0.0.1:12124
echo  - Stop the API: close the minimized "BMP-HTTP-API :12124" window
echo  - Stop all helpers: double-click stop_ALL.bat
echo ============================================================
timeout /t 6 >nul
endlocal
