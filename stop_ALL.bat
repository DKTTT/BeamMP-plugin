@echo off
REM ============================================================
REM  One-click stop: kill BMP HTTP API + leftover Bridge helpers
REM ============================================================
setlocal enabledelayedexpansion
chcp 936 >nul

echo.
echo ============================================================
echo  STOP  BMP-HTTP-API  /  BMPHWID-Bridge  helpers
echo ============================================================
echo.

set /A KILLED=0

REM --- by name via tasklist /V CSV column (window title / cmdline) ---
echo [INFO ] Killing by script name match ...
for /F "tokens=2 delims=," %%P in ('tasklist /V /FO CSV 2^>nul ^| findstr /I /C:"BMP_HTTP_API.py" /C:"BMPHWID_Bridge.py" /C:"start_BMP_API.bat"') do (
    set PID=%%~P
    if not "!PID!"=="PID" (
        echo [KILL ] name-match PID !PID!
        taskkill /PID !PID! /F /T >nul 2>nul
        set /A KILLED+=1
    )
)

REM --- by window title "BMP-HTTP-API :12124 ..." ---
echo [INFO ] Killing by window title "BMP-HTTP-API" ...
taskkill /F /FI "WINDOWTITLE eq BMP-HTTP-API :12124*" >nul 2>nul
taskkill /F /FI "WINDOWTITLE eq BMP-HTTP-API*"       >nul 2>nul

REM --- by listening port 12124 (fallback) ---
echo [INFO ] Scanning listeners on port 12124 ...
for /F "tokens=5" %%P in ('netstat -ano 2^>nul ^| findstr /R /C:":12124 .* LISTENING"') do (
    echo [KILL ] port 12124 PID=%%P
    taskkill /PID %%P /F /T >nul 2>nul
    set /A KILLED+=1
)

echo.
if %KILLED% EQU 0 (
    echo [DONE ] Nothing looked like it was running.
) else (
    echo [DONE ] Matches killed (see above).
)
echo.
timeout /t 4 >nul
endlocal
