<#
.SYNOPSIS
    Bangun ZkPushAgent-Setup.exe: installer Windows untuk agen push ZKTeco.

.DESCRIPTION
    Yang dilakukan skrip ini, berurutan:
      1. menyiapkan Inno Setup 6 (boleh lewat winget dengan -InstallInnoSetup),
      2. mengunduh payload ke .cache\ : Python 3.13 embeddable 32-bit dan
         VC++ Redistributable x86,
      3. MEMBUKTIKAN payload itu sehat sebelum dikemas - Python harus 32-bit dan
         check_setup.py harus bisa memuat zk_push_agent.py dari folder agen,
      4. memeriksa sintaks semua skrip payload (Parser PowerShell + ASCII tanpa BOM),
      5. menjalankan ISCC.exe,
      6. menulis dist\BUILD-INFO.txt (versi, ukuran, SHA256) sebagai jejak.

    Contoh:
        powershell -ExecutionPolicy Bypass -File build_installer.ps1 -InstallInnoSetup
        powershell -ExecutionPolicy Bypass -File build_installer.ps1 -Refresh -Version 1.2.0

.NOTES
    Isi .cache\ dan dist\ adalah artefak build, bukan sumber. Jangan pernah
    mengompilasi zk_push_agent.iss tanpa menjalankan skrip ini lebih dulu, karena
    [Files] merujuk ke .cache\python dan .cache\vc_redist.x86.exe.

    SDK resmi ZKTeco (pl*.dll) TIDAK ikut dikemas - lisensinya tidak
    memungkinkan redistribusi. Installer meminta operator menunjuk foldernya dan
    menyalin DLL itu dari mesin operator.
#>
[CmdletBinding()]
param(
    # Versi yang tampil di Add/remove programs dan nama berkas keluaran.
    # Default: 1.0.<yyMMdd> supaya selalu naik dan mudah dilacak.
    [string]$Version = '',

    # Versi Python embeddable. Default: 3.13 terbaru yang ada di python.org.
    [string]$PythonVersion = '',

    # Path ISCC.exe kalau tidak terpasang di lokasi standar.
    [string]$InnoSetupPath = '',

    # Izinkan skrip memasang Inno Setup lewat winget (perubahan di host ini).
    [switch]$InstallInnoSetup,

    # Unduh ulang payload walau sudah ada di .cache\.
    [switch]$Refresh
)

$ErrorActionPreference = 'Continue'

$root = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
$agentRoot = (Resolve-Path (Join-Path $root '..')).Path
$cache = Join-Path $root '.cache'
$dist = Join-Path $root 'dist'
$issFile = Join-Path $root 'zk_push_agent.iss'

$payLoadScripts = @(
    (Join-Path $root 'app\run_agent.cmd'),
    (Join-Path $root 'app\install_task.ps1'),
    (Join-Path $root 'app\check_agent.ps1'),
    (Join-Path $root 'app\gen_token.ps1')
)

# Env var agen, dibersihkan saat memverifikasi payload: shell developer bisa saja
# sedang menjalankan agen manual, dan nilai itu akan membuat hasil pemeriksaan
# menyesatkan (mis. "DLL ditemukan" atau token "terisi").
$agentEnvNames = @(
    'ZK_PULLSDK_DLL', 'ZK_PUSH_AGENT_TOKEN', 'ZK_PUSH_AGENT_HOST',
    'ZK_PUSH_AGENT_PORT', 'ZK_PANEL_PASSWORD', 'ZK_AGENT_BUFFER_SIZE', 'ZK_AGENT_LOGDIR'
)

function Write-Step([string]$Text) {
    Write-Host ''
    Write-Host "=== $Text ===" -ForegroundColor Cyan
}

function Write-Ok([string]$Text) { Write-Host "  OK   $Text" -ForegroundColor Green }
function Write-Warn([string]$Text) { Write-Host "  !!!  $Text" -ForegroundColor Yellow }

function Stop-Build([string]$Text) {
    Write-Host ''
    Write-Host "GAGAL: $Text" -ForegroundColor Red
    exit 1
}

function Test-AsciiNoBom([string]$Path) {
    $bytes = [System.IO.File]::ReadAllBytes($Path)
    if ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) {
        return $false
    }
    foreach ($b in $bytes) {
        if ($b -gt 127) { return $false }
    }
    return $true
}

function Test-PowerShellSyntax([string]$Path) {
    $tokens = $null
    $errors = $null
    [void][System.Management.Automation.Language.Parser]::ParseFile($Path, [ref]$tokens, [ref]$errors)
    return $errors
}

