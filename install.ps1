# ==============================================================================
# SparkGrid Web Client — Windows Installer (idempotent)
# Run as Administrator:  powershell -ExecutionPolicy Bypass -File .\install.ps1
# ==============================================================================
#Requires -Version 5.1
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
# StrictMode 2.0, not Latest: 'Latest' makes any reference to a
# non-existent property fatal, which is wrong for an installer that must
# probe the environment it's running in. That's exactly what killed the
# first real run — $PSVersionTable.Platform doesn't exist on Windows
# PowerShell 5.1, and the script died on line 37 before doing anything.
Set-StrictMode -Version 2.0

# Windows PowerShell 5.1 defaults to TLS 1.0/1.1, which python.org and
# bootstrap.pypa.io no longer accept — every download would fail with an
# opaque "Could not create SSL/TLS secure channel". Enabling TLS 1.2 must
# happen before the first web request. Written as a bitwise OR so any
# protocol already enabled is preserved.
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
} catch {
    Write-Warning "Не удалось включить TLS 1.2: $_"
}

$script:currentUser   = $env:USERNAME
$script:installDir    = 'C:\Program Files\SparkGrid Web Client'
$script:pythonDir     = Join-Path $script:installDir 'python'
$script:pythonExe     = Join-Path $script:pythonDir 'python.exe'
$script:internalDir   = Join-Path $script:installDir '_internal'
$script:servicesRoot  = Join-Path "C:\Users\$script:currentUser" 'SparkGrid-services'
$script:softwareDir   = Join-Path $script:servicesRoot 'software'
$script:botDir        = Join-Path $script:servicesRoot 'bot'
$script:dataDir       = "C:\Users\$script:currentUser\AppData\Local\SparkGrid\data"
$script:libDir        = "C:\Users\$script:currentUser\AppData\Local\SparkGrid\lib"
$script:patchesSrc    = Join-Path $PSScriptRoot 'patches'

# ── Logging helpers ───────────────────────────────────────────────────────────
function Write-Step  { param([string]$msg) Write-Host "[*] $msg" -ForegroundColor Cyan }
function Write-OK    { param([string]$msg) Write-Host "[+] $msg" -ForegroundColor Green }
function Write-Skip  { param([string]$msg) Write-Host "[~] $msg" -ForegroundColor DarkYellow }
function Write-Warn2 { param([string]$msg) Write-Host "[!] $msg" -ForegroundColor Yellow }
function Write-Err2  { param([string]$msg) Write-Host "[!] $msg" -ForegroundColor Red }

# ══════════════════════════════════════════════════════════════════════════════
# STEP 1: Pre-flight checks (Windows, admin, disk space, internet)
# ══════════════════════════════════════════════════════════════════════════════
Write-Step 'Шаг 1: Проверка окружения…'

# OS check
# Windows check that works on both Windows PowerShell 5.1 and
# PowerShell 7+. $PSVersionTable.Platform only exists from PS6 onward, so
# reading it directly breaks on 5.1 — which is what ships with Windows
# Server and what Alexander's VPS runs. [Environment]::OSVersion is
# present in every version.
$isWindowsOS = $true
try {
    $isWindowsOS = [Environment]::OSVersion.Platform -eq 'Win32NT'
} catch {
    $isWindowsOS = $true  # can't tell -> assume Windows, the installer is Windows-only anyway
}
if (-not $isWindowsOS) {
    Write-Err2 'Этот скрипт предназначен только для Windows.'
    exit 1
}
Write-OK 'ОС: Windows.'

# Admin rights
$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Err2 'Требуются права администратора. Перезапустите PowerShell от имени администратора.'
    exit 1
}
Write-OK 'Права администратора подтверждены.'

# 20 GB free disk on system drive
$sysDrive = $env:SystemDrive
if (-not $sysDrive) { $sysDrive = 'C:' }
$freeBytes = (Get-PSDrive -Name ($sysDrive.Replace(':', '')).Trim() -ErrorAction SilentlyContinue).Free
if (-not $freeBytes) {
    # Fallback to WMI
    $driveInfo = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID = '$sysDrive'"
    $freeBytes = $driveInfo.FreeSpace
}
$freeGB = [math]::Round($freeBytes / 1GB, 2)
if ($freeGB -lt 20) {
    Write-Err2 "Недостаточно свободного места на диске ${sysDrive}: нужно ≥20 GB, доступно $freeGB GB."
    exit 1
}
Write-OK "Свободное место на диске: $freeGB GB."

# Internet connectivity
$online = $false
try {
    $resp = Test-Connection -ComputerName '8.8.8.8' -Count 2 -Quiet -ErrorAction Stop
    if ($resp) { $online = $true }
} catch {}
if (-not $online) {
    try {
        $resp = Invoke-WebRequest -Uri 'https://www.python.org' -UseBasicParsing -TimeoutSec 10 -ErrorAction Stop
        $online = $true
    } catch {}
}
if (-not $online) {
    Write-Err2 'Нет интернет-соединения. Установите подключение и повторите.'
    exit 1
}
Write-OK 'Интернет-соединение активно.'

# ══════════════════════════════════════════════════════════════════════════════
# STEP 2: Python 3.11+ — check, download, install, verify pip
# ══════════════════════════════════════════════════════════════════════════════
Write-Step 'Шаг 2: Проверка Python 3.11+…'

