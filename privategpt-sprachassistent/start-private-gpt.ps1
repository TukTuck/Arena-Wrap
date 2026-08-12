<#
.SYNOPSIS
  Start, stop, status or repair PrivateGPT (native install, Windows + Ollama).

.DESCRIPTION
  - Verifies and repairs the three Windows patches (fcntl stub, magic loader,
    qdrant client builder) from ~\private-gpt-patches.
  - Checks Ollama on port 11434 and starts it if needed.
  - Starts `private-gpt serve --port 8080` with a cleared PORT env var
    (the Freebuff/agent environment sets PORT=62712, which private-gpt would
    otherwise inherit and fail to bind).
  - Logs go to %USERPROFILE%\pgpt.log (stdout) and pgpt.err.log (stderr).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File start-private-gpt.ps1 -Action Start
  powershell -ExecutionPolicy Bypass -File start-private-gpt.ps1 -Action Stop
  powershell -ExecutionPolicy Bypass -File start-private-gpt.ps1 -Action Status
  powershell -ExecutionPolicy Bypass -File start-private-gpt.ps1 -Action Repair
#>
param(
    [ValidateSet("Start", "Stop", "Status", "Restart", "Repair")]
    [string]$Action = "Start"
)

$ErrorActionPreference = "Stop"

$PrivateGptExe = Join-Path $env:USERPROFILE ".local\bin\private-gpt.exe"
$SitePackages  = Join-Path $env:APPDATA "uv\tools\private-gpt\Lib\site-packages"
$PatchesDir    = "C:\Users\Hansi\Arena Wrap\privategpt-sprachassistent\private-gpt-patches"
$StdoutLog     = Join-Path $env:USERPROFILE "pgpt.log"
$StderrLog     = Join-Path $env:USERPROFILE "pgpt.err.log"
$Port          = 8080
$HealthUrl     = "http://localhost:$Port/health"
$ModelsUrl     = "http://localhost:$Port/v1/models"
$OllamaUrl     = "http://localhost:11434/api/tags"

function Get-PrivateGptProcess {
    $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($conn) {
        $p = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue
        if ($p -and $p.ProcessName -like "private-gpt*") { return $p }
    }
    return Get-Process -Name "private-gpt" -ErrorAction SilentlyContinue | Select-Object -First 1
}

function Test-Ollama {
    try {
        Invoke-RestMethod -Uri $OllamaUrl -Method Get -TimeoutSec 5 | Out-Null
        return $true
    } catch {
        return $false
    }
}

function Test-Patches {
    $issues = @()
    if (-not (Test-Path (Join-Path $SitePackages "fcntl.py"))) {
        $issues += "fcntl stub missing"
    }
    if (-not (Select-String -Path (Join-Path $SitePackages "magic\loader.py") -Pattern "PATCHED for Windows" -Quiet -ErrorAction SilentlyContinue)) {
        $issues += "magic loader not patched"
    }
    if (-not (Select-String -Path (Join-Path $SitePackages "private_gpt\components\vector_store\qdrant_client_builder.py") -Pattern "never used in local mode" -Quiet -ErrorAction SilentlyContinue)) {
        $issues += "qdrant client builder not patched"
    }
    return $issues
}

function Repair-Patches {
    $issues = Test-Patches
    if (-not $issues) {
        Write-Host "[ok] All Windows patches present." -ForegroundColor Green
        return
    }
    Write-Host "[!] Missing patches: $($issues -join '; ')" -ForegroundColor Yellow
    if (-not (Test-Path $PatchesDir)) {
        throw "Patch directory not found: $PatchesDir"
    }
    Copy-Item (Join-Path $PatchesDir "fcntl.py") $SitePackages -Force
    Copy-Item (Join-Path $PatchesDir "magic_loader.py") (Join-Path $SitePackages "magic\loader.py") -Force
    Copy-Item (Join-Path $PatchesDir "qdrant_client_builder.py") (Join-Path $SitePackages "private_gpt\components\vector_store\qdrant_client_builder.py") -Force
    $remaining = Test-Patches
    if ($remaining) {
        throw "Patches could not be applied completely: $($remaining -join '; ')"
    }
    Write-Host "[ok] Windows patches repaired." -ForegroundColor Green
}