function Get-Iscc {
    $candidates = @()
    if ($InnoSetupPath) { $candidates += $InnoSetupPath }
    $onPath = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($onPath) { $candidates += $onPath.Source }
    $candidates += @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe'),
        'C:\Program Files (x86)\Inno Setup 6\ISCC.exe',
        'C:\Program Files\Inno Setup 6\ISCC.exe'
    )
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) { return $candidate }
    }
    return ''
}

function Get-LatestPython313 {
    $url = 'https://www.python.org/ftp/python/'
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri $url -TimeoutSec 30
    }
    catch {
        Stop-Build "tidak bisa membaca daftar versi Python dari $url ($($_.Exception.Message)). Kirim -PythonVersion 3.13.15 kalau unduhan manual."
    }
    $matches = [regex]::Matches($response.Content, 'href="(3\.13\.\d+)/"')
    if ($matches.Count -eq 0) {
        Stop-Build 'tidak menemukan versi 3.13.x di daftar python.org. Kirim -PythonVersion secara eksplisit.'
    }
    $versions = $matches | ForEach-Object { [version]$_.Groups[1].Value }
    return ($versions | Sort-Object -Descending | Select-Object -First 1).ToString()
}

function Get-RemoteFile([string]$Url, [string]$Destination) {
    if ((Test-Path -LiteralPath $Destination) -and (-not $Refresh)) {
        Write-Ok "sudah ada di cache: $(Split-Path -Leaf $Destination)"
        return
    }
    Write-Host "  unduh $Url"
    try {
        Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile $Destination -TimeoutSec 300
    }
    catch {
        Stop-Build "unduhan gagal: $Url ($($_.Exception.Message))"
    }
    $size = [math]::Round((Get-Item -LiteralPath $Destination).Length / 1MB, 1)
    Write-Ok "terunduh $(Split-Path -Leaf $Destination) ($size MB)"
}

