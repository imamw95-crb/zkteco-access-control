<#
.SYNOPSIS
    Periksa kesehatan agen push ZKTeco: DLL, Python 32-bit, task, dan /health.

.DESCRIPTION
    Dipanggil installer di akhir pemasangan (menulis laporan ke berkas supaya
    bisa ditampilkan/dibuka), dan boleh dijalankan kapan saja oleh operator:

        powershell -ExecutionPolicy Bypass -File check_agent.ps1
        powershell -ExecutionPolicy Bypass -File check_agent.ps1 -PanelIp 10.100.1.12 -Restart

    Tanpa -Restart task yang sedang berjalan TIDAK diganggu (aman dijalankan
    saat push berlangsung). Installer memakai -Restart supaya proses agen
    membaca environment variable yang baru.

    Seluruh keluaran ASCII: laporan dibaca ulang oleh installer dan dibuka di
    Notepad, jadi jangan pakai centang/panah atau karakter non-ASCII lain.
#>
[CmdletBinding()]
param(
    [string]$Python,
    [string]$AgentDir,
    [string]$Dll,
    [string]$Token,
    [string]$AgentHost,
    [string]$Port,
    [string]$PanelIp,
    [string]$TaskName = 'ZkPushAgent',
    [string]$Report,
    [int]$WaitSeconds = 20,
    [switch]$Restart
)

$ErrorActionPreference = 'Continue'

$scriptDir = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
if (-not $AgentDir) { $AgentDir = $scriptDir }
if (-not $Python) { $Python = Join-Path $AgentDir 'python\python.exe' }

# Environment variable yang di-set installer jadi nilai default di sini, supaya
# skrip tetap berguna kalau dijalankan manual dari shell baru.
function Get-AgentEnv([string]$Name) {
    return [Environment]::GetEnvironmentVariable($Name, 'Machine')
}

if (-not $Dll) { $Dll = Get-AgentEnv 'ZK_PULLSDK_DLL' }
if (-not $Dll) { $Dll = Join-Path $AgentDir 'sdk\plcommpro.dll' }
if (-not $Token) { $Token = Get-AgentEnv 'ZK_PUSH_AGENT_TOKEN' }
if (-not $AgentHost) { $AgentHost = Get-AgentEnv 'ZK_PUSH_AGENT_HOST' }
if (-not $AgentHost) { $AgentHost = '127.0.0.1' }
if (-not $Port) { $Port = Get-AgentEnv 'ZK_PUSH_AGENT_PORT' }
if (-not $Port) { $Port = '8081' }

# Nilai efektif DIPAKSA ke environment proses ini sebelum apa pun dijalankan:
# anak proses (check_setup.py, run_agent.cmd, python agen) mewarisinya. Kalau tidak,
# mereka membaca ZK_* milik shell yang menjalankan installer - dan level User MENANG
# atas level Machine. Terbukti: uji pemasangan pernah menjalankan agen di port 8081
# dengan DLL C:\PullSDK padahal installer memasang port 8099 dan DLL di folder
# aplikasi, lalu laporannya menyalahkan payload yang sebenarnya sehat.
$env:ZK_PULLSDK_DLL = $Dll
$env:ZK_PUSH_AGENT_TOKEN = $Token
$env:ZK_PUSH_AGENT_HOST = $AgentHost
$env:ZK_PUSH_AGENT_PORT = $Port

# 0.0.0.0 bukan alamat yang bisa dituju; dari mesin ini selalu localhost.
$probeHost = $AgentHost
if ($probeHost -eq '0.0.0.0' -or $probeHost -eq '*') { $probeHost = '127.0.0.1' }

# Nilai ZK_* yang nyangkut di level User/Machine tidak mengganggu pemeriksaan ini
# (lihat blok di atas) tapi menyesatkan operator yang menjalankan agen manual dari
# shell-nya sendiri, jadi dilaporkan.
$conflicts = @()
foreach ($pair in @(
        @('ZK_PUSH_AGENT_TOKEN', $Token),
        @('ZK_PULLSDK_DLL', $Dll),
        @('ZK_PUSH_AGENT_PORT', $Port),
        @('ZK_PUSH_AGENT_HOST', $AgentHost))) {
    foreach ($scope in @('User', 'Machine')) {
        $ambient = [Environment]::GetEnvironmentVariable($pair[0], $scope)
        if ($ambient -and ($ambient -ne $pair[1])) { $conflicts += "$($pair[0]) ($scope)" }
    }
}