function Get-PythonVersion {
    param([string]$exePath)
    if (-not (Test-Path $exePath)) { return $null }
    try {
        $out = & $exePath --version 2>&1
        $verStr = ($out | Out-String).Trim()
        # Python 3.x.y → parse
        if ($verStr -match 'Python (\d+)\.(\d+)\.(\d+)') {
            return @{
                Major = [int]$Matches[1]
                Minor = [int]$Matches[2]
                Patch = [int]$Matches[3]
                Raw   = $verStr
            }
        }
    } catch {}
    return $null
}

function Test-PythonOK {
    param([string]$exePath)
    $ver = Get-PythonVersion $exePath
    if ($null -eq $ver) { return $false }
    return ($ver.Major -ge 3 -and $ver.Minor -ge 11)
}

function Test-PipOK {
    param([string]$exePath)
    if (-not (Test-Path $exePath)) { return $false }
    try {
        $null = & $exePath -m pip --version 2>&1
        return $LASTEXITCODE -eq 0
    } catch { return $false }
}

$needPython = $true
if (Test-Path $script:pythonExe) {
    if (Test-PythonOK $script:pythonExe) {
        $v = Get-PythonVersion $script:pythonExe
        Write-OK "Bundled Python найден: $($v.Raw) по пути $($script:pythonExe)"
        $needPython = $false
    } else {
        Write-Warn2 "Bundled Python устарел или неработоспособен — переустанавливаю."
        Remove-Item $script:pythonDir -Recurse -Force -ErrorAction SilentlyContinue
    }
} else {
    # Check system Python
    $sysPython = $null
    try {
        $sysPython = (Get-Command python -ErrorAction Stop).Source
    } catch {}
    if (-not $sysPython) {
        try { $sysPython = (Get-Command python3 -ErrorAction Stop).Source } catch {}
    }
    # Reject Python that belongs to someone else's virtual environment.
    # The first real install found hermes-agent\venv\Scripts\python.exe on
    # PATH and installed all 19 packages into it. Three problems with that:
    # the packages vanish if that tool rebuilds its venv, the software ends
    # up depending on an unrelated program, and on a buyer's machine that
    # path doesn't exist at all. A bundled Python costs ~25MB and belongs
    # to us.
    $isForeignEnv = $false
    if ($sysPython) {
        $lowerPath = $sysPython.ToLower()
        foreach ($marker in @('\venv\', '\.venv\', '\virtualenv\', '\hermes', '\conda', '\anaconda')) {
            if ($lowerPath.Contains($marker)) { $isForeignEnv = $true; break }
        }
    }
    if ($isForeignEnv) {
        Write-Warn2 "Найден Python внутри чужого окружения ($sysPython) — не используем его."
        Write-Host "  Установим собственный Python, чтобы SparkGrid ни от чего не зависел."
    }
    elseif ($sysPython -and (Test-Path $sysPython)) {
        if (Test-PythonOK $sysPython) {
            $v = Get-PythonVersion $sysPython
            Write-OK "Системный Python найден: $($v.Raw) ($sysPython). Используем его."
            # Use system python — set pythonExe to system path
            $script:pythonExe = $sysPython
            $needPython = $false
        }
    }
}

if ($needPython) {
    Write-Step 'Python 3.11+ не найден. Скачивание и установка embeddable Python…'
    
    # Determine latest Python 3.11.x from python.org
    $pyVer = '3.11.9'
    $pyArch = 'amd64'
    $pyUrl = "https://www.python.org/ftp/python/$pyVer/python-$pyVer-embed-$pyArch.zip"
    $pyZip = Join-Path $env:TEMP "python-$pyVer-embed-$pyArch.zip"
    
    Write-Host "  Скачивание: $pyUrl"
    try {
        Invoke-WebRequest -Uri $pyUrl -OutFile $pyZip -UseBasicParsing -ErrorAction Stop
        # A download can "succeed" and still leave a truncated or empty
        # file — a dropped connection, a proxy error page. Unpacking that
        # fails later with something confusing, so check the size now
        # while we still know what went wrong.
        if (-not (Test-Path $pyZip) -or (Get-Item $pyZip).Length -lt 1MB) {
            throw "Скачанный файл повреждён или пуст"
        }
    } catch {
        Write-Warn2 "Не удалось скачать embeddable Python: $_"
        Write-Step 'Пробую установщик python.org (exe)...'
        $installerUrl = "https://www.python.org/ftp/python/$pyVer/python-$pyVer-amd64.exe"
        $installerPath = Join-Path $env:TEMP "python-$pyVer-amd64.exe"
        try {
            Invoke-WebRequest -Uri $installerUrl -OutFile $installerPath -UseBasicParsing -ErrorAction Stop
            if (-not (Test-Path $installerPath) -or (Get-Item $installerPath).Length -lt 5MB) {
                throw "Установщик Python скачан повреждённым"
            }
            Write-Host "  Запуск установщика (silent, для всех пользователей)…"
            $installProc = Start-Process -FilePath $installerPath -ArgumentList '/quiet','InstallAllUsers=1','PrependPath=1','Include_pip=1' -Wait -PassThru
            if ($installProc.ExitCode -ne 0) {
                Write-Err2 "Установщик Python завершился с кодом $($installProc.ExitCode)."
                exit 1
            }
            # Refresh PATH and find python
            $machinePath = [System.Environment]::GetEnvironmentVariable('PATH', 'Machine')
            $userPath    = [System.Environment]::GetEnvironmentVariable('PATH', 'User')
            $env:PATH    = "$machinePath;$userPath"
            $sysPy = $null
            try { $sysPy = (Get-Command python -ErrorAction Stop).Source } catch {}
            if ($sysPy) {
                $script:pythonExe = $sysPy
                # Copy python.exe to bundled dir for consistency
                if (-not (Test-Path $script:pythonDir)) { New-Item -Path $script:pythonDir -ItemType Directory -Force | Out-Null }
                Copy-Item $sysPy $script:pythonExe -Force -ErrorAction SilentlyContinue
            }
        } catch {
            Write-Err2 "Не удалось скачать установщик Python: $_"
            exit 1
        }
    }
    
    if (Test-Path $pyZip) {
        # Extract embeddable Python
        if (-not (Test-Path $script:pythonDir)) {
            New-Item -Path $script:pythonDir -ItemType Directory -Force | Out-Null
        }
        Expand-Archive -Path $pyZip -DestinationPath $script:pythonDir -Force
        Remove-Item $pyZip -Force -ErrorAction SilentlyContinue
        
        # Embeddable Python has no pip — install it
        Write-Step 'Установка pip для embeddable Python…'
        $getPip = Join-Path $env:TEMP 'get-pip.py'
        try {
            Invoke-WebRequest -Uri 'https://bootstrap.pypa.io/get-pip.py' -OutFile $getPip -UseBasicParsing -ErrorAction Stop
            if (-not (Test-Path $getPip) -or (Get-Item $getPip).Length -lt 10KB) {
                throw "get-pip.py скачан повреждённым"
            }
            & $script:pythonExe $getPip --no-warn-script-location 2>&1 | Out-Host
        } catch {
            Write-Err2 "Не удалось установить pip: $_"
            exit 1
        }
        Remove-Item $getPip -Force -ErrorAction SilentlyContinue
    }
    
    # Verify
    if (-not (Test-PythonOK $script:pythonExe)) {
        Write-Err2 "Python не установлен или версия ниже 3.11. Установите Python 3.11+ вручную и повторите."
        exit 1
    }
    $v = Get-PythonVersion $script:pythonExe
    Write-OK "Python установлен: $($v.Raw)"
}

# Verify pip
if (Test-PipOK $script:pythonExe) {
    Write-OK 'pip работает.'
} else {
    Write-Step 'pip не найден — устанавливаю…'
    $getPip = Join-Path $env:TEMP 'get-pip.py'
    try {
        Invoke-WebRequest -Uri 'https://bootstrap.pypa.io/get-pip.py' -OutFile $getPip -UseBasicParsing -ErrorAction Stop
        if (-not (Test-Path $getPip) -or (Get-Item $getPip).Length -lt 10KB) {
            throw "get-pip.py скачан повреждённым"
        }
        & $script:pythonExe $getPip --no-warn-script-location 2>&1 | Out-Host
    } catch {
        Write-Err2 "Не удалось установить pip: $_"
        exit 1
    }
    Remove-Item $getPip -Force -ErrorAction SilentlyContinue
    if (-not (Test-PipOK $script:pythonExe)) {
        Write-Err2 'pip всё ещё не работает после установки get-pip.py.'
        exit 1
    }
    Write-OK 'pip установлен и работает.'
}

# ══════════════════════════════════════════════════════════════════════════════
# STEP 3: Install all Python dependencies (idempotent)
# ══════════════════════════════════════════════════════════════════════════════
Write-Step 'Шаг 3: Установка Python-зависимостей…'

$packages = @(
    'fastapi',
    'uvicorn',
    'playwright',
    'pydantic',
    'pyotp',
    'aiohttp',
    'requests',
    'camoufox[geoip]',
    'browserforge',
    'pillow',
    'lxml',
    'pyyaml',
    'python-telegram-bot[job-queue]',
    'anthropic',
    'cryptography',
    'numpy',
    'imageio-ffmpeg',
    'platformdirs',
    'PySocks'
)

# Get list of installed packages
$installedRaw = & $script:pythonExe -m pip list --format=freeze 2>&1 | Out-String
$installedLines = $installedRaw -split "`n" | ForEach-Object { ($_ -split '==')[0].ToLower().Trim() }
$installedSet = [System.Collections.Generic.HashSet[string]]::new()
foreach ($line in $installedLines) { if ($line) { [void]$installedSet.Add($line) } }

function Test-PackageInstalled {
    param([string]$pkgSpec)
    # Strip version specifiers and extras
    $baseName = $pkgSpec -replace '\[.*\]', '' -replace '[>=<].*', '' -replace '_', '-'
    $baseName = $baseName.ToLower().Trim()
    return $installedSet.Contains($baseName)
}

$toInstall = @()
foreach ($pkg in $packages) {
    if (Test-PackageInstalled $pkg) {
        Write-Skip "  $pkg — уже установлен"
    } else {
        $toInstall += $pkg
    }
}

if ($toInstall.Count -gt 0) {
    Write-Step "Устанавливаю $($toInstall.Count) пакетов: $($toInstall -join ', ')"
    $pkgArgs = @('-m', 'pip', 'install', '--upgrade') + $toInstall
    & $script:pythonExe @pkgArgs 2>&1 | Out-Host
    if ($LASTEXITCODE -ne 0) {
        Write-Err2 "Ошибка установки пакетов: $($toInstall -join ', ')"
        exit 1
    }
    Write-OK 'Все пакеты установлены.'
} else {
    Write-OK 'Все зависимости уже установлены.'
}

# ══════════════════════════════════════════════════════════════════════════════
# STEP 4: Playwright chromium + Camoufox fetch
# ══════════════════════════════════════════════════════════════════════════════
Write-Step 'Шаг 4: Установка Playwright Chromium и Camoufox…'

# Playwright chromium
$pwInstalled = $false
try {
    $pwCheck = & $script:pythonExe -c "from playwright._impl._driver import compute_driver_executable; print(compute_driver_executable())" 2>&1
    # Also check if chromium browser is downloaded
    $pwBrowsersDir = Join-Path $env:LOCALAPPDATA 'ms-playwright'
    $chromiumFound = Get-ChildItem -Path $pwBrowsersDir -Filter 'chromium-*' -Directory -ErrorAction SilentlyContinue
    if ($chromiumFound) { $pwInstalled = $true }
} catch {}
if (-not $pwInstalled) {
    Write-Host '  Установка Chromium для Playwright…'
    & $script:pythonExe -m playwright install chromium 2>&1 | Out-Host
    if ($LASTEXITCODE -ne 0) {
        Write-Warn2 'Playwright install chromium завершился с ошибкой — продолжаем (возможно Chromium уже есть).'
    } else {
        Write-OK 'Playwright Chromium установлен.'
    }
} else {
    Write-Skip 'Playwright Chromium уже установлен.'
}

# Camoufox fetch
$camoufoxInstalled = $false
try {
    $camoCheck = & $script:pythonExe -c "import camoufox; print(camoufox.get_path())" 2>&1
    if ($camoCheck -and $camoCheck -match '\\') { $camoufoxInstalled = $true }
} catch {}
if (-not $camoufoxInstalled) {
    Write-Host '  Загрузка Camoufox…'
    & $script:pythonExe -m camoufox fetch 2>&1 | Out-Host
    if ($LASTEXITCODE -ne 0) {
        Write-Warn2 'Camoufox fetch завершился с ошибкой — продолжаем (можно повторить позже вручную).'
    } else {
        Write-OK 'Camoufox загружен.'
    }
} else {
    Write-Skip 'Camoufox уже загружен.'
}

# ══════════════════════════════════════════════════════════════════════════════
# STEP 5: Create service directories
# ══════════════════════════════════════════════════════════════════════════════
Write-Step 'Шаг 5: Создание директорий служб…'

$dirsToCreate = @($script:installDir, $script:internalDir, $script:pythonDir, $script:servicesRoot, $script:softwareDir, $script:botDir, $script:dataDir, $script:libDir)
foreach ($d in $dirsToCreate) {
    if (Test-Path $d) {
        Write-Skip "  $d — существует"
    } else {
        New-Item -Path $d -ItemType Directory -Force | Out-Null
        Write-OK "  $d — создан"
    }
}

# ══════════════════════════════════════════════════════════════════════════════
# STEP 6: Write start/stop scripts (with UTF-8 BOM)
# ══════════════════════════════════════════════════════════════════════════════
Write-Step 'Шаг 6: Создание скриптов запуска/остановки…'

# Helper: write file with UTF-8 BOM
function Write-FileBOM {
    param([string]$Path, [string]$Content)
    $utf8BOM = New-Object System.Text.UTF8Encoding($true)
    [System.IO.File]::WriteAllText($Path, $Content, $utf8BOM)
}

# ─── start_software.ps1 ─────────────────────────────────────────────────────────
$startSoftware = @'
# SparkGrid Software (Server) — detached launcher with auto-restart
# Process becomes independent of the launching session.

$workDir    = "C:\Program Files\SparkGrid Web Client"
$exe        = "$workDir\python\python.exe"
$scriptArgs = "_internal\app.py"
$logFile    = "C:\Users\<USER>\SparkGrid-services\software\run.log"
$pidFile    = "C:\Users\<USER>\SparkGrid-services\software\run.pid"

# Load secrets (API keys, tokens) from local file if present
$secretsFile = Join-Path $PSScriptRoot "secrets.local.ps1"
if (Test-Path $secretsFile) {
    . $secretsFile
} else {
    Write-Warning "secrets.local.ps1 не найден рядом со скриптом"
}

# Environment variables
$env:SPARKGRID_DATA_DIR     = "C:\Users\<USER>\AppData\Local\SparkGrid\data"
$env:SPARKGRID_CAMOUFOX_DIR = "$workDir\_internal\SparkBrowser"
$env:SPARKGRID_GEOIP_PATH   = "$workDir\_internal\camoufox\GeoLite2-City.mmdb"
$env:PYTHONPATH             = "$workDir\python\Lib\site-packages;$workDir\_internal;C:\Users\<USER>\AppData\Local\SparkGrid\lib;$workDir\lib"
$env:WEB_UI_HOST            = "127.0.0.1"
$env:WEB_UI_PORT            = "8770"

# Auto-restart wrapper script (runs hidden, restarts on crash with 10s delay)
# Crash-loop guard: 5 fast crashes (<120s each) stops auto-restart
$wrapperScript = @"
`$ErrorActionPreference = 'SilentlyContinue'
`$crashCount = 0
while (`$true) {
    `Write-Output "``[`$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] Starting SparkGrid Server..."
    `$startTime = `Get-Date
    `& "$exe" "$scriptArgs" 2>&1
    `$elapsed = (`Get-Date) - `$startTime
    if (`$elapsed.TotalSeconds -ge 120) {
        `$crashCount = 0
    } else {
        `$crashCount++
        `Write-Output "``[`$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] Short run (`$([int]`$elapsed.TotalSeconds)s) — crash `$crashCount/5"
    }
    if (`$crashCount -ge 5) {
        `Write-Output "``[`$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] Crash-loop: 5 быстрых падений подряд, автоперезапуск остановлен. Разберись руками, потом запусти заново через start-скрипт."
        break
    }
    `Write-Output "``[`$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] Server exited (code `$LASTEXITCODE). Restarting in 10s..."
    `Start-Sleep -Seconds 10
}
"@

# Check if already running
if (Test-Path $pidFile) {
    $oldPid = Get-Content $pidFile -ErrorAction SilentlyContinue
    if ($oldPid -and (Get-Process -Id $oldPid -ErrorAction SilentlyContinue)) {
        Write-Host "Уже запущено, PID $oldPid."
        exit 0
    }
}

# Write wrapper to temp file
$wrapperFile = "C:\Users\<USER>\SparkGrid-services\software\_wrapper.ps1"
$wrapperScript | Out-File -FilePath $wrapperFile -Encoding UTF8 -Force

$proc = Start-Process -FilePath "powershell.exe" `
    -ArgumentList "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $wrapperFile `
    -WorkingDirectory $workDir `
    -WindowStyle Hidden `
    -RedirectStandardOutput $logFile `
    -RedirectStandardError "$logFile.err" `
    -PassThru

$proc.Id | Out-File -FilePath $pidFile -Encoding ascii -Force
Write-Host "Запущено. PID: $($proc.Id)"
Write-Host "Лог: $logFile"
Write-Host "Ошибки: $logFile.err"
'@

# ─── stop_software.ps1 ──────────────────────────────────────────────────────────
$stopSoftware = @'
# SparkGrid Software (Server) — stop script
# Recursively kills the full process tree and waits for port 8770 release.

$pidFile = "C:\Users\<USER>\SparkGrid-services\software\run.pid"
$port = [int]($env:WEB_UI_PORT)
if (-not $port) { $port = 8770 }

function Get-DescendantProcessIds {
    param([int]$RootId)
    $all = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Select-Object ProcessId, ParentProcessId
    $result = @($RootId)
    $frontier = @($RootId)
    while ($frontier.Count -gt 0) {
        $children = $all | Where-Object { $frontier -contains $_.ParentProcessId } |
            Select-Object -ExpandProperty ProcessId
        $newOnes = $children | Where-Object { $result -notcontains $_ }
        if (-not $newOnes) { break }
        $result += $newOnes
        $frontier = $newOnes
    }
    return $result
}

if (-not (Test-Path $pidFile)) {
    Write-Host "PID-файл не найден."
    exit 0
}

$targetPid = Get-Content $pidFile -ErrorAction SilentlyContinue
if (-not $targetPid) {
    Write-Host "PID-файл пуст."
    Remove-Item $pidFile -ErrorAction SilentlyContinue
    exit 0
}

$proc = Get-Process -Id $targetPid -ErrorAction SilentlyContinue
if ($proc) {
    $tree = Get-DescendantProcessIds -RootId $targetPid
    Write-Host "Дерево процессов на остановку: $($tree -join ', ')"
    foreach ($p in $tree) {
        Stop-Process -Id $p -Force -ErrorAction SilentlyContinue
    }

    # Ждём подтверждения что все процессы дерева реально умерли
    $deadline = (Get-Date).AddSeconds(15)
    $stillAlive = @()
    do {
        Start-Sleep -Milliseconds 300
        $stillAlive = @($tree | Where-Object { Get-Process -Id $_ -ErrorAction SilentlyContinue })
    } while ($stillAlive.Count -gt 0 -and (Get-Date) -lt $deadline)

    if ($stillAlive.Count -gt 0) {
        Write-Warning "Не все процессы завершились за 15с: $($stillAlive -join ', ')"
    } else {
        Write-Host "Все процессы дерева подтверждённо остановлены."
    }

    # Ждём освобождения порта 8770
    $portDeadline = (Get-Date).AddSeconds(15)
    $portBusy = $null
    do {
        $portBusy = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
        if ($portBusy) { Start-Sleep -Milliseconds 300 }
    } while ($portBusy -and (Get-Date) -lt $portDeadline)

    if ($portBusy) {
        Write-Warning "Порт $port всё ещё занят спустя 15с ожидания."
    } else {
        Write-Host "Порт $port свободен."
    }
} else {
    Write-Host "Процесс с PID $targetPid уже не висит."
}
Remove-Item $pidFile -ErrorAction SilentlyContinue
'@

# ─── start_bot.ps1 ──────────────────────────────────────────────────────────────
$startBot = @'
# SparkGrid Telegram Bot — detached launcher with auto-restart
# Process becomes independent of the launching session.
$workDir    = "C:\Program Files\SparkGrid Web Client"
$exe        = "$workDir\python\python.exe"
$scriptArgs = "_internal\telegram_bot.py"
$logFile    = "C:\Users\<USER>\SparkGrid-services\bot\run.log"
$pidFile    = "C:\Users\<USER>\SparkGrid-services\bot\run.pid"

# Load secrets (API keys, tokens) from local file if present
$secretsFile = Join-Path $PSScriptRoot "secrets.local.ps1"
if (Test-Path $secretsFile) {
    . $secretsFile
} else {
    Write-Warning "secrets.local.ps1 не найден рядом со скриптом"
    # Try reading secrets from bot.db ads_power_config table
    $botDb = "C:\Users\<USER>\AppData\Local\SparkGrid\data\bot.db"
    if (Test-Path $botDb) {
        Write-Host "Чтение секретов из bot.db…"
        try {
            $dbResult = & $exe -c @"
import sqlite3, json
conn = sqlite3.connect(r'$botDb')
cur = conn.cursor()
try:
    cur.execute("SELECT key, value FROM ads_power_config")
    rows = cur.fetchall()
    for k, v in rows:
        print(f"{k}={v}")
except Exception:
    pass
conn.close()
"@
            foreach ($line in $dbResult) {
                if ($line -match '^(\S+)=(.*)$') {
                    $key = $Matches[1]
                    $val = $Matches[2]
                    Set-Item -Path "env:$key" -Value $val
                }
            }
        } catch {
            Write-Warning "Не удалось прочитать секреты из bot.db: $_"
        }
    }
}

# Environment variables
# TELEGRAM_CHAT_ID: must be set via /setup page or secrets.local.ps1
# If not set, bot will log a warning and notifications won't work.
if (-not $env:TELEGRAM_CHAT_ID) {
    Write-Warning "TELEGRAM_CHAT_ID не задан. Уведомления бота работать не будут. Настройте через http://127.0.0.1:8770/setup"
}
$env:SPARKGRID_API_URL  = "http://127.0.0.1:8770"
$env:SPARKGRID_DATA_DIR = "C:\Users\<USER>\AppData\Local\SparkGrid\data"
$env:PYTHONPATH         = "$workDir\python\Lib\site-packages;$workDir\_internal;C:\Users\<USER>\AppData\Local\SparkGrid\lib"

$wrapperFile = "C:\Users\<USER>\SparkGrid-services\bot\_wrapper.ps1"

# ── Kill any wrapper still alive from a previous launch ──────────────────
# Match the bot's full wrapper path (NOT software's _wrapper.ps1) so we
# don't accidentally kill the software service.
$staleWrappers = Get-CimInstance Win32_Process -Filter "Name = 'powershell.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine -like "*SparkGrid-services\bot\_wrapper.ps1*" }
if ($staleWrappers) {
    foreach ($w in $staleWrappers) {
        Write-Host "Найдена старая обёртка (PID $($w.ProcessId)) — останавливаю."
        # Kill its python children first so they can't outlive the wrapper.
        Get-CimInstance Win32_Process -Filter "ParentProcessId = $($w.ProcessId)" -ErrorAction SilentlyContinue |
            ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
        Stop-Process -Id $w.ProcessId -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 3
}

# Auto-restart wrapper script (runs hidden, restarts on crash with 10s delay)
# Crash-loop guard: 5 fast crashes (<120s each) stops auto-restart
$wrapperScript = @"
`$ErrorActionPreference = 'SilentlyContinue'
`$crashCount = 0
while (`$true) {
    `Write-Output "``[`$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] Starting Telegram Bot..."
    `$startTime = `Get-Date
    `& "$exe" "$scriptArgs" 2>&1
    `$elapsed = (`Get-Date) - `$startTime
    if (`$elapsed.TotalSeconds -ge 120) {
        `$crashCount = 0
    } else {
        `$crashCount++
        `Write-Output "``[`$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] Short run (`$([int]`$elapsed.TotalSeconds)s) — crash `$crashCount/5"
    }
    if (`$crashCount -ge 5) {
        `Write-Output "``[`$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] Crash-loop: 5 быстрых падений подряд, автоперезапуск остановлен."
        break
    }
    `Write-Output "``[`$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] Bot exited (code `$LASTEXITCODE). Restarting in 10s..."
    `Start-Sleep -Seconds 10
}
"@

# Check if already running
if (Test-Path $pidFile) {
    $oldPid = Get-Content $pidFile -ErrorAction SilentlyContinue
    if ($oldPid -and (Get-Process -Id $oldPid -ErrorAction SilentlyContinue)) {
        Write-Host "Уже запущено, PID $oldPid."
        exit 0
    }
}

# Write wrapper to temp file
$wrapperScript | Out-File -FilePath $wrapperFile -Encoding UTF8 -Force

$proc = Start-Process -FilePath "powershell.exe" `
    -ArgumentList "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $wrapperFile `
    -WorkingDirectory $workDir `
    -WindowStyle Hidden `
    -RedirectStandardOutput $logFile `
    -RedirectStandardError "$logFile.err" `
    -PassThru

$proc.Id | Out-File -FilePath $pidFile -Encoding ascii -Force
Write-Host "Запущено. PID: $($proc.Id)"
Write-Host "Лог: $logFile"
Write-Host "Ошибки: $logFile.err"
'@

# ─── stop_bot.ps1 ───────────────────────────────────────────────────────────────
$stopBot = @'
# SparkGrid Telegram Bot — stop script
# Recursively kills the full process tree and waits for Telegram settle.

$pidFile = "C:\Users\<USER>\SparkGrid-services\bot\run.pid"
$postKillSettleSeconds = 5
$deathTimeoutSeconds = 20

function Get-DescendantProcessIds {
    param([int]$RootPid)
    $allProcesses = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Select-Object ProcessId, ParentProcessId
    $all = @($RootPid)
    $frontier = @($RootPid)
    while ($frontier.Count -gt 0) {
        $children = $allProcesses |
            Where-Object { $frontier -contains $_.ParentProcessId } |
            Select-Object -ExpandProperty ProcessId
        $newOnes = $children | Where-Object { $all -notcontains $_ }
        if (-not $newOnes) { break }
        $all += $newOnes
        $frontier = $newOnes
    }
    return $all
}

function Wait-ForDeath {
    param([int[]]$ProcessIds, [int]$TimeoutSeconds)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        $stillAlive = $ProcessIds | Where-Object { Get-Process -Id $_ -ErrorAction SilentlyContinue }
        if (-not $stillAlive) { return $true }
        Start-Sleep -Milliseconds 500
    }
    $remaining = $ProcessIds | Where-Object { Get-Process -Id $_ -ErrorAction SilentlyContinue }
    return $remaining.Count -eq 0
}

if (-not (Test-Path $pidFile)) {
    Write-Host "PID-файл не найден — бот, судя по всему, уже остановлен."
    exit 0
}

$targetPid = Get-Content $pidFile -ErrorAction SilentlyContinue
if (-not $targetPid) {
    Write-Host "PID-файл пуст."
    Remove-Item $pidFile -ErrorAction SilentlyContinue
    exit 0
}

$targetPid = [int]$targetPid
$rootProc = Get-Process -Id $targetPid -ErrorAction SilentlyContinue

if ($rootProc) {
    $tree = Get-DescendantProcessIds -RootPid $targetPid
    Write-Host "Дерево процессов на остановку: $($tree -join ', ')"

    foreach ($procId in $tree) {
        Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
    }

    $confirmed = Wait-ForDeath -ProcessIds $tree -TimeoutSeconds $deathTimeoutSeconds
    if ($confirmed) {
        Write-Host "Все процессы дерева подтверждённо остановлены."
    } else {
        $stuck = $tree | Where-Object { Get-Process -Id $_ -ErrorAction SilentlyContinue }
        Write-Host "ВНИМАНИЕ: не дождались смерти PID: $($stuck -join ', ') за $deathTimeoutSeconds сек."
    }

    Write-Host "Пауза $postKillSettleSeconds сек — даём Telegram признать старое long-poll соединение закрытым."
    Start-Sleep -Seconds $postKillSettleSeconds
} else {
    Write-Host "Процесс с PID $targetPid уже не висит."
}

Remove-Item $pidFile -ErrorAction SilentlyContinue
Write-Host "Готово."
'@

# Replace <USER> placeholders with current user
$startSoftware = $startSoftware.Replace('<USER>', $script:currentUser)
$stopSoftware  = $stopSoftware.Replace('<USER>', $script:currentUser)
$startBot      = $startBot.Replace('<USER>', $script:currentUser)
$stopBot       = $stopBot.Replace('<USER>', $script:currentUser)

# Write all scripts with UTF-8 BOM
$scriptFiles = @{
    (Join-Path $script:softwareDir 'start_software.ps1') = $startSoftware
    (Join-Path $script:softwareDir 'stop_software.ps1')  = $stopSoftware
    (Join-Path $script:botDir 'start_bot.ps1')            = $startBot
    (Join-Path $script:botDir 'stop_bot.ps1')             = $stopBot
}

foreach ($kv in $scriptFiles.GetEnumerator()) {
    Write-FileBOM -Path $kv.Key -Content $kv.Value
    Write-OK "  $($kv.Key) — записан (UTF-8 BOM)"
}

# ══════════════════════════════════════════════════════════════════════════════
# STEP 7: Copy patches to install dir
# ══════════════════════════════════════════════════════════════════════════════
Write-Step 'Шаг 7: Копирование патчей в директорию установки…'

if (Test-Path $script:patchesSrc) {
    # Copy *.py to _internal/
    $pyFiles = Get-ChildItem -Path $script:patchesSrc -Filter '*.py' -File -ErrorAction SilentlyContinue
    foreach ($f in $pyFiles) {
        $dest = Join-Path $script:internalDir $f.Name
        Copy-Item $f.FullName $dest -Force
        Write-OK "  $($f.Name) → _internal\"
    }
    Write-OK "Скопировано $($pyFiles.Count) .py файлов."

    # Copy *.html to _internal/
    $htmlFiles = Get-ChildItem -Path $script:patchesSrc -Filter '*.html' -File -ErrorAction SilentlyContinue
    foreach ($f in $htmlFiles) {
        $dest = Join-Path $script:internalDir $f.Name
        Copy-Item $f.FullName $dest -Force
        Write-OK "  $($f.Name) → _internal\"
    }

    # Copy patches/ui/*.html to _internal/ui/
    $uiDir = Join-Path $script:patchesSrc 'ui'
    if (Test-Path $uiDir) {
        $uiDest = Join-Path $script:internalDir 'ui'
        if (-not (Test-Path $uiDest)) {
            New-Item -Path $uiDest -ItemType Directory -Force | Out-Null
        }
        $uiFiles = Get-ChildItem -Path $uiDir -Filter '*.html' -File -ErrorAction SilentlyContinue
        foreach ($f in $uiFiles) {
            $dest = Join-Path $uiDest $f.Name
            Copy-Item $f.FullName $dest -Force
            Write-OK "  ui\$($f.Name) → _internal\ui\"
        }
        Write-OK "Скопировано $($uiFiles.Count) ui/*.html файлов."
    } else {
        Write-Skip '  patches/ui/ не найден — пропуск.'
    }
} else {
    Write-Warn2 "Директория patches/ не найдена по пути $script:patchesSrc — пропуск копирования."
}

# ══════════════════════════════════════════════════════════════════════════════
# STEP 8: Set ExecutionPolicy
# ══════════════════════════════════════════════════════════════════════════════
Write-Step 'Шаг 8: Настройка ExecutionPolicy…'
try {
    $currentPolicy = Get-ExecutionPolicy -Scope CurrentUser -ErrorAction Stop
    if ($currentPolicy -eq 'RemoteSigned' -or $currentPolicy -eq 'Bypass' -or $currentPolicy -eq 'Unrestricted') {
        Write-Skip "  ExecutionPolicy уже = $currentPolicy (CurrentUser)"
    } else {
        Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser -Force -ErrorAction Stop
        Write-OK '  ExecutionPolicy = RemoteSigned (CurrentUser)'
    }
} catch {
    Write-Warn2 "  Не удалось установить ExecutionPolicy: $_"
}

# ══════════════════════════════════════════════════════════════════════════════
# STEP 9: Disable screen saver + power timeouts
# ══════════════════════════════════════════════════════════════════════════════
Write-Step 'Шаг 9: Отключение скринсейвера и тайм-аутов питания…'

try {
    reg add "HKCU\Control Panel\Desktop" /v ScreenSaveActive /t REG_SZ /d 0 /f 2>&1 | Out-Null
    Write-OK '  ScreenSaveActive = 0'
} catch {
    Write-Warn2 "  Не удалось отключить скринсейвер: $_"
}

try {
    powercfg /change monitor-timeout-ac 0 2>&1 | Out-Null
    Write-OK '  monitor-timeout-ac = 0'
} catch {
    Write-Warn2 "  Не удалось установить monitor-timeout-ac: $_"
}

try {
    powercfg /change standby-timeout-ac 0 2>&1 | Out-Null
    Write-OK '  standby-timeout-ac = 0'
} catch {
    Write-Warn2 "  Не удалось установить standby-timeout-ac: $_"
}

# ══════════════════════════════════════════════════════════════════════════════
# STEP 10: Create START_ALL.bat and STOP_ALL.bat shortcuts on Desktop
# ══════════════════════════════════════════════════════════════════════════════
Write-Step 'Шаг 10: Создание ярлыков START_ALL.bat и STOP_ALL.bat на рабочем столе…'

$desktopPath = [System.Environment]::GetFolderPath('Desktop')

$startAllBat = Join-Path $desktopPath 'START_ALL.bat'
$stopAllBat  = Join-Path $desktopPath 'STOP_ALL.bat'

$startAllContent = @"
@echo off
chcp 65001 >nul
echo === SparkGrid: Запуск служб ===
echo.
echo [1/2] Запуск Software (Server)...
powershell -NoProfile -ExecutionPolicy Bypass -File "$script:softwareDir\start_software.ps1"
echo.
echo [2/2] Запуск Telegram Bot...
timeout /t 3 /nobreak >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "$script:botDir\start_bot.ps1"
echo.
echo === Запущено ===
echo Откройте http://127.0.0.1:8770 в браузере.
pause
"@

$stopAllContent = @"
@echo off
chcp 65001 >nul
echo === SparkGrid: Остановка служб ===
echo.
echo [1/2] Остановка Telegram Bot...
powershell -NoProfile -ExecutionPolicy Bypass -File "$script:botDir\stop_bot.ps1"
echo.
echo [2/2] Остановка Software (Server)...
powershell -NoProfile -ExecutionPolicy Bypass -File "$script:softwareDir\stop_software.ps1"
echo.
echo === Остановлено ===
pause
"@

# Write .bat files with UTF-8 BOM (they contain Russian text)
Write-FileBOM -Path $startAllBat -Content $startAllContent
Write-FileBOM -Path $stopAllBat -Content $stopAllContent
Write-OK "  $startAllBat — создан"
Write-OK "  $stopAllBat — создан"

# ══════════════════════════════════════════════════════════════════════════════
# DONE
# ══════════════════════════════════════════════════════════════════════════════
Write-Host ''
Write-Host '════════════════════════════════════════════════════════════════' -ForegroundColor Green
Write-OK 'Установка SparkGrid Web Client завершена!'
Write-Host '════════════════════════════════════════════════════════════════' -ForegroundColor Green
Write-Host ''
Write-Host 'Откройте http://127.0.0.1:8770/setup и введите ключи' -ForegroundColor Yellow
Write-Host ''