# ---------------------------------------------------------------- 1. Inno Setup
Write-Step 'Inno Setup 6'
$iscc = Get-Iscc
if ((-not $iscc) -and $InstallInnoSetup) {
    Write-Host '  memasang Inno Setup lewat winget...'
    & winget install --id JRSoftware.InnoSetup --exact --silent `
        --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) {
        Write-Warn "winget keluar dengan kode $LASTEXITCODE"
    }
    $iscc = Get-Iscc
}
if (-not $iscc) {
    Stop-Build @'
ISCC.exe (Inno Setup 6) tidak ditemukan. Pilih salah satu:
  winget install --id JRSoftware.InnoSetup --exact
  atau unduh https://jrsoftware.org/isdl.php lalu jalankan skrip ini dengan
  -InnoSetupPath "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
'@
}
Write-Ok "ISCC: $iscc"

# ------------------------------------------------------------------ 2. Payload
Write-Step 'Payload (.cache)'
New-Item -ItemType Directory -Path $cache -Force | Out-Null

if (-not $PythonVersion) {
    $PythonVersion = Get-LatestPython313
}
Write-Host "  Python embeddable: $PythonVersion (win32)"

$pyZip = Join-Path $cache "python-$PythonVersion-embed-win32.zip"
$pyDir = Join-Path $cache 'python'
Get-RemoteFile "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-embed-win32.zip" $pyZip

if ($Refresh -and (Test-Path -LiteralPath $pyDir)) {
    Remove-Item -LiteralPath $pyDir -Recurse -Force
}
if (-not (Test-Path -LiteralPath (Join-Path $pyDir 'python.exe'))) {
    Write-Host '  ekstrak arsip Python...'
    if (Test-Path -LiteralPath $pyDir) { Remove-Item -LiteralPath $pyDir -Recurse -Force }
    Expand-Archive -LiteralPath $pyZip -DestinationPath $pyDir -Force
}
$pyExe = Join-Path $pyDir 'python.exe'
if (-not (Test-Path -LiteralPath $pyExe)) { Stop-Build "python.exe tidak ada di $pyDir" }
Write-Ok "python.exe di $pyDir"

# Interpreter embeddable TIDAK menambahkan folder skrip ke sys.path - berkas
# python313._pth menggantikan sys.path sepenuhnya. check_setup.py mengimpor
# zk_push_agent dari folder aplikasi ({app}, induk dari python\), jadi ".." harus
# ada di ._pth. Tanpa baris ini check_setup.py mati dengan ModuleNotFoundError.
$pthFile = Get-ChildItem -LiteralPath $pyDir -Filter '*._pth' | Select-Object -First 1
if (-not $pthFile) { Stop-Build "berkas ._pth tidak ada di $pyDir - payload bukan distribusi embeddable?" }
$pthLines = @(Get-Content -LiteralPath $pthFile.FullName)
if ($pthLines -notcontains '..') {
    Add-Content -LiteralPath $pthFile.FullName -Value '..' -Encoding Ascii
    Write-Ok "menambahkan '..' ke $($pthFile.Name) (folder agen jadi importable)"
}
else {
    Write-Ok "$($pthFile.Name) sudah memuat '..'"
}

$vcRedist = Join-Path $cache 'vc_redist.x86.exe'
Get-RemoteFile 'https://aka.ms/vs/17/release/vc_redist.x86.exe' $vcRedist

# ------------------------------------------------------- 3. Buktikan payload
Write-Step 'Verifikasi payload'
$bitsOut = (& $pyExe -c "import sys, struct; print(sys.version.split()[0], struct.calcsize('P') * 8)" 2>&1) -join ' '
Write-Host "  $pyExe -> $bitsOut"
if ($bitsOut -notmatch '32') {
    Stop-Build "payload Python bukan 32-bit: $bitsOut (DLL Pull SDK 32-bit tidak akan bisa dimuat)"
}
Write-Ok 'Python payload 32-bit'

# check_setup.py mengimpor zk_push_agent dari folder yang sama, jadi uji ini
# harus memakai tata letak seperti hasil pemasangan: skrip agen di induk folder
# python\. Menjalankannya langsung dari folder repo justru salah - di sana ".."
# menunjuk ke tempat lain dan impornya gagal walau payloadnya sehat.
$pyParent = Split-Path -Parent $pyDir
if ((Split-Path -Leaf $pyParent) -ne '.cache') {
    Stop-Build "tata letak cache tidak seperti yang diharapkan: $pyParent"
}

$pathOut = (& $pyExe -c "import sys; print('|'.join(sys.path))" 2>&1) -join ''
if ($pathOut -notlike "*$pyParent*") {
    Stop-Build "interpreter payload tidak memuat folder induk ($pyParent) di sys.path - cek isi ._pth"
}
Write-Ok "sys.path payload memuat folder induk (tempat skrip agen akan berada)"

$staged = @()
foreach ($name in @('zk_push_agent.py', 'check_setup.py')) {
    $target = Join-Path $pyParent $name
    Copy-Item -LiteralPath (Join-Path $agentRoot $name) -Destination $target -Force
    $staged += $target
}

$setupOut = ''
$savedEnv = @{}
foreach ($name in $agentEnvNames) {
    $savedEnv[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
    Remove-Item "Env:$name" -ErrorAction SilentlyContinue
}
try {
    $setupOut = (& $pyExe (Join-Path $pyParent 'check_setup.py') 2>&1) -join "`n"
}
finally {
    foreach ($name in $agentEnvNames) {
        if ($savedEnv[$name]) { Set-Item "Env:$name" $savedEnv[$name] }
    }
}
foreach ($file in $staged) { Remove-Item -LiteralPath $file -Force -ErrorAction SilentlyContinue }

if ($setupOut -match 'Traceback') {
    Write-Host $setupOut
    Stop-Build 'check_setup.py gagal dengan Python payload (impor zk_push_agent?)'
}
if ($setupOut -notmatch '=== Konfigurasi agen ===') {
    Write-Host $setupOut
    Stop-Build 'check_setup.py berhenti di tengah jalan dengan Python payload'
}
if ($setupOut -notmatch '32-bit' -or $setupOut -notmatch 'OK\s+Python 3\.13') {
    Write-Host $setupOut
    Stop-Build 'check_setup.py tidak melaporkan Python 3.13 32-bit dari payload'
}
# Kalau baris ini tidak muncul, environment tidak benar-benar bersih dan hasil
# pemeriksaan bisa menyesatkan (mis. "DLL ditemukan" dari env milik agen manual).
if ($setupOut -notmatch 'ZK_PUSH_AGENT_TOKEN BELUM diisi') {
    Write-Host $setupOut
    Stop-Build 'environment pemeriksaan tidak bersih (ZK_* masih ter-set)'
}
Write-Ok 'check_setup.py jalan dengan interpreter payload, environment bersih'
Write-Host '  --- 20 baris pertama keluaran check_setup.py ---'
($setupOut -split "`n" | Select-Object -First 20) | ForEach-Object { Write-Host "  $_" }

