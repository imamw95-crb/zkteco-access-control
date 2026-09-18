<#
.SYNOPSIS
    Uji installer TANPA hak Administrator: kompilasi salinan .iss yang ditambal,
    pasang senyap, periksa, lalu uninstall.

.DESCRIPTION
    Ini yang menangkap kesalahan di blok [Code] Inno sebelum installer sampai ke
    PC operator. Contoh nyata yang pernah lolos ke produksi: FindSdkFolder()
    memanggil ExpandConstant('{app}') selama InitializeWizard - installer mati
    dengan "attempt to expand the app constant before it was initialized" sebelum
    halaman pertama muncul. Tanpa uji ini, satu-satunya cara menemukannya adalah
    menjalankannya di PC orang lain.

    Salinan .iss yang diuji ditambal hanya pada tiga hal:
      1. PrivilegesRequired=admin  -> lowest      (supaya bisa jalan tanpa UAC)
      2. if NeedVcRedist() then    -> if False    (vc_redist minta elevasi sendiri)
      3. OutputBaseFilename        -> ZkPushAgent-Smoke (jangan menimpa dist produk)

    Akibatnya bagian yang butuh admin (task Task Scheduler, env mesin, firewall)
    dilaporkan sebagai masalah di CHECK-REPORT.txt - itu memang diharapkan; yang
    diuji di sini adalah seluruh kode Pascal, penyalinan berkas, token, launcher,
    dan jalur HTTP /health.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File smoke_test.ps1
    powershell -ExecutionPolicy Bypass -File smoke_test.ps1 -Port 8099 -Keep
#>
[CmdletBinding()]
param(
    [int]$Port = 8099,
    [string]$Sdk = 'C:\PullSDK',
    [string]$InnoSetupPath = '',

    # Jangan hapus folder kerja (untuk memeriksa CHECK-REPORT.txt / setup.log).
    [switch]$Keep
)

$ErrorActionPreference = 'Continue'

$root = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
$sourceIss = Join-Path $root 'zk_push_agent.iss'
$smokeIss = Join-Path $root 'zk_push_agent.smoke.iss'
$cache = Join-Path $root '.cache'

$failures = New-Object System.Collections.Generic.List[string]

function Step([string]$Text) {
    Write-Host ''
    Write-Host "=== $Text ===" -ForegroundColor Cyan
}

function Check([string]$Text, [bool]$Ok) {
    if ($Ok) {
        Write-Host "  OK   $Text" -ForegroundColor Green
    }
    else {
        Write-Host "  X    $Text" -ForegroundColor Red
        $failures.Add($Text)
    }
}

function Stop-Smoke([string]$Text) {
    Write-Host ''
    Write-Host "GAGAL: $Text" -ForegroundColor Red
    Remove-Item -LiteralPath $smokeIss -Force -ErrorAction SilentlyContinue
    exit 1
}

function Need([bool]$Ok, [string]$Text) {
    if (-not $Ok) { Stop-Smoke $Text }
}

# ------------------------------------------------------------------ ruang kerja
$work = Join-Path $env:TEMP ('zkagent-smoke-' + (Get-Date -Format 'HHmmss'))
$dist = Join-Path $work 'dist'
$installDir = Join-Path $work 'app'
$env:ZK_AGENT_LOGDIR = Join-Path $work 'logs'

# Shell developer bisa sedang menjalankan agen manual. `setx` menulis level User,
# dan level User MENANG atas level Machine - kalau dibiarkan, agen uji akan start
# dengan port/DLL/token milik setup lama dan pemeriksaan gagal tanpa sebab nyata.
# (check_agent.ps1 juga memaksa nilai efektif, ini lapisan keduanya.)
foreach ($name in @('ZK_PULLSDK_DLL', 'ZK_PUSH_AGENT_TOKEN', 'ZK_PUSH_AGENT_HOST',
        'ZK_PUSH_AGENT_PORT', 'ZK_PANEL_PASSWORD', 'ZK_AGENT_BUFFER_SIZE')) {
    Remove-Item "Env:$name" -ErrorAction SilentlyContinue
}

