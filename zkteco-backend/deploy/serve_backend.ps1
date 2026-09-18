<#
.SYNOPSIS
    Menyalakan backend ZKTeco C3 sebagai proses yang hidup terus.

.DESCRIPTION
    "Tombol" untuk dev di Windows. Default: uvicorn dinyalakan sebagai proses
    LATAR (window tersembunyi) supaya backend tetap jalan walaupun terminal atau
    VS Code ditutup, dan supervisor ini menyalakannya lagi kalau prosesnya mati
    (crash / koneksi putus / keluar sendiri). Jadi server tidak lagi "mati diam".

    Yang dijalankan:
        uvicorn app.main:app --app-dir <repo> --host 0.0.0.0 --port 8000
        --reload --reload-include *.html
    --reload berarti perubahan *.py DAN app/static/dashboard.html langsung
    dipakai tanpa restart manual (dashboard.html dibaca saat import).

    Environment yang dipasang untuk proses backend:
        DATABASE_URL       default sqlite:///<repo>/live_check.db
        SCHEDULER_ENABLED  selalu "false" - scheduler hanya di proses worker;
                           panel hanya menerima satu koneksi sekaligus
        PUSH_AGENT_URL     parameter -PushAgentUrl, lalu registry (User, lalu
                           Machine), baru environment proses; terakhir default
                           http://127.0.0.1:8081. Bacaannya dicatat di
                           supervisor.log supaya agen yang salah tidak dipakai
                           diam-diam
        PUSH_AGENT_TOKEN   urutan sama: -PushAgentToken, registry
                           (PUSH_AGENT_TOKEN / ZK_PUSH_AGENT_TOKEN), lalu
                           environment proses

    Berkas di <repo>\logs\:
        backend.log      log uvicorn (stderr) untuk run yang sedang/terakhir jalan
        backend.log.1    log run SEBELUMNYA - ini yang dibaca setelah crash
        backend.out.log  stdout uvicorn (biasanya sepi)
        supervisor.log   catatan supervisor: restart, exit code, alasan
        backend.pid      PID supervisor, dipakai oleh -Action stop

.PARAMETER Action
    start (default) | stop | status

.PARAMETER Port
    Port HTTP. Default 8000 (port yang dipakai tim).

.PARAMETER DatabaseUrl
    Override DATABASE_URL, mis. sqlite:///C:/temp/coba.db

.PARAMETER NoReload
    Matikan auto-reload (uvicorn berjalan seperti tanpa --reload).

.PARAMETER Force
    Dipakai dengan -Action stop: bersihkan juga proses python YATIM yang masih
    memegang port. Proses seperti itu muncul kalau reloader uvicorn dibunuh
    manual, sehingga anak spawn-nya ('--multiprocessing-fork') jadi yatim dan
    tidak bisa ditemukan lewat daftar port. Agent push (zk_push_agent.py) tidak
    pernah disentuh.

.PARAMETER AsSupervisor
    Internal. Dipakai saat skrip ini melahirkan dirinya sendiri sebagai proses
    latar; jangan dipanggil manual.

.EXAMPLE
    .\deploy\serve_backend.ps1
    .\deploy\serve_backend.ps1 -Action status
    .\deploy\serve_backend.ps1 -Action stop
    .\deploy\serve_backend.ps1 -Port 8010 -NoReload
#>
[CmdletBinding()]
param(
    [ValidateSet('start', 'stop', 'status')]
    [string]$Action = 'start',

    [int]$Port = 8000,
    [string]$BindHost = '0.0.0.0',

    # Kosong = deteksi otomatis: .venv di dalam repo, lalu Python sistem.
    [string]$Python = '',

    [string]$DatabaseUrl = '',
    [string]$PushAgentUrl = '',
    [string]$PushAgentToken = '',

    [switch]$NoReload,

    [switch]$Force,

    [int]$RestartDelaySeconds = 3,

    [switch]$AsSupervisor
)

$ErrorActionPreference = 'Stop'
# Log ditulis UTF-8, bukan UTF-16 (default Redirection di PowerShell 5.1).
$PSDefaultParameterValues['Out-File:Encoding'] = 'utf8'