function Ensure-Ollama {
    if (Test-Ollama) {
        Write-Host "[ok] Ollama is running (port 11434)." -ForegroundColor Green
        return
    }
    Write-Host "[!] Ollama not reachable - trying to start it ..." -ForegroundColor Yellow
    $ollamaCmd = Get-Command ollama -ErrorAction SilentlyContinue
    if (-not $ollamaCmd) {
        throw "Ollama executable not found. Please start Ollama manually."
    }
    Start-Process -FilePath $ollamaCmd.Source -ArgumentList "serve" -WindowStyle Hidden
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Seconds 1
        if (Test-Ollama) {
            Write-Host "[ok] Ollama started." -ForegroundColor Green
            return
        }
    }
    throw "Ollama did not become reachable on port 11434."
}

function Start-PrivateGpt {
    $existing = Get-PrivateGptProcess
    if ($existing) {
        Write-Host "[!] private-gpt is already running (PID $($existing.Id), port $Port)." -ForegroundColor Yellow
        return
    }
    if (-not (Test-Path $PrivateGptExe)) {
        throw "private-gpt executable not found: $PrivateGptExe (install with: uv tool install --python 3.11 --find-links https://wheels.privategpt.dev/packages/ 'private-gpt[core,qdrant]')"
    }
    Ensure-Ollama
    Repair-Patches

    # Clear PORT so private-gpt does not inherit e.g. PORT=62712 from this shell.
    $env:PORT = ""
    $env:HF_HUB_OFFLINE = "1"
    $env:OPENAI_API_BASE = "http://localhost:11434/v1"
    $env:OPENAI_EMBEDDING_API_BASE = "http://localhost:11434/v1"

    # Start via cmd.exe so the redirects happen inside cmd (no PowerShell
    # pipe handles to block on when launched from a non-interactive shell).
    $cmdLine = '""{0}" serve --port {1} >> "{2}" 2>> "{3}""' -f $PrivateGptExe, $Port, $StdoutLog, $StderrLog
    $p = Start-Process -FilePath "cmd.exe" -ArgumentList @("/c", $cmdLine) `
        -WindowStyle Hidden -PassThru
    Write-Host "[.] private-gpt starting (cmd PID $($p.Id)) - waiting for /health ..."

    for ($i = 0; $i -lt 150; $i++) {
        Start-Sleep -Seconds 1
        try {
            $h = Invoke-RestMethod -Uri $HealthUrl -Method Get -TimeoutSec 3
            if ($h.status -eq "ok") {
                Write-Host "[ok] Server ready: http://localhost:$Port  (UI: http://localhost:$Port/ui)" -ForegroundColor Green
                Write-Host "     Logs: $StdoutLog / $StderrLog"
                return
            }
        } catch {
            # not ready yet
        }
        if (($i + 1) % 15 -eq 0) {
            Write-Host "     ... still waiting ($($i + 1)s)"
        }
    }
    throw "Server did not become ready in time. Check log: $StderrLog"
}

function Stop-PrivateGpt {
    $proc = Get-PrivateGptProcess
    if (-not $proc) {
        Write-Host "[!] private-gpt is not running." -ForegroundColor Yellow
        return
    }
    Stop-Process -Id $proc.Id -Force
    Write-Host "[ok] private-gpt stopped (PID $($proc.Id))." -ForegroundColor Green
}

function Show-Status {
    $proc = Get-PrivateGptProcess
    if ($proc) {
        Write-Host "[ok] Status: running (PID $($proc.Id), port $Port)" -ForegroundColor Green
        try {
            $h = Invoke-RestMethod -Uri $HealthUrl -Method Get -TimeoutSec 3
            Write-Host "     Health: $($h.status)"
        } catch {
            Write-Host "[!] Health: not reachable" -ForegroundColor Yellow
        }
        try {
            $m = Invoke-RestMethod -Uri $ModelsUrl -Method Get -TimeoutSec 5
            Write-Host "     Registered models: $($m.data.Count)"
        } catch {
            Write-Host "[!] Could not list models." -ForegroundColor Yellow
        }
    } else {
        Write-Host "[!] Status: stopped" -ForegroundColor Yellow
    }

    $issues = Test-Patches
    if ($issues) {
        Write-Host "[!] Patches MISSING: $($issues -join '; ')  (run -Action Repair)" -ForegroundColor Red
    } else {
        Write-Host "[ok] Patches: all present" -ForegroundColor Green
    }

    if (Test-Ollama) {
        Write-Host "[ok] Ollama: running" -ForegroundColor Green
    } else {
        Write-Host "[!] Ollama: not running" -ForegroundColor Yellow
    }
}

switch ($Action) {
    "Start"   { Start-PrivateGpt }
    "Stop"    { Stop-PrivateGpt }
    "Status"  { Show-Status }
    "Restart" { Stop-PrivateGpt; Start-Sleep -Seconds 2; Start-PrivateGpt }
    "Repair"  { Repair-Patches }
}
