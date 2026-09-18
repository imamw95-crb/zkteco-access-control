@echo off
REM ===================================================================
REM  Tombol: hentikan backend ZKTeco C3 (beserta seluruh anak proses).
REM  Setelah ini port 8000 bebas dan supervisor auto-restart berhenti.
REM ===================================================================
setlocal
title Hentikan backend ZKTeco C3

set "SCRIPT=%~dp0zkteco-backend\deploy\serve_backend.ps1"
if not exist "%SCRIPT%" goto :tidak_ada

powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT%" -Action stop

echo.
echo Tekan tombol apa saja untuk menutup jendela ini.
pause >nul
exit /b 0

:tidak_ada
echo [x] Tidak menemukan: "%SCRIPT%"
echo.
pause
exit /b 1