$repoRoot = Split-Path -Parent $PSScriptRoot          # deploy/ -> zkteco-backend/
$logDir = Join-Path $repoRoot 'logs'
$backendLog = Join-Path $logDir 'backend.log'
$backendOutLog = Join-Path $logDir 'backend.out.log'
$supervisorLog = Join-Path $logDir 'supervisor.log'
$pidFile = Join-Path $logDir 'backend.pid'
$healthUrl = "http://127.0.0.1:$Port/health"
$backendUrl = "http://localhost:$Port/"

# ---------------------------------------------------------------- utilitas ---

function Write-Note {
    param([string]$Text, [string]$Colour = 'Gray')

    # Catatan supervisor ditulis ke berkas TERPISAH: saat uvicorn jalan, berkas
    # backend.log sedang dipegang proses itu, dan menulis ke sana bisa gagal
    # diam-diam karena bentrok berbagi berkas.
    $line = "[{0}] {1}" -f (Get-Date -Format 'HH:mm:ss'), $Text
    Write-Host $line -ForegroundColor $Colour
    if (Test-Path -LiteralPath $logDir) {
        Add-Content -LiteralPath $supervisorLog -Value $line -ErrorAction SilentlyContinue
    }
}

function Get-PythonPath {
    $candidates = @()
    if ($Python) { $candidates += $Python }
    $candidates += @(
        (Join-Path $repoRoot '.venv\Scripts\python.exe'),
        (Join-Path $repoRoot '.venv\bin\python'),
        'C:\Python313\python.exe'
    )
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }
    $onPath = Get-Command python -ErrorAction SilentlyContinue
    if ($onPath) { return $onPath.Source }
    throw 'Python tidak ditemukan. Pakai -Python <path python.exe>.'
}

function Assert-UvicornAvailable {
    param([string]$PythonPath)

    # stderr proses native menjadi error-record di PowerShell 5.1; turunkan
    # ErrorActionPreference supaya pemeriksaan ini tidak berhenti di tengah.
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $PythonPath -c 'import uvicorn' *> $null
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
    }
    if ($exitCode -ne 0) {
        throw ("Interpreter ini tidak punya uvicorn: $PythonPath`n" +
               "  Perbaiki: & `"$PythonPath`" -m pip install -r `"$repoRoot\requirements.txt`"")
    }
}

function Get-ListenerPid {
    param([int]$LocalPort)

    $connection = Get-NetTCPConnection -LocalPort $LocalPort -State Listen -ErrorAction SilentlyContinue
    if ($connection) { return ($connection | Select-Object -First 1).OwningProcess }
    return $null
}

function Get-SupervisorPid {
    if (-not (Test-Path -LiteralPath $pidFile)) { return $null }
    $value = 0
    if ([int]::TryParse((Get-Content -LiteralPath $pidFile -Raw).Trim(), [ref]$value)) { return $value }
    return $null
}

function Test-PidAlive {
    param($ProcessId)

    if (-not $ProcessId) { return $false }
    return [bool](Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)
}

function Stop-ProcessTree {
    param([int]$ProcessId)

    # taskkill.exe menulis pesan gagal ke stderr. Di PowerShell 5.1 itu menjadi
    # ErrorRecord, dan dengan ErrorActionPreference=Stop skrip MATI di tengah
    # jalan hanya karena PID yang sudah tidak ada. Semua stream dibuang dan
    # hasilnya cukup dari exit code (0 = berhasil, 128 = tidak ditemukan).
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & taskkill.exe /PID $ProcessId /T /F *> $null
        return $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
    }
}

function Get-StrayUvicornProcesses {
    # Proses python YATIM: anak spawn dari reloader uvicorn
    # ('--multiprocessing-fork') yang induknya sudah mati. Daftar port bisa
    # menunjuk PID yang sudah tidak ada, jadi prosesnya dicari lewat command
    # line. Agent push (zk_push_agent.py) TIDAK PERNAH disentuh.
    $live = @{}
    foreach ($item in (Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)) {
        $live[[int]$item.ProcessId] = $true
    }

    $stray = @()
    $pythonProcesses = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue
    foreach ($item in $pythonProcesses) {
        $commandLine = [string]$item.CommandLine
        if ($commandLine -like '*zk_push_agent*') { continue }
        if ($commandLine -like '*uvicorn*app.main*') {
            $stray += $item
            continue
        }
        if ($commandLine -like '*multiprocessing-fork*' -and -not $live[[int]$item.ParentProcessId]) {
            $stray += $item
        }
    }
    return $stray
}

