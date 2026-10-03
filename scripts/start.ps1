<#
.SYNOPSIS
  Launch Blender Icon Studio: prepare the Python venv and the web build if needed, start the server on
  http://127.0.0.1:8420 (or reuse a running one) and open it as an Edge app window.

.DESCRIPTION
  Used by "Blender Icon Studio.cmd". This console window hosts the server: close it (or press Ctrl+C)
  to quit - the Blender worker processes are tied to the server and exit with it.

.PARAMETER Port      HTTP port (default 8420).
.PARAMETER NoBrowser Do not open a browser window.
.PARAMETER Rebuild   Force `npm run build` of the web UI.
.PARAMETER SkipBuild Never build the web UI (use web/dist as it is).
.PARAMETER Fake      Run without Blender (BIS_FAKE_BLENDER=1): flat Pillow previews, for UI work without a GPU.
.PARAMETER NoWorker  Do not start the persistent Blender worker at startup (it starts on the first render).
#>
[CmdletBinding()]
param(
    [int]$Port = 8420,
    [switch]$NoBrowser,
    [switch]$Rebuild,
    [switch]$SkipBuild,
    [switch]$Fake,
    [switch]$NoWorker
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$Url = "http://127.0.0.1:$Port"
$Host.UI.RawUI.WindowTitle = "Blender Icon Studio - server ($Url) - close this window to quit"

function Write-Step([string]$Text) { Write-Host "==> $Text" -ForegroundColor Cyan }
function Write-Note([string]$Text) { Write-Host "    $Text" -ForegroundColor DarkGray }
function Write-Warn([string]$Text) { Write-Host "!!  $Text" -ForegroundColor Yellow }

function Test-Studio([int]$P) {
    try {
        $r = Invoke-RestMethod -Uri "http://127.0.0.1:$P/api/health" -TimeoutSec 2
        return ($r.ok -eq $true)
    } catch { return $false }
}

function Open-StudioWindow([string]$Target) {
    if ($NoBrowser) { return }
    $candidates = @(
        (Join-Path ${env:ProgramFiles(x86)} 'Microsoft\Edge\Application\msedge.exe'),
        (Join-Path $env:ProgramFiles 'Microsoft\Edge\Application\msedge.exe'),
        (Join-Path $env:LOCALAPPDATA 'Microsoft\Edge\Application\msedge.exe')
    )
    $edge = $candidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
    if ($edge) {
        Write-Step "Opening Blender Icon Studio (Edge app window)"
        Start-Process -FilePath $edge -ArgumentList "--app=$Target", '--window-size=1680,1050'
    } else {
        Write-Step "Opening $Target in the default browser"
        Start-Process $Target
    }
}

# --------------------------------------------------------------------------------------------- reuse
if (Test-Studio $Port) {
    Write-Step "Blender Icon Studio is already running on $Url"
    Open-StudioWindow $Url
    exit 0
}
$busy = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($busy) {
    Write-Warn "Port $Port is used by another program (PID $($busy[0].OwningProcess)). Use -Port <n>."
    exit 1
}

# --------------------------------------------------------------------------------------------- python venv
$Py = Join-Path $Root '.venv\Scripts\python.exe'
$Req = Join-Path $Root 'requirements.txt'
$Stamp = Join-Path $Root '.venv\.bis-requirements.sha256'
if (-not (Test-Path $Py)) {
    Write-Step "Creating the Python virtual environment (.venv)"
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($launcher) {
        & py.exe -3.13 -m venv (Join-Path $Root '.venv')
        if ($LASTEXITCODE -ne 0) { & py.exe -3 -m venv (Join-Path $Root '.venv') }
    } else {
        $python = Get-Command python.exe -ErrorAction SilentlyContinue
        if (-not $python) { Write-Warn "Python 3.13 not found. Install it from python.org and re-run."; exit 1 }
        & $python.Source -m venv (Join-Path $Root '.venv')
    }
    if (-not (Test-Path $Py)) { Write-Warn "Could not create .venv"; exit 1 }
}
$reqHash = (Get-FileHash -Algorithm SHA256 $Req).Hash
$oldHash = if (Test-Path $Stamp) { (Get-Content $Stamp -Raw).Trim() } else { '' }
if ($reqHash -ne $oldHash) {
    Write-Step "Installing Python dependencies (requirements.txt)"
    & $Py -m pip install --disable-pip-version-check -q -r $Req
    if ($LASTEXITCODE -ne 0) { Write-Warn "pip install failed"; exit 1 }
    Set-Content -Path $Stamp -Value $reqHash -Encoding ascii
}

# --------------------------------------------------------------------------------------------- web build
$Web = Join-Path $Root 'web'
$DistIndex = Join-Path $Web 'dist\index.html'
$needBuild = $Rebuild -or -not (Test-Path $DistIndex)
if ($SkipBuild) { $needBuild = $false }
elseif (-not $needBuild) {
    $built = (Get-Item $DistIndex).LastWriteTimeUtc
    $inputs = @(Get-ChildItem -Path (Join-Path $Web 'src') -Recurse -File -ErrorAction SilentlyContinue)
    foreach ($f in 'index.html', 'package.json', 'vite.config.ts', 'tsconfig.json') {
        $p = Join-Path $Web $f
        if (Test-Path $p) { $inputs += Get-Item $p }
    }
    $newer = $inputs | Where-Object { $_.LastWriteTimeUtc -gt $built } | Select-Object -First 1
    if ($newer) { $needBuild = $true; Write-Note "web/dist is older than $($newer.Name)" }
}
if ($needBuild) {
    $npm = Get-Command npm.cmd -ErrorAction SilentlyContinue
    if (-not $npm) {
        Write-Warn "Node.js/npm not found - cannot build the web UI (install Node 22+). Starting the API only."
    } else {
        Push-Location $Web
        try {
            if (-not (Test-Path (Join-Path $Web 'node_modules'))) {
                Write-Step "Installing web dependencies (npm install)"
                & npm.cmd install --no-audit --no-fund
                if ($LASTEXITCODE -ne 0) { throw "npm install failed" }
            }
            Write-Step "Building the web UI (npm run build)"
            & npm.cmd run build
            if ($LASTEXITCODE -ne 0) {
                Write-Warn "Type-checked build failed; retrying with 'vite build' only"
                & npm.cmd exec -- vite build
                if ($LASTEXITCODE -ne 0) {
                    if (Test-Path $DistIndex) { Write-Warn "vite build failed - using the previous web/dist" }
                    else { Write-Warn "vite build failed - the UI is unavailable (API still works)" }
                }
            }
        } catch {
            Write-Warn $_.Exception.Message
        } finally {
            Pop-Location
        }
    }
}

# --------------------------------------------------------------------------------------------- server
$env:BIS_PORT = "$Port"
if ($Fake) { $env:BIS_FAKE_BLENDER = '1' }
if ($NoWorker) { $env:BIS_NO_WORKER = '1' }
$blender = if ($env:BIS_BLENDER) { $env:BIS_BLENDER } else { 'C:\Program Files\Blender Foundation\Blender 5.0\blender.exe' }
if (-not $Fake -and -not (Test-Path $blender)) {
    Write-Warn "Blender 5.0 not found at '$blender' - set BIS_BLENDER or install Blender 5.0. Renders will fail."
}

Write-Step "Starting the server on $Url"
$server = Start-Process -FilePath $Py -NoNewWindow -PassThru -WorkingDirectory $Root -ArgumentList @(
    '-m', 'uvicorn', 'bis.main:app', '--app-dir', 'server', '--host', '127.0.0.1', '--port', "$Port",
    '--log-level', 'warning'
)
$null = $server.Handle  # cache the handle: Windows PowerShell 5.1 only reports ExitCode when it was opened


$deadline = (Get-Date).AddSeconds(60)
while (-not (Test-Studio $Port)) {
    if ($server.HasExited) { Write-Warn "The server exited (code $($server.ExitCode))."; exit 1 }
    if ((Get-Date) -gt $deadline) { Write-Warn "The server did not answer within 60 s."; break }
    Start-Sleep -Milliseconds 250
}
try {
    $status = Invoke-RestMethod -Uri "$Url/api/system" -TimeoutSec 5
    Write-Note ("Blender: {0} {1} | GPU: {2} | worker: {3}" -f $status.blender.path, $status.blender.version,
        $status.gpu.name, $status.worker.state)
} catch { }

Open-StudioWindow $Url
Write-Host ""
Write-Host "Blender Icon Studio is running at $Url" -ForegroundColor Green
Write-Host "Close this window (or press Ctrl+C) to quit." -ForegroundColor Green
try {
    Wait-Process -Id $server.Id
} finally {
    if (-not $server.HasExited) { Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue }
}
