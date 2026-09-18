@echo off
REM ===================================================================
REM  Tombol: nyalakan backend ZKTeco C3 (http://localhost:8000).
REM  Backend berjalan sebagai proses LATAR, jadi tetap hidup setelah
REM  jendela ini ditutup, dan otomatis dinyalakan ulang kalau crash.
REM  Hentikan dengan: hentikan-backend.bat
REM ===================================================================
setlocal
title Jalankan backend ZKTeco C3

set "SCRIPT=%~dp0zkteco-backend\deploy\serve_backend.ps1"
if not exist "%SCRIPT%" goto :tidak_ada

powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT%" -Action start

echo.
echo Tekan tombol apa saja untuk menutup jendela ini.
echo Backend tetap jalan di latar belakang.
pause >nul
exit /b 0

:tidak_ada
echo [x] Tidak menemukan: "%SCRIPT%"
echo     Taruh file .bat ini di folder root workspace, sejajar dengan AGENTS.md
echo.
pause
exit /b 1