Step 'Persiapan'
Need (Test-Path -LiteralPath $sourceIss) "$sourceIss tidak ada"
Need (Test-Path -LiteralPath (Join-Path $Sdk 'plcommpro.dll')) "SDK tidak ditemukan di $Sdk (kirim -Sdk <folder>)"

$busy = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
Need ($busy.Count -eq 0) "port $Port sedang dipakai; kirim -Port <port lain>"

$iscc = ''
foreach ($candidate in @($InnoSetupPath, (Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe'),
        'C:\Program Files (x86)\Inno Setup 6\ISCC.exe', 'C:\Program Files\Inno Setup 6\ISCC.exe')) {
    if ($candidate -and (Test-Path -LiteralPath $candidate)) { $iscc = $candidate; break }
}
Need ($iscc -ne '') 'ISCC.exe tidak ditemukan (jalankan build_installer.ps1 -InstallInnoSetup)'

Write-Host "  folder kerja: $work"
Write-Host "  port uji    : $Port"
Write-Host "  SDK         : $Sdk"

# ------------------------------------------------------------------- kompilasi
Step 'Kompilasi salinan uji'
$text = Get-Content -LiteralPath $sourceIss -Raw
$patched = $text.Replace('PrivilegesRequired=admin', 'PrivilegesRequired=lowest')
Need ($patched -ne $text) 'tambalan PrivilegesRequired gagal (teks sumber berubah?)'
$patched2 = $patched.Replace('if NeedVcRedist() then', 'if False then')
Need ($patched2 -ne $patched) 'tambalan NeedVcRedist gagal (teks sumber berubah?)'
$patched3 = $patched2.Replace('OutputBaseFilename=ZkPushAgent-Setup', 'OutputBaseFilename=ZkPushAgent-Smoke')
Need ($patched3 -ne $patched2) 'tambalan OutputBaseFilename gagal (teks sumber berubah?)'
Set-Content -LiteralPath $smokeIss -Value $patched3 -Encoding Ascii

New-Item -ItemType Directory -Path $dist -Force | Out-Null
& $iscc "/DAppVer=0.0.0-smoke" '/DVerNum=0.0.0.1' "/O$dist" $smokeIss | Out-Null
$setup = Join-Path $dist 'ZkPushAgent-Smoke.exe'
Need (Test-Path -LiteralPath $setup) 'kompilasi gagal (ISCC keluar tanpa menghasilkan ZkPushAgent-Smoke.exe)'
Write-Host "  $setup"

# --------------------------------------------------------------------- pasang
Step 'Pemasangan senyap (tanpa admin)'
$token = 'smoketoken0123456789abcdefghijkl'
$log = Join-Path $work 'setup.log'

# Satu string command line, bukan array: Start-Process -ArgumentList berupa array
# menambahkan spasi di dalam elemen ber-quote (jebakan yang sudah memakan waktu di
# repo ini) sehingga /DIR bisa sampai sebagai " C:\... ".
$cli = '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /DIR="' + $installDir + '" /SDK="' +
    $Sdk + '" /HOST=127.0.0.1 /PORT=' + $Port + ' /TOKEN=' + $token + ' /LOG="' + $log + '"'
$proc = Start-Process -FilePath $setup -ArgumentList $cli -PassThru

# Batas waktu: installer yang menggantung (mis. menunggu dialog saat senyap)
# harus menjadi kegagalan yang terlihat, bukan terminal yang blokir selamanya.
$deadline = (Get-Date).AddSeconds(300)
while (((Get-Date) -lt $deadline) -and (-not $proc.HasExited)) { Start-Sleep -Seconds 2 }
if (-not $proc.HasExited) {
    Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
    Get-CimInstance Win32_Process -Filter "Name = 'ZkPushAgent-Smoke.exe' OR Name = 'ZkPushAgent-Smoke.tmp'" |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Stop-Smoke 'installer tidak keluar dalam 300 detik (menggantung, mungkin menunggu dialog?)'
}
$setupCode = $proc.ExitCode
Check "installer keluar dengan kode 0 (dapat $setupCode)" ($setupCode -eq 0)
if ($setupCode -ne 0) {
    Write-Host "  --- 30 baris terakhir setup.log ---"
    Get-Content -LiteralPath $log -ErrorAction SilentlyContinue | Select-Object -Last 30 |
        ForEach-Object { Write-Host "  $_" }
    Stop-Smoke 'pemasangan gagal - pesan runtime error Inno ada di setup.log di atas'
}

# --------------------------------------------------------------- hasil pasang
Step 'Hasil pemasangan'
foreach ($name in @('zk_push_agent.py', 'check_setup.py', 'run_agent.cmd', 'install_task.ps1',
        'check_agent.ps1', 'gen_token.ps1', 'CHECK-REPORT.txt', 'BACKEND-SNIPPET.txt',
        'python\python.exe', 'sdk\plcommpro.dll', 'unins000.exe')) {
    Check "ada: $name" (Test-Path -LiteralPath (Join-Path $installDir $name))
}

Step 'Isi laporan pemeriksaan'
$report = Get-Content -LiteralPath (Join-Path $installDir 'CHECK-REPORT.txt') -Raw -ErrorAction SilentlyContinue
Need ($report -ne $null) 'CHECK-REPORT.txt tidak ada'
Check 'laporan tidak memuat Traceback' ($report -notmatch 'Traceback')
Check 'Python payload 32-bit dilaporkan' ($report -match 'OK\s+Python 32-bit')
Check 'GET /health dijawab agen' ($report -match 'OK\s+GET /health')
Check 'health melaporkan python_bits 32' ($report -match '"python_bits": 32')
Check 'task belum terdaftar (diharapkan tanpa admin)' ($report -match 'task belum terdaftar')
Check 'laporan berakhir dengan verdict' ($report -match 'VERDICT:')

$snippet = Get-Content -LiteralPath (Join-Path $installDir 'BACKEND-SNIPPET.txt') -Raw -ErrorAction SilentlyContinue
Check 'BACKEND-SNIPPET memuat URL dengan port uji' ($snippet -match "PUSH_AGENT_URL=http://[^:]+:$Port")
Check 'BACKEND-SNIPPET memuat token' ($snippet -match [regex]::Escape($token))

Step 'Sisa proses'
$leftover = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
Check 'tidak ada proses tertinggal di port uji' ($leftover.Count -eq 0)

# ------------------------------------------------------------------ uninstall
Step 'Uninstall senyap'
Start-Process -FilePath (Join-Path $installDir 'unins000.exe') `
    -ArgumentList '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART' | Out-Null
$gone = $false
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 1
    if (-not (Test-Path -LiteralPath (Join-Path $installDir 'zk_push_agent.py'))) { $gone = $true; break }
}
Check 'berkas aplikasi terhapus oleh uninstaller' $gone
Check 'folder sdk terhapus' (-not (Test-Path -LiteralPath (Join-Path $installDir 'sdk')))

Remove-Item -LiteralPath $smokeIss -Force -ErrorAction SilentlyContinue

Step 'Hasil'
if ($failures.Count -eq 0) {
    Write-Host '  SEMUA OK - installer lulus uji tanpa admin.' -ForegroundColor Green
    if (-not $Keep) { Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue }
    else { Write-Host "  folder kerja disimpan: $work" }
    exit 0
}

Write-Host "  $($failures.Count) pemeriksaan gagal:" -ForegroundColor Red
foreach ($failure in $failures) { Write-Host "    - $failure" -ForegroundColor Red }
Write-Host "  folder kerja disimpan untuk diagnosis: $work"
exit 1
