@echo off
rem ---------------------------------------------------------------------------
rem Launcher agen push ZKTeco. Dipakai oleh task Task Scheduler "ZkPushAgent"
rem (akun SYSTEM) dan boleh juga dijalankan manual dari command prompt.
rem
rem Gaya file ini mengikuti aturan repo: murni ASCII tanpa BOM, tanpa tanda
rem kurung di dalam teks echo, dan tanpa && (cmd tidak punya itu).
rem ---------------------------------------------------------------------------
setlocal
set "APPDIR=%~dp0"

if defined ZK_AGENT_LOGDIR goto :logdir
if not defined ProgramData goto :localdir
set "ZK_AGENT_LOGDIR=%ProgramData%\ZkPushAgent"
goto :logdir
:localdir
set "ZK_AGENT_LOGDIR=%APPDIR%logs"
:logdir
if not exist "%ZK_AGENT_LOGDIR%" mkdir "%ZK_AGENT_LOGDIR%" >nul 2>&1

set "LOG=%ZK_AGENT_LOGDIR%\agent.log"

rem Putar log kalau sudah lebih dari 5 MB supaya tidak tumbuh tanpa batas.
if not exist "%LOG%" goto :run
for %%F in ("%LOG%") do if %%~zF GEQ 5242880 move /y "%LOG%" "%LOG%.1" >nul 2>&1

:run
echo [%DATE% %TIME%] mulai agen dari "%APPDIR%" >> "%LOG%"
"%APPDIR%python\python.exe" "%APPDIR%zk_push_agent.py" >> "%LOG%" 2>&1
set "RC=%ERRORLEVEL%"
echo [%DATE% %TIME%] agen berhenti, exit=%RC% >> "%LOG%"

rem 2 = ZK_PUSH_AGENT_TOKEN belum diset, 3 = DLL gagal dimuat. Task Scheduler
rem akan mengulang otomatis; lihat agent.log kalau berkasnya terus bertambah.
exit /b %RC%
