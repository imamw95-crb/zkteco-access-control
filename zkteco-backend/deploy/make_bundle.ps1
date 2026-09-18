<#
.SYNOPSIS
    Build a deployment tarball of the backend, excluding dev artefacts.

.EXAMPLE
    powershell -File deploy/make_bundle.ps1
    # -> deploy/zkteco-backend-<timestamp>.tar.gz
#>
[CmdletBinding()]
param(
    [string]$Source,
    [string]$OutputDir
)

$ErrorActionPreference = 'Stop'

# Resolved in the body: $PSScriptRoot is not reliably populated in param
# defaults when the script is launched with `powershell -File`.
$scriptDir = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
if (-not $Source)    { $Source = Split-Path -Parent $scriptDir }
if (-not $OutputDir) { $OutputDir = $scriptDir }

$Source = (Resolve-Path $Source).Path
$OutputDir = (Resolve-Path $OutputDir).Path

if (-not (Test-Path $Source)) { throw "Source tidak ditemukan: $Source" }

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$archive = Join-Path $OutputDir "zkteco-backend-$stamp.tar.gz"

# Staging directory so the archive has a clean top-level folder.
$staging = Join-Path ([System.IO.Path]::GetTempPath()) "zkteco-bundle-$stamp"
$target = Join-Path $staging 'zkteco-backend'

New-Item -ItemType Directory -Path $target -Force | Out-Null

$excludeDirs = @('.venv', 'venv', '__pycache__', '.pytest_cache', '.ruff_cache', '.git', 'deploy', 'dist', '.cache')
$excludeFiles = @('*.db', '*.sqlite3', '*.pyc', '.env')

Write-Host "Mengumpulkan file dari $Source" -ForegroundColor Cyan

Get-ChildItem -Path $Source -Recurse -Force | Where-Object {
    $rel = $_.FullName.Substring($Source.Length).TrimStart('\')
    $parts = $rel -split '\\'
    $skip = $false
    foreach ($part in $parts) { if ($excludeDirs -contains $part) { $skip = $true; break } }
    if (-not $skip) {
        foreach ($pattern in $excludeFiles) { if ($_.Name -like $pattern) { $skip = $true; break } }
    }
    -not $skip
} | ForEach-Object {
    $rel = $_.FullName.Substring($Source.Length).TrimStart('\')
    $dest = Join-Path $target $rel
    if ($_.PSIsContainer) {
        New-Item -ItemType Directory -Path $dest -Force | Out-Null
    } else {
        $parent = Split-Path -Parent $dest
        if (-not (Test-Path $parent)) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
        Copy-Item $_.FullName -Destination $dest -Force
    }
}

# The deploy/ folder is excluded above then re-added deliberately: install.sh
# and the unit files must ship, but nothing else from it.
$deploySrc = Join-Path $Source 'deploy'
if (Test-Path $deploySrc) {
    $deployDst = Join-Path $target 'deploy'
    New-Item -ItemType Directory -Path $deployDst -Force | Out-Null
    foreach ($f in 'install.sh', 'zkteco-api.service', 'zkteco-worker.service', 'nginx-zkteco.conf') {
        $src = Join-Path $deploySrc $f
        if (Test-Path $src) { Copy-Item $src -Destination $deployDst -Force }
    }
}

# Ship the panel list so the pre-flight check can run on the server.
$csv = Join-Path (Split-Path -Parent $Source) 'devices_input.csv'
if (Test-Path $csv) {
    Copy-Item $csv -Destination (Join-Path $target 'devices_input.csv') -Force
    Write-Host "  + devices_input.csv" -ForegroundColor DarkGray
}

if (Test-Path $archive) { Remove-Item $archive -Force }

Write-Host "Membuat arsip..." -ForegroundColor Cyan
tar -czf $archive -C $staging zkteco-backend

Remove-Item $staging -Recurse -Force

$size = [math]::Round((Get-Item $archive).Length / 1KB, 1)
Write-Host ""
Write-Host "Bundle siap: $archive ($size KB)" -ForegroundColor Green
Write-Host ""
Write-Host "Kirim ke server:" -ForegroundColor Yellow
Write-Host "  scp `"$archive`" sentral@192.168.0.27:/tmp/"
Write-Host ""
Write-Host "Di server:" -ForegroundColor Yellow
Write-Host "  sudo tar -xzf /tmp/$(Split-Path $archive -Leaf) -C /tmp"
Write-Host "  sudo mv /tmp/zkteco-backend /opt/zkteco-backend"
Write-Host "  cd /opt/zkteco-backend && sudo bash deploy/install.sh"
Write-Host ""
Write-Host "Catatan: pakai 'bash deploy/install.sh', bukan './deploy/install.sh'." -ForegroundColor DarkGray
Write-Host "tar dari Windows tidak menyimpan bit executable." -ForegroundColor DarkGray