function Get-HealthJson {
    try {
        $health = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 3 -UseBasicParsing
        return ($health | ConvertTo-Json -Compress)
    } catch {
        return $null
    }
}

function Show-LogTail {
    param([int]$Lines = 10)

    # backend.log = run yang sedang/terakhir jalan; .1 = run sebelumnya (tempat
    # membaca penyebab crash); supervisor.log = kapan dan kenapa di-restart.
    $targets = @(
        @{ Path = $backendLog; Lines = $Lines },
        @{ Path = "${backendLog}.1"; Lines = 6 },
        @{ Path = $supervisorLog; Lines = 8 }
    )
    foreach ($target in $targets) {
        if (-not (Test-Path -LiteralPath $target.Path)) { continue }
        Write-Host "  --- $($target.Path) (ekor $($target.Lines) baris) ---" -ForegroundColor DarkGray
        Get-Content -LiteralPath $target.Path -Tail $target.Lines -ErrorAction SilentlyContinue |
            ForEach-Object { Write-Host "  $_" -ForegroundColor DarkGray }
    }
}

function Get-PersistedSetting {
    param([string]$Name)

    # Cadangan untuk environment yang sudah BASI. Shell task VS Code memakai
    # snapshot environment saat VS Code dinyalakan, jadi nilai yang di-set
    # belakangan lewat setx (scope User) atau oleh installer agen (scope
    # Machine) tidak terlihat di sana. Dulu akibatnya launcher DIAM-DIAM
    # kembali ke 127.0.0.1:8081, dan push menuju agen yang salah tanpa pesan
    # apa pun. User dibaca lebih dulu: itu precedence Windows untuk proses yang
    # dijalankan user ini.
    foreach ($scope in 'User', 'Machine') {
        try { $value = [Environment]::GetEnvironmentVariable($Name, $scope) } catch { $value = '' }
        if ($value) { return $value }
    }
    return ''
}

function Test-PushAgentToken {
    param([string]$Url, [string]$Token)

    # Best-effort. Kegagalan di sini TIDAK menghentikan backend: agen bisa saja
    # baru dibuka setelah backend jalan. Tujuannya hanya membuat masalah token
    # terlihat di layar/berkas log saat start, bukan menunggu push pertama gagal
    # dengan 401 di dashboard (2026-09-18: satu nilai PUSH_AGENT_TOKEN lama
    # membuat SEMUA panel menjawab "Authorization: Bearer <token> wajib").
    try {
        $request = [System.Net.HttpWebRequest]::Create(($Url.TrimEnd('/') + '/health'))
        $request.Method = 'GET'
        $request.Timeout = 4000
        if ($Token) { $request.Headers.Add('Authorization', "Bearer $Token") }
        $response = $request.GetResponse()
        $response.Close()
        return 'OK - token diterima agen'
    } catch [System.Net.WebException] {
        $status = 0
        if ($_.Exception.Response) { $status = [int]$_.Exception.Response.StatusCode }
        if ($status -eq 401) {
            return 'DITOLAK (401) - token backend tidak sama dengan token agen'
        }
        return 'tidak bisa dihubungi - pastikan agen jalan dan portnya terbuka'
    } catch {
        return 'tidak bisa diperiksa'
    }
}