$problems = New-Object System.Collections.Generic.List[string]

function Say([string]$Text) {
    Write-Host $Text
    if ($Report) {
        try {
            Add-Content -LiteralPath $Report -Value $Text -Encoding Ascii -ErrorAction Stop
        }
        catch {
            # Laporan gagal ditulis bukan alasan menggagalkan pemeriksaan.
        }
    }
}

if ($Report) {
    try {
        Set-Content -LiteralPath $Report -Value '' -Encoding Ascii -ErrorAction Stop
    }
    catch {
        $Report = ''
    }
}

Say ("=== Periksa agen push ZKTeco === " + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))
Say "Folder agen : $AgentDir"
Say "Python      : $Python"
Say "DLL SDK     : $Dll"
Say "Bind/port   : $AgentHost : $Port"
Say "Task        : $TaskName"
if ($conflicts.Count -gt 0) {
    Say ""
    Say "!!!  ZK_* ini usang dan MENANG di shell user (bukan di task SYSTEM):"
    Say "     $($conflicts -join ', ')"
    Say "     Pemeriksaan ini memakai konfigurasi terpasang. Untuk menjalankan agen"
    Say "     manual, pakai check_agent.ps1, atau hapus nilai usangnya:"
    Say "     [Environment]::SetEnvironmentVariable('ZK_...', `$null, 'User')"
}
Say ""

# --- 1. Python 32-bit -------------------------------------------------------
if (-not (Test-Path -LiteralPath $Python)) {
    Say "X    python.exe tidak ditemukan."
    $problems.Add('python.exe tidak ditemukan')
}
else {
    $bitsOut = & $Python -c "import struct; print(struct.calcsize('P') * 8)" 2>&1
    $bits = ("$bitsOut").Trim()
    if ($bits -eq '32') {
        Say "OK   Python 32-bit"
    }
    else {
        Say "X    Python terbaca $bits-bit, seharusnya 32-bit."
        $problems.Add('Python bukan 32-bit')
    }
}

# --- 2. DLL SDK -------------------------------------------------------------
if (Test-Path -LiteralPath $Dll) {
    $sdkDir = Split-Path -Parent $Dll
    $plDlls = @(Get-ChildItem -LiteralPath $sdkDir -Filter 'pl*.dll' -ErrorAction SilentlyContinue)
    Say "OK   DLL ada, $($plDlls.Count) berkas pl*.dll di $sdkDir"
    if ($plDlls.Count -lt 3) {
        Say "!!!  Jumlah pl*.dll sedikit. DLL transport (pltcpcomm.dll dll.) harus ada di"
        Say "     folder yang SAMA, kalau tidak Connect gagal dengan PullLastError=-201."
        $problems.Add('DLL transport mungkin tidak lengkap (risiko -201)')
    }
}
else {
    Say "X    DLL tidak ada di $Dll"
    Say "     Salin SEMUA pl*.dll dari SDK resmi ZKTeco ke satu folder, lalu pasang ulang."
    $problems.Add('plcommpro.dll tidak ditemukan')
}

# --- 3. check_setup.py (dan sesi sungguhan kalau PanelIp diisi) -------------
Say ""
Say "--- check_setup.py (baca-saja) ---"
if ((Test-Path -LiteralPath $Python) -and (Test-Path -LiteralPath (Join-Path $AgentDir 'check_setup.py'))) {
    $setupArgs = @((Join-Path $AgentDir 'check_setup.py'))
    if ($PanelIp) { $setupArgs += @('--test', $PanelIp) }
    $setupOut = & $Python @setupArgs 2>&1
    foreach ($line in $setupOut) { Say "$line" }
    if (-not $PanelIp) {
        Say "(tanpa -PanelIp tidak ada sesi ke panel; tambahkan -PanelIp 10.100.1.x untuk bukti nyata)"
    }
}
else {
    Say "X    check_setup.py atau python.exe tidak ditemukan di $AgentDir"
    $problems.Add('check_setup.py tidak bisa dijalankan')
}

