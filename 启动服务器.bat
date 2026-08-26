@echo off
REM ===== Console code page: 65001 = UTF-8 =====
REM   BeamMP Lua prints raw UTF-8 bytes (Chinese strings in main.lua are UTF-8).
REM   With chcp 936 (GBK) console decodes those bytes incorrectly -> mojibake.
REM   With chcp 65001 console shows perfect Chinese directly.
REM   Server.log is also UTF-8 on disk, so always read it with UTF-8 decoding.
chcp 65001 >nul

cd /d "%~dp0"

REM =============================================================
REM STEP 1 - Kill any stale BeamMP-Server.exe process (ASCII only!)
REM   This prevents:
REM     [ERROR] bind() failed: ...port only allowed once... (port 12123 conflict)
REM     [WARN]  rename: file Server.log used by another process (log lock from previous instance)
REM   MUST stay ASCII - cmd breaks Chinese in powershell -Command args.
REM =============================================================
echo [Pre] Checking for stale BeamMP-Server processes...
powershell -NoProfile -Command "$pr=Get-Process BeamMP-Server -ErrorAction SilentlyContinue; if($pr){ $pr|%%{ Write-Host ('[Pre] Killing stale BeamMP-Server PID='+$_.Id) }; $pr|Stop-Process -Force; Start-Sleep -Milliseconds 600 } else { Write-Host '[Pre] No stale BeamMP-Server running - OK' }"

REM =============================================================
REM STEP 2 - Strip UTF-8 BOM from main.lua (ASCII only!)
REM   BeamMP Lua parser rejects BOM: LUA ERROR L1: unexpected symbol near '<239>'
REM   IDEs (e.g. VS Code on Chinese Windows) often re-add BOM on save.
REM =============================================================
powershell -NoProfile -Command "$f='Resources\Server\BMPLogin\main.lua'; $b=[IO.File]::ReadAllBytes($f); if($b.Length -ge 3 -and $b[0] -eq 239 -and $b[1] -eq 187 -and $b[2] -eq 191){ [IO.File]::WriteAllBytes($f, $b[3..($b.Length-1)]); Write-Host '[Pre] Stripped UTF-8 BOM from main.lua - OK' } else { Write-Host '[Pre] main.lua has no BOM - OK' }"

echo.
echo Starting BeamMP Server...
echo.
BeamMP-Server.exe
pause
