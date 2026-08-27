@echo off
REM ============================================================
REM  BeamMP HTTP API Server Launcher - just double-click to run
REM  EDIT THE CONFIG SECTION BELOW BEFORE FIRST RUN
REM ============================================================
setlocal enabledelayedexpansion
chcp 936 >nul

REM ---------- CONFIG (edit to your needs) ----------
set HOST=0.0.0.0
set PORT=12124
set ADMINS=DRIFTKING
set DATA_DIR=bmp_login
REM Optional IP whitelist (comma-separated), empty = allow all
REM set ALLOW_IPS=127.0.0.1,192.168.0.0/16
set ALLOW_IPS=
REM Python launcher (py -3 or python or python3)
set PY=py -3
REM --------------------------------------------------

cd /d "%~dp0"

echo.
echo ============================================================
echo   BeamMP  HTTP  API   (shared with BMPLogin accounts.json)
echo ============================================================
echo   Host      : %HOST%
echo   Port      : %PORT%
echo   Admins    : %ADMINS%
echo   Data dir  : %DATA_DIR%
echo   Allow IPs : %ALLOW_IPS%   [empty = all IPs]
echo ============================================================
echo.

REM --- python check (? goto ?? if ?????) ---
%PY% -c "import sys;sys.exit(0 if sys.version_info >= (3,8) else 1)" >nul 2>nul
if errorlevel 1 goto :PY_MISSING
goto :PY_OK

:PY_MISSING
echo [ERROR] Python 3.8+ not installed or 'py -3' missing.
echo         Install from https://www.python.org/downloads/
echo         [TICK] "Add Python to PATH" while installing
echo.
echo [HINT ] Or change  set PY=py -3  to  set PY=python  in this bat
echo.
goto :END

:PY_OK
echo [OK   ] Python check passed.

REM --- port already listening? ---
netstat -ano | findstr /R /C:":%PORT% .* LISTENING" >nul 2>nul
if errorlevel 1 goto :PORT_FREE
echo [WARN ] Port %PORT% is already LISTENING.
echo         Kill old API first [use stop_ALL.bat] or change PORT.
echo         Continuing in 5s ...  [Ctrl+C to abort]
timeout /t 6 /nobreak >nul

:PORT_FREE
REM --- create data dir ---
if not exist "%DATA_DIR%\" goto :MKDIR
goto :DIR_OK

:MKDIR
echo [INFO ] Data dir '%DATA_DIR%' not found, creating ...
mkdir "%DATA_DIR%"

:DIR_OK
REM --- build args ---
set ARGS=--host %HOST% --port %PORT% --admins "%ADMINS%" --data-dir "%DATA_DIR%"
if not "%ALLOW_IPS%"=="" set ARGS=%ARGS% --allow-ips "%ALLOW_IPS%"

echo [START] %PY% BMP_HTTP_API.py %ARGS%
echo.
echo   ^>^>  Players set Bridge Tab3 API URL = http://ServerPublicIP:%PORT%
echo   ^>^>  Your public IP:  open  https://ifconfig.me  on this machine
echo.
echo   [Ctrl+C twice to stop the API server]
echo ============================================================
echo.

REM === Launch the API (blocks here until Ctrl+C / window close) ===
%PY% BMP_HTTP_API.py %ARGS%

echo.
echo [STOPPED] API exited with code %ERRORLEVEL%
echo.

:END
echo.
echo [Press any key to close this window]
pause >nul
endlocal