# --- 4. Token ---------------------------------------------------------------
if ($Token) {
    Say ""
    Say "OK   ZK_PUSH_AGENT_TOKEN terisi ($($Token.Length) karakter)."
}
else {
    Say ""
    Say "X    ZK_PUSH_AGENT_TOKEN kosong. Agen MENOLAK start (exit 2)."
    $problems.Add('token belum diisi')
}

# --- 5. Task + /health ------------------------------------------------------
# Jalur HTTP dibuktikan walau task belum terdaftar: kalau task-nya hilang
# (pendaftaran butuh Administrator), launcher dijalankan langsung supaya
# laporannya tetap bisa memisahkan "payload rusak" dari "task belum dibuat".
Say ""
Say "--- Task Scheduler + /health ---"
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
$launcher = Join-Path $AgentDir 'run_agent.cmd'
$startedByUs = $false
$portBusy = @(Get-NetTCPConnection -LocalPort ([int]$Port) -State Listen -ErrorAction SilentlyContinue).Count -gt 0

if ($task) {
    Say "     task '$TaskName' terdaftar (State=$($task.State))."
    if ($Restart -and $task.State -eq 'Running') {
        Say "     menghentikan task yang sedang berjalan (agar env baru terbaca)..."
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 1
    }
    if ((Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue).State -ne 'Running') {
        Say "     memulai task..."
        Start-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    }
}
else {
    Say "!!!  task '$TaskName' belum terdaftar (pendaftaran butuh Administrator):"
    Say "     powershell -ExecutionPolicy Bypass -File `"$AgentDir\install_task.ps1`" -Python `"$Python`""
    if ($portBusy) {
        Say "     port $Port sudah dipakai proses lain - uji jalur HTTP dilewati."
    }
    elseif (Test-Path -LiteralPath $launcher) {
        Say "     menguji jalur HTTP dengan menjalankan run_agent.cmd langsung..."
        Start-Process -FilePath 'cmd.exe' -ArgumentList ('/c ""' + $launcher + '""') -WindowStyle Hidden | Out-Null
        $startedByUs = $true
    }
    else {
        Say "X    run_agent.cmd tidak ada di $AgentDir"
    }
    $problems.Add('task belum terdaftar')
}

$deadline = (Get-Date).AddSeconds($WaitSeconds)
$health = $null
$lastError = ''
do {
    try {
        $health = Invoke-WebRequest -UseBasicParsing -TimeoutSec 10 `
            -Headers @{ Authorization = "Bearer $Token" } `
            -Uri "http://${probeHost}:$Port/health" -ErrorAction Stop
        break
    }
    catch {
        $lastError = $_.Exception.Message
        Start-Sleep -Seconds 2
    }
} while ((Get-Date) -lt $deadline)

if ($task) {
    Say "     State task: $((Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue).State)"
}
if ($startedByUs) {
    # Proses yang kita luncurkan sendiri dihentikan lagi; HANYA yang memegang
    # port ini, supaya proses lain (mis. agen manual milik operator) tidak kena.
    Say "     menghentikan proses uji..."
    foreach ($owner in @(Get-NetTCPConnection -LocalPort ([int]$Port) -State Listen -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty OwningProcess -Unique)) {
        Stop-Process -Id $owner -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 1
}

if ($health) {
    Say "OK   GET /health -> $($health.Content)"
}
else {
    Say "X    /health tidak menjawab dalam $WaitSeconds detik terakhir: $lastError"
    Say "     Cek log: $(Join-Path (Join-Path $env:ProgramData 'ZkPushAgent') 'agent.log')"
    $problems.Add('endpoint /health tidak menjawab')
}

# --- 6. Verdict -------------------------------------------------------------
Say ""
if ($problems.Count -eq 0) {
    Say "VERDICT: OK - agen siap dipakai."
    Say "Langkah berikutnya: set di environment server backend, lalu restart backend:"
    Say "  PUSH_AGENT_URL=http://$([Environment]::MachineName):$Port"
    Say "  PUSH_AGENT_TOKEN=$Token"
    exit 0
}

Say "VERDICT: PERIKSA - $($problems.Count) masalah: $($problems -join '; ')"
exit 1