# ------------------------------------------------------- 4. Skrip payload sehat
Write-Step 'Skrip payload (ASCII, tanpa BOM, sintaks)'
foreach ($script in $payLoadScripts) {
    if (-not (Test-Path -LiteralPath $script)) { Stop-Build "berkas payload hilang: $script" }
    if (-not (Test-AsciiNoBom $script)) {
        Stop-Build "$script mengandung karakter non-ASCII atau BOM (cmd.exe & PowerShell 5.1 akan salah membacanya)"
    }
    if ($script -like '*.ps1') {
        $errors = Test-PowerShellSyntax $script
        if ($errors -and $errors.Count -gt 0) {
            foreach ($e in $errors) { Write-Host "  $e" -ForegroundColor Red }
            Stop-Build "sintaks PowerShell salah di $script"
        }
    }
    Write-Ok "$(Split-Path -Leaf $script)"
}

# --------------------------------------------------------------- 5. Kompilasi
Write-Step 'Kompilasi installer'
if (-not $Version) {
    $Version = '1.0.' + (Get-Date -Format 'yyMMdd')
}

# VersionInfoVersion harus x.x.x.x dengan setiap bagian 0-65535, jadi AppVer
# berformat tanggal (1.0.260918) TIDAK bisa dipakai apa adanya. Turunkan nomor
# build terpisah: hari sejak 2020 (masih < 65535 sampai 2199) + menit.
$verNum = ''
if ($Version -match '^\d+(\.\d+){3}$') {
    $parts = $Version -split '\.'
    if (-not ($parts | Where-Object { [int]$_ -gt 65535 })) { $verNum = $Version }
}
if (-not $verNum) {
    $days = [int]((Get-Date).Date - [datetime]'2020-01-01').TotalDays
    $minutes = (Get-Date).Hour * 60 + (Get-Date).Minute
    $verNum = "1.0.$days.$minutes"
}

New-Item -ItemType Directory -Path $dist -Force | Out-Null
Write-Host "  versi: $Version (VersionInfoVersion $verNum)"

& $iscc "/DAppVer=$Version" "/DVerNum=$verNum" "/O$dist" $issFile
if ($LASTEXITCODE -ne 0) {
    Stop-Build "ISCC gagal dengan kode $LASTEXITCODE"
}

$setupExe = Join-Path $dist 'ZkPushAgent-Setup.exe'
if (-not (Test-Path -LiteralPath $setupExe)) { Stop-Build "ISCC selesai tapi $setupExe tidak ada" }

$versionedExe = Join-Path $dist "ZkPushAgent-Setup-$Version.exe"
Copy-Item -LiteralPath $setupExe -Destination $versionedExe -Force

$size = [math]::Round((Get-Item -LiteralPath $setupExe).Length / 1MB, 1)
$hash = (Get-FileHash -LiteralPath $setupExe -Algorithm SHA256).Hash

$info = @(
    "ZkPushAgent-Setup.exe"
    "dibangun   : $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
    "versi      : $Version (VersionInfoVersion $verNum)"
    "python     : $PythonVersion embeddable win32"
    "vc redist  : $vcRedist"
    "ukuran     : $size MB"
    "sha256     : $hash"
    ""
    "Pemasangan biasa  : jalankan ZkPushAgent-Setup.exe (butuh Administrator)"
    "Pemasangan senyap : ZkPushAgent-Setup.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /TOKEN=<token> /SDK=`"C:\PullSDK`" /HOST=0.0.0.0 /PORT=8081"
    "                    (parameter lain: /PANELIP=<ip panel uji> /PANELPWD=<password> /BUFFER=65536)"
)
Set-Content -LiteralPath (Join-Path $dist 'BUILD-INFO.txt') -Value $info -Encoding Ascii

Write-Step 'Selesai'
Write-Ok "$versionedExe ($size MB)"
Write-Host "  sha256: $hash"
Write-Host ''
Write-Host 'Pemasangan senyap untuk banyak PC:' -ForegroundColor Yellow
Write-Host "  \\share\ZkPushAgent-Setup.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /TOKEN=<token> /SDK=`"C:\PullSDK`" /HOST=0.0.0.0"
Write-Host ''
Write-Host 'Setelah pemasangan, tempel ke environment server backend lalu restart backend:' -ForegroundColor Yellow
Write-Host '  PUSH_AGENT_URL=http://<nama-pc>:8081'
Write-Host '  PUSH_AGENT_TOKEN=<token yang sama>'
exit 0