function Set-BackendEnvironment {
    if ($DatabaseUrl) {
        $env:DATABASE_URL = $DatabaseUrl
    } elseif (-not $env:DATABASE_URL) {
        $env:DATABASE_URL = 'sqlite:///' + ((Join-Path $repoRoot 'live_check.db') -replace '\\', '/')
    }

    # Wajib: scheduler di proses API membuat poll dobel, dan panel hanya
    # menerima satu koneksi sekaligus. Worker terpisah yang menyalakannya.
    $env:SCHEDULER_ENABLED = 'false'

    #: Setting agen: parameter menang, lalu nilai yang DIPERSISTENSI di registry,
    #: baru environment proses. Registry didahulukan karena shell task VS Code
    #: memakai snapshot environment saat VS Code dinyalakan: nilai lama di sana
    #: tidak bisa dibedakan dari pilihan operator, dan akibatnya push menuju
    #: agen yang salah tanpa satu pun pesan (terjadi 2026-09-18, setelah agen
    #: dipindah ke PC lain). Nilai yang diabaikan SELALU dicatat di
    #: supervisor.log; -PushAgentUrl / -PushAgentToken tetap bisa memaksa.
    $defaultPushAgentUrl = 'http://127.0.0.1:8081'
    $storedUrl = Get-PersistedSetting 'PUSH_AGENT_URL'
    $envUrl = $env:PUSH_AGENT_URL
    if ($PushAgentUrl) {
        $env:PUSH_AGENT_URL = $PushAgentUrl
        $script:PushAgentUrlSource = 'dari parameter -PushAgentUrl'
    } elseif ($storedUrl) {
        $env:PUSH_AGENT_URL = $storedUrl
        $script:PushAgentUrlSource = 'tersimpan di registry (User/Machine)'
        if ($envUrl -and $envUrl -ne $storedUrl) {
            $script:PushAgentUrlSource += "; PUSH_AGENT_URL proses ($envUrl) diabaikan"
        }
    } elseif ($envUrl) {
        $env:PUSH_AGENT_URL = $envUrl
        $script:PushAgentUrlSource = 'dari environment proses'
    } else {
        $env:PUSH_AGENT_URL = $defaultPushAgentUrl
        $script:PushAgentUrlSource = 'DEFAULT - PUSH_AGENT_URL tidak diset'
    }

    # Token diperlakukan sama. Bedanya tidak ada nilai default: satu-satunya
    # cara push menuju agen adalah token yang cocok dengan token agen itu.
    $storedToken = Get-PersistedSetting 'ZK_PUSH_AGENT_TOKEN'
    $storedTokenName = 'ZK_PUSH_AGENT_TOKEN'
    if (-not $storedToken) {
        $storedToken = Get-PersistedSetting 'PUSH_AGENT_TOKEN'
        $storedTokenName = 'PUSH_AGENT_TOKEN'
    }
    $envToken = $env:PUSH_AGENT_TOKEN
    $envTokenName = 'PUSH_AGENT_TOKEN'
    if (-not $envToken) {
        $envToken = $env:ZK_PUSH_AGENT_TOKEN
        $envTokenName = 'ZK_PUSH_AGENT_TOKEN'
    }

    if ($PushAgentToken) {
        $env:PUSH_AGENT_TOKEN = $PushAgentToken
        $script:PushAgentTokenSource = 'dari parameter -PushAgentToken'
    } elseif ($storedToken) {
        $env:PUSH_AGENT_TOKEN = $storedToken
        $script:PushAgentTokenSource = "tersimpan di registry ($storedTokenName)"
        if ($envToken -and $envToken -ne $storedToken) {
            $script:PushAgentTokenSource += "; $envTokenName proses diabaikan (berbeda)"
        }
    } elseif ($envToken) {
        $env:PUSH_AGENT_TOKEN = $envToken
        $script:PushAgentTokenSource = "dari environment proses ($envTokenName)"
    } else {
        $env:PUSH_AGENT_TOKEN = ''
        $script:PushAgentTokenSource = 'KOSONG - push akan menjawab 503'
    }
    if (-not $env:LOG_LEVEL) { $env:LOG_LEVEL = 'INFO' }
    # Log uvicorn ditulis oleh Python; paksa UTF-8 supaya tidak ikut codepage ANSI.
    $env:PYTHONIOENCODING = 'utf-8'
}

function Get-BackendArguments {
    param([string]$RepoRoot, [int]$Port, [string]$BindHost, [switch]$NoReload)

    $arguments = @(
        '-m', 'uvicorn', 'app.main:app',
        '--app-dir', $RepoRoot,
        '--host', $BindHost,
        '--port', $Port
    )
    if (-not $NoReload) {
        $arguments += @('--reload', '--reload-dir', $RepoRoot, '--reload-include', '*.html')
    }
    return $arguments
}

function Quote-NativeArgument {
    param([string]$Value)

    if ($Value -match '\s') { return '"' + $Value + '"' }
    return $Value
}

