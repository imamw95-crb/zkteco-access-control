<#
.SYNOPSIS
    Daftarkan task Task Scheduler "ZkPushAgent" untuk menjalankan agen push.

.DESCRIPTION
    Dipanggil oleh installer (ZkPushAgent-Setup.exe) dan boleh dipanggil ulang
    manual untuk memperbaiki / memperbarui pendaftaran task.

    Kenapa Task Scheduler dan bukan layanan Windows: tidak perlu biner pihak
    ketiga, dan task berjalan sebagai SYSTEM tanpa menyimpan password akun
    (pakai -LogonType ServiceAccount).

    Penjaga penting: skrip ini MENOLAK Python 64-bit. DLL Pull SDK 32-bit tidak
    bisa dimuat proses 64-bit, dan kesalahan itu biasanya baru terlihat sebagai
    "DLL gagal dimuat" jauh di kemudian hari.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File install_task.ps1 `
        -Python "C:\Program Files (x86)\ZkPushAgent\python\python.exe"

.EXAMPLE
    # Hapus task (dipakai uninstaller)
    powershell -ExecutionPolicy Bypass -File install_task.ps1 -Unregister
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Python,

    # Launcher .cmd yang membungkus python.exe + rotasi log.
    [string]$Launcher,

    [string]$TaskName = 'ZkPushAgent',
    [int]$RestartMinutes = 1,
    [int]$RestartCount = 999,

    # Hapus task, jangan pasang.
    [switch]$Unregister
)

# Native command (schtasks/taskkill) yang gagal tidak boleh mematikan skrip
# di tengah jalan; setiap kegagalan diperiksa dan dilaporkan eksplisit.
$ErrorActionPreference = 'Continue'

$scriptDir = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
if (-not $Launcher) { $Launcher = Join-Path $scriptDir 'run_agent.cmd' }

function Test-Admin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

if ($Unregister) {
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $existing) {
        Write-Host "[install_task] task $TaskName tidak ada, tidak ada yang dihapus."
        exit 0
    }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Write-Host "[install_task] GAGAL menghapus task $TaskName."
        exit 5
    }
    Write-Host "[install_task] task $TaskName dihapus."
    exit 0
}

if (-not (Test-Admin)) {
    Write-Host "[install_task] GAGAL: butuh hak Administrator untuk mendaftarkan task SYSTEM."
    exit 1
}

if (-not (Test-Path -LiteralPath $Python)) {
    Write-Host "[install_task] GAGAL: Python tidak ditemukan di $Python"
    exit 2
}

# --- Penjaga 32-bit: satu-satunya kesalahan yang tidak bisa diakali ----------
$bitsOut = & $Python -c "import struct; print(struct.calcsize('P') * 8)" 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "[install_task] GAGAL: Python di $Python tidak bisa dijalankan."
    Write-Host $bitsOut
    exit 3
}
$bits = ("$bitsOut").Trim()
if ($bits -ne '32') {
    Write-Host "[install_task] GAGAL: Python di $Python bukan 32-bit (terbaca $bits-bit)."
    Write-Host "[install_task]         DLL Pull SDK 32-bit tidak akan bisa dimuat. Pakai Python 32-bit."
    exit 4
}

if (-not (Test-Path -LiteralPath $Launcher)) {
    Write-Host "[install_task] GAGAL: launcher tidak ditemukan di $Launcher"
    exit 6
}

try {
    $cmdExe = Join-Path $env:SystemRoot 'System32\cmd.exe'
    # cmd /c ""<launcher>"" -- tanda kutip ganda karena path bisa berisi spasi.
    $arguments = '/c ""' + $Launcher + '""'

    $action = New-ScheduledTaskAction -Execute $cmdExe -Argument $arguments `
        -WorkingDirectory (Split-Path -Parent $Launcher)

    $trigger = New-ScheduledTaskTrigger -AtStartup

    $settings = New-ScheduledTaskSettingsSet `
        -MultipleInstances IgnoreNew `
        -RestartCount $RestartCount `
        -RestartInterval (New-TimeSpan -Minutes $RestartMinutes) `
        -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable

    $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest

    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Settings $settings -Principal $principal -Force `
        -Description 'Agen push ZKTeco C3 (jalur tulis ke panel lewat plcommpro.dll 32-bit). Dipasang oleh ZkPushAgent-Setup.exe.' `
        -ErrorAction Stop | Out-Null
}
catch {
    Write-Host "[install_task] GAGAL mendaftarkan task: $($_.Exception.Message)"
    exit 7
}

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task) {
    Write-Host "[install_task] GAGAL: task terdaftar tapi tidak bisa dibaca kembali."
    exit 8
}

Write-Host "[install_task] OK  task '$TaskName' terdaftar (State=$($task.State))."
Write-Host "[install_task]     aksi   : $cmdExe $arguments"
Write-Host "[install_task]     akun   : SYSTEM, mulai saat boot, ulang otomatis tiap $RestartMinutes menit"
exit 0
