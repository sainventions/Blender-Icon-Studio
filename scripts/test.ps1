<#
.SYNOPSIS
  Run the Blender Icon Studio test suites: pytest (no Blender), the optional real-Blender suite, the three.js
  viewport / UI node tests, the TypeScript check and a production Vite build.

.DESCRIPTION
  Steps (each one reports PASS / FAIL / SKIP; the script exits 1 if any step failed):
    1. pytest      .venv\Scripts\python.exe -m pytest -q tests --ignore=tests/blender   (Blender-free)
    2. blender     only with -Blender: pytest tests/blender + BIS_REAL_BLENDER=1 tests/test_api_integration.py
                   and tests/test_batch.py (their opt-in real-Blender cases)
                   (launches Blender 5.0 on the OptiX GPU; draft/preview renders <= 256 px)
    3. node        node --test src/viewport/tests/*.test.mjs   (viewport <-> worker parity, UI polish)
    4. tsc         TypeScript check of web/ (tsc -b --noEmit)
    5. vite        production build into a temporary folder (web/dist is left alone)

  Examples:
    scripts\test.ps1                 # everything except the GPU suite
    scripts\test.ps1 -Blender        # + real Blender 5.0
    scripts\test.ps1 -Only node,tsc  # just the web checks
    scripts\test.ps1 -PytestArgs '-k export'

.PARAMETER Blender     Also run the real-Blender suites (needs Blender 5.0 + an NVIDIA RTX GPU).
.PARAMETER Only        Run only these steps: pytest, blender, node, tsc, vite.
.PARAMETER Skip        Skip these steps.
.PARAMETER PytestArgs  Extra arguments for pytest (e.g. '-k export' or '-x').
.PARAMETER KeepBuild   Keep the temporary Vite build folder (its path is printed).
#>
[CmdletBinding()]
param(
    [switch]$Blender,
    [string[]]$Only = @(),
    [string[]]$Skip = @(),
    [string]$PytestArgs = '',
    [switch]$KeepBuild
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$Web = Join-Path $Root 'web'
$Py = Join-Path $Root '.venv\Scripts\python.exe'
$results = New-Object System.Collections.Generic.List[object]

function Write-Step([string]$Text) { Write-Host "==> $Text" -ForegroundColor Cyan }
function Write-Note([string]$Text) { Write-Host "    $Text" -ForegroundColor DarkGray }

function Test-Wanted([string]$Name) {
    $only = @($Only | ForEach-Object { $_ -split ',' } | Where-Object { $_ })
    $skip = @($Skip | ForEach-Object { $_ -split ',' } | Where-Object { $_ })
    if ($only.Count -gt 0 -and $only -notcontains $Name) { return $false }
    return ($skip -notcontains $Name)
}

function Invoke-Step([string]$Name, [string]$Title, [scriptblock]$Body) {
    if (-not (Test-Wanted $Name)) { $results.Add([pscustomobject]@{ Step = $Name; Result = 'SKIP'; Seconds = 0; Note = 'not selected' }); return }
    Write-Step $Title
    $script:SkipReason = $null
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $note = ''
    try {
        $global:LASTEXITCODE = 0
        & $Body | Out-Host
        $ok = ($LASTEXITCODE -eq 0)
        if (-not $ok) { $note = "exit code $LASTEXITCODE" }
    } catch {
        $ok = $false
        $note = $_.Exception.Message
        Write-Host "!!  $note" -ForegroundColor Yellow
    }
    $sw.Stop()
    $secs = [math]::Round($sw.Elapsed.TotalSeconds, 1)
    if ($script:SkipReason) {
        Write-Host "    skipped: $($script:SkipReason)" -ForegroundColor Yellow
        $results.Add([pscustomobject]@{ Step = $Name; Result = 'SKIP'; Seconds = $secs; Note = $script:SkipReason })
        return
    }
    $results.Add([pscustomobject]@{ Step = $Name; Result = $(if ($ok) { 'PASS' } else { 'FAIL' }); Seconds = $secs; Note = $note })
}

function Skip-Step([string]$Reason) { $script:SkipReason = $Reason }

function Get-Node {
    $node = Get-Command node.exe -ErrorAction SilentlyContinue
    if (-not $node) { Skip-Step 'Node.js not found (install Node 22+)'; return $null }
    if (-not (Test-Path (Join-Path $Web 'node_modules'))) {
        Skip-Step "web\node_modules is missing - run 'npm install' in web\ (or the launcher) first"
        return $null
    }
    return $node.Source
}

if (-not (Test-Path $Py)) {
    Write-Host "!!  No .venv - run 'Blender Icon Studio.cmd' (or scripts\start.ps1) once to create it." -ForegroundColor Yellow
}

# --------------------------------------------------------------------------------------------- 1. pytest
Invoke-Step 'pytest' 'pytest (Blender-free: SVG pipeline, API with a fake bridge, exports, styles, batch)' {
    if (-not (Test-Path $Py)) { Skip-Step 'no .venv'; return }
    $env:BIS_REAL_BLENDER = $null
    $extra = @($PytestArgs -split '\s+' | Where-Object { $_ })
    Push-Location $Root
    try { & $Py -m pytest -q tests --ignore=tests/blender @extra } finally { Pop-Location }
}

# --------------------------------------------------------------------------------------------- 2. blender
if ($Blender) {
    Invoke-Step 'blender' 'pytest with real Blender 5.0 (OptiX; small draft / preview renders)' {
        if (-not (Test-Path $Py)) { Skip-Step 'no .venv'; return }
        $exe = if ($env:BIS_BLENDER) { $env:BIS_BLENDER } else { 'C:\Program Files\Blender Foundation\Blender 5.0\blender.exe' }
        if (-not (Test-Path $exe)) { Skip-Step "Blender 5.0 not found at '$exe' (set BIS_BLENDER)"; return }
        $extra = @($PytestArgs -split '\s+' | Where-Object { $_ })
        Push-Location $Root
        try {
            & $Py -m pytest -q tests/blender @extra
            $first = $LASTEXITCODE
            $env:BIS_REAL_BLENDER = '1'
            try { & $Py -m pytest -q tests/test_api_integration.py tests/test_batch.py @extra } finally { $env:BIS_REAL_BLENDER = $null }
            if ($first -ne 0) { $global:LASTEXITCODE = $first }
        } finally { Pop-Location }
    }
} else {
    $results.Add([pscustomobject]@{ Step = 'blender'; Result = 'SKIP'; Seconds = 0; Note = 'pass -Blender to run' })
}

# --------------------------------------------------------------------------------------------- 3. node
Invoke-Step 'node' 'node --test src/viewport/tests/*.test.mjs (viewport parity + UI polish)' {
    $node = Get-Node
    if (-not $node) { return }
    Push-Location $Web
    try { & $node --test 'src/viewport/tests/*.test.mjs' } finally { Pop-Location }
}

# --------------------------------------------------------------------------------------------- 4. tsc
Invoke-Step 'tsc' 'TypeScript check (tsc -b --noEmit)' {
    $node = Get-Node
    if (-not $node) { return }
    Push-Location $Web
    try { & $node (Join-Path $Web 'node_modules\typescript\bin\tsc') -b --noEmit } finally { Pop-Location }
}

# --------------------------------------------------------------------------------------------- 5. vite
Invoke-Step 'vite' 'Vite production build (temporary folder)' {
    $node = Get-Node
    if (-not $node) { return }
    $out = Join-Path ([System.IO.Path]::GetTempPath()) ("bis-vite-check-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
    Push-Location $Web
    try {
        & $node (Join-Path $Web 'node_modules\vite\bin\vite.js') build --outDir $out --emptyOutDir --logLevel warn
        if ($LASTEXITCODE -eq 0 -and -not (Test-Path (Join-Path $out 'index.html'))) { throw "vite build produced no index.html" }
    } finally {
        Pop-Location
        if ($KeepBuild) { Write-Note "build kept at $out" }
        elseif (Test-Path $out) { Remove-Item -Recurse -Force $out -ErrorAction SilentlyContinue }
    }
}

# --------------------------------------------------------------------------------------------- summary
Write-Host ""
$results | Format-Table -AutoSize Step, Result, Seconds, Note | Out-String -Width 160 | Write-Host
$failed = @($results | Where-Object { $_.Result -eq 'FAIL' })
if ($failed.Count -gt 0) {
    Write-Host ("FAILED: " + (($failed | ForEach-Object { $_.Step }) -join ', ')) -ForegroundColor Red
    exit 1
}
Write-Host "All selected test steps passed." -ForegroundColor Green
exit 0
