@echo off
REM ============================================================
REM  BeamMP HWID Bridge Launcher - double-click
REM ============================================================
setlocal enabledelayedexpansion
chcp 936 >nul
cd /d "%~dp0"

set EXE=dist\BMPHWID_Bridge.exe
set SRC=BMPHWID_Bridge.py
set PY=py -3

echo.
echo ============================================================
echo   BeamMP  HWID  Bridge  Launcher
echo ============================================================
echo.

if exist "%EXE%" (
    echo [OK  ] Packaged EXE found: %EXE%
    echo [START] Opening GUI (new window) ...
    start "" "%~dp0%EXE%"
    goto :END_OK
)

REM EXE missing - fall back to source
echo [WARN] EXE not found at %EXE%
echo [INFO ] Try to launch from Python source: %PY% %SRC%
echo.

%PY% -c "import tkinter,sys;sys.exit(0)" >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python 3 with tkinter not available.
    echo         Install Python 3.8+ with tcl/tk, or rebuild EXE with:
    echo         %PY% -m PyInstaller --onefile --windowed --name BMPHWID_Bridge %SRC%
    goto :END_ERR
)

if not exist "%SRC%" (
    echo [ERROR] Source %SRC% missing. Nothing to launch.
    goto :END_ERR
)

start "" %PY% "%~dp0%SRC%"
goto :END_OK

:END_OK
echo.
echo [DONE ] Bridge is now running.
echo        Tab3 - set API URL = http://ServerIP:12124
echo        click [Test Connect] -^> green dot = OK
goto :END

:END_ERR
echo.
echo [FAIL ] Launch failed. See messages above.

:END
echo.
timeout /t 4 >nul
endlocal
