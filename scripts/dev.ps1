<#
.SYNOPSIS
  Development mode: FastAPI with --reload (new window) + Vite dev server with HMR (this window).

.DESCRIPTION
  API/WS/files on http://127.0.0.1:8420, UI on http://localhost:5173 (Vite proxies /api, /ws, /files).
  Close the server window / press Ctrl+C here to stop.

.PARAMETER Fake      BIS_FAKE_BLENDER=1 (no Blender / GPU needed).
.PARAMETER NoWorker  Do not start the persistent Blender worker at startup.
.PARAMETER NoBrowser Do not open the browser.
#>
[CmdletBinding()]
param([switch]$Fake, [switch]$NoWorker, [switch]$NoBrowser)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$Py = Join-Path $Root '.venv\Scripts\python.exe'
if (-not (Test-Path $Py)) { Write-Host "No .venv - run scripts\start.ps1 once first." -ForegroundColor Yellow; exit 1 }

$envPrefix = ''
if ($Fake) { $envPrefix += '$env:BIS_FAKE_BLENDER=''1''; ' }
if ($NoWorker) { $envPrefix += '$env:BIS_NO_WORKER=''1''; ' }
$serverCmd = $envPrefix + "Set-Location '$Root'; `$Host.UI.RawUI.WindowTitle = 'BIS server (reload) :8420'; " +
    "& '$Py' -m uvicorn bis.main:app --app-dir server --host 127.0.0.1 --port 8420 --reload --reload-dir server"
Write-Host "==> Starting the API server (auto-reload) in a new window" -ForegroundColor Cyan
Start-Process powershell.exe -ArgumentList @('-NoProfile', '-NoExit', '-Command', $serverCmd) | Out-Null

$Web = Join-Path $Root 'web'
if (-not (Test-Path (Join-Path $Web 'node_modules'))) {
    Write-Host "==> npm install" -ForegroundColor Cyan
    Push-Location $Web; & npm.cmd install --no-audit --no-fund; Pop-Location
}
if (-not $NoBrowser) {
    Start-Job -ScriptBlock {
        for ($i = 0; $i -lt 120; $i++) {
            try { Invoke-WebRequest -UseBasicParsing -Uri 'http://localhost:5173' -TimeoutSec 2 | Out-Null; break }
            catch { Start-Sleep -Milliseconds 500 }
        }
        Start-Process 'http://localhost:5173'
    } | Out-Null
}
Write-Host "==> Vite dev server on http://localhost:5173 (Ctrl+C to stop)" -ForegroundColor Cyan
Push-Location $Web
try { & npm.cmd run dev } finally { Pop-Location }
