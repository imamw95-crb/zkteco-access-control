<#
.SYNOPSIS
    Kirim kode terbaru ke server deploy, lalu deploy ulang di sana.

.DESCRIPTION
    Dipakai untuk "deploy ulang setelah ada update fitur":

      1. bungkus kode backend jadi satu tar.gz -- TANPA .env dan TANPA *.db,
      2. scp ke server,
      3. ssh: pasang kode, build ulang image, jalankan migrasi, lalu verifikasi.

    Yang TIDAK pernah tersentuh di server (tidak ikut di dalam arsip, jadi tidak
    mungkin tertimpa):
      - .env          : sandi Postgres + token agen push
      - live_check.db : sumber data mentah yang dipakai untuk penyalinan awal

    Migrasi Alembic dijalankan otomatis oleh container `backend` saat start
    (`alembic upgrade head`), jadi update yang mengubah skema tidak butuh langkah
    tambahan. Data di volume `zkteco_pgdata` tidak pernah dihapus oleh skrip ini.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\deploy\push_to_server.ps1
    # hanya kirim kodenya, jangan nyalakan ulang apa pun:
    powershell -ExecutionPolicy Bypass -File .\deploy\push_to_server.ps1 -UploadOnly
    # timpa isi database server dengan live_check.db (hati-hati):
    powershell -ExecutionPolicy Bypass -File .\deploy\push_to_server.ps1 -ForceCopyData
#>
[CmdletBinding()]
param(
    [string]$Target = 'zkteco-prod',
    [string]$RemoteDir = '/home/sentral/zkteco-backend',
    [switch]$UploadOnly,
    [switch]$ForceCopyData
)

$ErrorActionPreference = 'Stop'

$Backend = Split-Path -Parent $PSScriptRoot
$Workspace = Split-Path -Parent $Backend
$Archive = Join-Path $env:TEMP 'zkteco-src.tar.gz'

# scp menulis bilah kemajuan ke stderr. Dengan ErrorActionPreference=Stop,
# PowerShell 5.1 menganggap tulisan stderr itu error yang mematikan skrip di
# tengah jalan, jadi proses native dijalankan dengan 'Continue' dan hasilnya
# diperiksa lewat $LASTEXITCODE.
function Invoke-Native {
    param([string]$Exe, [string[]]$Arguments)
    $before = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $Exe @Arguments
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $before
    }
    if ($code -ne 0) {
        throw "$Exe keluar dengan kode $code ($($Arguments -join ' '))"
    }
}

if (Test-Path $Archive) { Remove-Item $Archive -Force }

Write-Host "1/3  Mengumpulkan kode dari $Backend" -ForegroundColor Cyan
Invoke-Native 'tar' @(
    '-czf', $Archive,
    '-C', $Backend,
    '--exclude=__pycache__', '--exclude=.env', '--exclude=*.db', '--exclude=*.tar.gz',
    'app', 'migrations', 'scripts',
    'alembic.ini', 'pyproject.toml', 'requirements.txt',
    'Dockerfile', 'docker-compose.yml', '.env.example',
    # Skrip server harus ikut, kalau tidak versi lama di server yang dipakai.
    # Tambahkan skrip server baru ke daftar ini.
    'deploy/server_deploy.sh', 'deploy/server_setup_env.sh', 'deploy/server_status.sh',
    '-C', $Workspace, 'devices_input.csv'
)
Write-Host ("     {0} byte" -f (Get-Item $Archive).Length)

Write-Host "2/3  Mengirim ke $Target" -ForegroundColor Cyan
Invoke-Native 'scp' @($Archive, "${Target}:/tmp/zkteco-src.tar.gz")

Write-Host "3/3  Memasang dan deploy di $RemoteDir" -ForegroundColor Cyan
# `rm -rf app migrations scripts` disengaja: supaya server jadi salinan setia
# pohon sumber. Kalau sebuah modul dihapus di sini, image tidak menyimpan sisa
# lamanya. Hanya tiga direktori kode itu yang disentuh; .env, live_check.db dan
# deploy/ tetap apa adanya.
$remote = 'set -e; cd ' + $RemoteDir + '; rm -rf app migrations scripts; tar -xzf /tmp/zkteco-src.tar.gz; chmod +x deploy/*.sh'
if ($UploadOnly) {
    $remote += '; echo KODE_TERPASANG (tanpa deploy ulang)'
} else {
    $remote += '; bash deploy/server_deploy.sh'
    if ($ForceCopyData) { $remote += ' --force-copy' }
}

Invoke-Native 'ssh' @($Target, $remote)

Write-Host ""
Write-Host "Selesai. Dashboard: http://192.168.0.27:8000/" -ForegroundColor Green