# ------------------------------------------------------------- supervisor ----

function Invoke-Supervisor {
    param($PythonPath, $UvicornArguments)

    New-Item -ItemType Directory -Path $logDir -Force | Out-Null

    Set-Content -LiteralPath $pidFile -Value $PID -Encoding ascii
    Set-BackendEnvironment

    # Satu string command-line utuh: -ArgumentList berupa array menambahkan spasi
    # pada elemen yang sudah ber-quote di PowerShell 5.1.
    $nativeArguments = (($UvicornArguments | ForEach-Object { Quote-NativeArgument $_ }) -join ' ')

    Write-Note "supervisor start (PID $PID), python: $PythonPath" 'Cyan'
    Write-Note "database: $env:DATABASE_URL"
    Write-Note "push agent: $env:PUSH_AGENT_URL ($script:PushAgentUrlSource)"
    Write-Note "token agen: $script:PushAgentTokenSource"
    $agentCheck = Test-PushAgentToken -Url $env:PUSH_AGENT_URL -Token $env:PUSH_AGENT_TOKEN
    $agentColour = if ($agentCheck -eq 'OK - token diterima agen') { 'Green' } else { 'Yellow' }
    Write-Note "agen: $agentCheck" $agentColour

    $delay = $RestartDelaySeconds
    try {
        while ($true) {
            # Seluruh isi loop dibungkus: supervisor TIDAK BOLEH mati diam-diam
            # karena satu error tak terduga. Kalau gagal, dicatat lalu diulang.
            try {
                # Run sebelumnya disimpan sebagai .1 supaya penyebab crash tidak
                # hilang. Move-Item -Force di PowerShell 5.1 tidak selalu menimpa
                # berkas .1 yang sudah ada, jadi berkas lama dihapus lebih dulu.
                foreach ($path in @($backendLog, $backendOutLog)) {
                    if (-not (Test-Path -LiteralPath $path)) { continue }
                    if (Test-Path -LiteralPath "${path}.1") {
                        Remove-Item -LiteralPath "${path}.1" -Force
                    }
                    Move-Item -LiteralPath $path -Destination "${path}.1" -Force
                }

                Write-Note ("menjalankan: python " + $nativeArguments)
                $startedAt = Get-Date

                # Menunggu proses PYTHON-nya langsung, bukan pembungkus cmd.exe:
                # cmd.exe dapat tetap hidup setelah anaknya dibunuh, sehingga
                # supervisor menunggu selamanya dan restart tidak pernah terjadi.
                $process = Start-Process -FilePath $PythonPath `
                    -ArgumentList $nativeArguments `
                    -WorkingDirectory $repoRoot -NoNewWindow -PassThru `
                    -RedirectStandardOutput $backendOutLog `
                    -RedirectStandardError $backendLog
                Write-Note "uvicorn PID $($process.Id) jalan."
                $process.WaitForExit()

                # ExitCode dibaca lewat try/catch: pada PowerShell 5.1 nilai ini
                # bisa hilang ($null) setelah handle proses dilepas.
                $exitCode = $null
                try { $exitCode = $process.ExitCode } catch { $exitCode = 'tidak diketahui' }
                if ($null -eq $exitCode) { $exitCode = 'tidak diketahui' }

                $ranSeconds = [int]((Get-Date) - $startedAt).TotalSeconds
                Write-Note "uvicorn berhenti (exit $exitCode) setelah $ranSeconds detik." 'Yellow'

                # Backoff kalau prosesnya langsung mati berulang kali (mis. error
                # konfigurasi), supaya log tidak dibanjiri percobaan restart.
                if ($ranSeconds -lt 5) {
                    $delay = [Math]::Min($delay * 2, 30)
                } else {
                    $delay = $RestartDelaySeconds
                }
                Write-Note "menyalakan lagi dalam $delay detik..."
            } catch {
                Write-Note "ERROR tak terduga: $($_.Exception.Message)" 'Red'
            }
            Start-Sleep -Seconds $delay
        }
    } finally {
        Remove-Item -LiteralPath $pidFile -ErrorAction SilentlyContinue
        Write-Note 'supervisor berhenti.' 'Cyan'
    }
}

