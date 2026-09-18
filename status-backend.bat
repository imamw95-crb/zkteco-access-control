@echo off
REM ===================================================================
REM  Tombol: cek apakah backend hidup, plus ekor log terakhir.
REM ===================================================================
setlocal
title Status backend ZKTeco C3

set "SCRIPT=%~dp0zkteco-backend\deploy\serve_backend.ps1"
if not exist "%SCRIPT%" goto :tidak_ada

powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT%" -Action status
set "RC=%ERRORLEVEL%"

echo.
if "%RC%"=="0" goto :sehat
echo Backend tidak sehat. Jalankan: jalankan-backend.bat
goto :selesai

:sehat
echo Backend sehat. Buka: http://localhost:8000/

:selesai
echo.
echo Tekan tombol apa saja untuk menutup jendela ini.
pause >nul
exit /b 0

:tidak_ada
echo [x] Tidak menemukan: "%SCRIPT%"
echo.
pause
exit /b 1