# ------------------------------------------------------------------ aksi -----

if ($Action -eq 'start' -and -not $AsSupervisor) {
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null

    $pythonPath = Get-PythonPath
    Assert-UvicornAvailable -PythonPath $pythonPath

    $existingPid = Get-SupervisorPid
    if (Test-PidAlive $existingPid) {
        Write-Note "Sudah jalan di latar (supervisor PID $existingPid)." 'Green'
        Write-Note "Status: .\status-backend.bat   -   Hentikan: .\hentikan-backend.bat"
        exit 0
    }

    # Peringatan dini, bukan penghalang: DB baru dibuat oleh aplikasi, tapi
    # skemanya belum tentu mengikuti Alembic.
    if ($DatabaseUrl) { $env:DATABASE_URL = $DatabaseUrl }
    if (-not $env:DATABASE_URL) {
        $env:DATABASE_URL = 'sqlite:///' + ((Join-Path $repoRoot 'live_check.db') -replace '\\', '/')
    }
    if ($env:DATABASE_URL -match '^sqlite:///(.+)$') {
        $dbPath = $matches[1] -replace '/', '\'
        if (-not (Test-Path -LiteralPath $dbPath)) {
            Write-Note "Database belum ada: $dbPath" 'Yellow'
            Write-Note "Jalankan dulu: alembic upgrade head" 'Yellow'
        }
    }

    $listener = Get-ListenerPid -LocalPort $Port
    if ($listener) {
        $name = (Get-Process -Id $listener -ErrorAction SilentlyContinue).ProcessName
        Write-Note "Port $Port sudah dipakai proses PID $listener ($name)." 'Red'
        Write-Note 'Hentikan dulu: .\hentikan-backend.bat' 'Red'
        Write-Note 'Kalau prosesnya sisa/yatim: .\deploy\serve_backend.ps1 -Action stop -Force' 'Red'
        exit 1
    }

    # Satu string command-line utuh (bukan array): -ArgumentList berupa array
    # menambahkan spasi pada elemen yang sudah ber-quote, sehingga
    # -Python "C:\..." sampai sebagai " C:\... " dan diabaikan.
    $childCommandLine = '-NoProfile -ExecutionPolicy Bypass -File "' + $PSCommandPath + '"' +
        ' -Action start -AsSupervisor' +
        ' -Port ' + $Port +
        ' -BindHost ' + $BindHost +
        ' -Python "' + $pythonPath + '"' +
        ' -RestartDelaySeconds ' + $RestartDelaySeconds
    if ($DatabaseUrl) { $childCommandLine += ' -DatabaseUrl "' + $DatabaseUrl + '"' }
    if ($PushAgentUrl) { $childCommandLine += ' -PushAgentUrl "' + $PushAgentUrl + '"' }
    if ($PushAgentToken) { $childCommandLine += ' -PushAgentToken "' + $PushAgentToken + '"' }
    if ($NoReload) { $childCommandLine += ' -NoReload' }

    Start-Process -FilePath 'powershell.exe' -ArgumentList $childCommandLine -WindowStyle Hidden | Out-Null
    Write-Note 'Menyalakan backend di latar (proses tetap hidup setelah jendela ini ditutup)...'

    $health = $null
    for ($attempt = 1; $attempt -le 10 -and -not $health; $attempt++) {
        Start-Sleep -Seconds 1
        $health = Get-HealthJson
    }

    if ($health) {
        Write-Note "Backend JALAN di $backendUrl" 'Green'
        Write-Note "Health : $health"
        Write-Note "PID    : supervisor $(Get-SupervisorPid), port $Port"
        Write-Note "Log    : $backendLog"
        Write-Note "         $supervisorLog (kapan/kenapa di-restart)"
        Write-Note 'Perubahan *.py dan dashboard.html otomatis dipakai (auto-reload).'
        exit 0
    }

    Write-Note "Backend belum merespons di $healthUrl." 'Red'
    Show-LogTail -Lines 15
    exit 1
}

if ($Action -eq 'start') {
    Invoke-Supervisor -PythonPath $Python -UvicornArguments (Get-BackendArguments -RepoRoot $repoRoot -Port $Port -BindHost $BindHost -NoReload:$NoReload)
    exit 0
}

if ($Action -eq 'stop') {
    $supervisorPid = Get-SupervisorPid
    $stopped = $false

    if (Test-PidAlive $supervisorPid) {
        Write-Note "Menghentikan supervisor PID $supervisorPid (beserta seluruh anak prosesnya)..."
        [void](Stop-ProcessTree -ProcessId $supervisorPid)
        $stopped = $true
    }
    Remove-Item -LiteralPath $pidFile -ErrorAction SilentlyContinue

    Start-Sleep -Milliseconds 700
    $listener = Get-ListenerPid -LocalPort $Port
    if ($listener) {
        $process = Get-Process -Id $listener -ErrorAction SilentlyContinue
        $name = if ($process) { $process.ProcessName } else { '' }
        if ($name -and $name -notin @('python', 'python3', 'pythonw', 'powershell', 'pwsh', 'cmd', 'uvicorn')) {
            Write-Note "Port $Port dipakai proses lain ($name, PID $listener) - TIDAK dihentikan." 'Red'
        } else {
            Write-Note "Masih ada proses di port $Port (PID $listener) - dihentikan juga."
            if ((Stop-ProcessTree -ProcessId $listener) -eq 0) {
                $stopped = $true
            } else {
                Write-Note "PID $listener sudah tidak ada (sisa proses yatim)." 'Yellow'
            }
        }
    }

    Start-Sleep -Milliseconds 500
    if (Get-ListenerPid -LocalPort $Port) {
        $stray = @(Get-StrayUvicornProcesses)
        if ($stray.Count -eq 0) {
            Write-Note "Port $Port masih tercatat dipakai, tapi tidak ada proses yang jelas." 'Yellow'
            Write-Note 'Coba jalankan stop sekali lagi sesaat lagi.' 'Yellow'
            exit 1
        }
        if (-not $Force) {
            Write-Note "Sisa proses yatim pemakai port: PID $($stray.ProcessId -join ', ')." 'Yellow'
            Write-Note 'Bersihkan dengan: .\deploy\serve_backend.ps1 -Action stop -Force' 'Yellow'
            exit 1
        }
        foreach ($process in $stray) {
            Write-Note "Menghentikan proses yatim PID $($process.ProcessId) (-Force)..." 'Yellow'
            if ((Stop-ProcessTree -ProcessId $process.ProcessId) -eq 0) { $stopped = $true }
        }
        Start-Sleep -Milliseconds 600
        if (Get-ListenerPid -LocalPort $Port) {
            Write-Note "Port $Port masih dipakai. Cek: .\status-backend.bat" 'Red'
            exit 1
        }
    }

    if ($stopped) { Write-Note "Backend berhenti. Port $Port bebas." 'Green' }
    else { Write-Note "Tidak ada backend yang jalan di port $Port." }
    exit 0
}

# status
$supervisorPid = Get-SupervisorPid
$supervisorAlive = Test-PidAlive $supervisorPid
$listener = Get-ListenerPid -LocalPort $Port
$health = Get-HealthJson

$state = if ($health) { 'JALAN' } elseif ($listener) { 'JALAN (tanpa supervisor)' } else { 'MATI' }
$colour = if ($health) { 'Green' } else { 'Red' }

Write-Host ''
Write-Host "Status  : $state" -ForegroundColor $colour
Write-Host "URL     : $backendUrl"
if ($supervisorAlive) { Write-Host "PID     : $supervisorPid (supervisor, auto-restart aktif)" }
else { Write-Host 'PID     : -' }
if ($listener) { Write-Host "Port    : $Port didengarkan PID $listener" }
else { Write-Host "Port    : $Port bebas" }
if ($health) { Write-Host "Health  : $health" }
else { Write-Host "Health  : tidak merespons ($healthUrl)" }
Write-Host "Log app : $backendLog"
Write-Host "          ${backendLog}.1 (run sebelumnya), $backendOutLog (stdout)"
Write-Host "Log sup : $supervisorLog"
if (-not $supervisorAlive -and -not $listener) {
    Write-Host 'Jalankan: .\jalankan-backend.bat' -ForegroundColor Yellow
}
Show-LogTail -Lines 15

if ($health) { exit 0 }
exit 1
