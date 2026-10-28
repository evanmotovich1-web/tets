# Evan Algo · 1-Command Windows PowerShell Installer
# Usage: irm https://raw.githubusercontent.com/evanmotovich1-web/tets/main/install.ps1 | iex
$ErrorActionPreference = "Stop"

Write-Host "================================================================================" -ForegroundColor Cyan
Write-Host "  EVAN ALGO · 1-COMMAND WINDOWS INSTALLER + HERMES AI MODEL WIZARD" -ForegroundColor Green
Write-Host "================================================================================" -ForegroundColor Cyan

# 1. Locate or clone the repository
$ScriptDir = if ($PSScriptRoot) { $PSScriptRoot } else { (Get-Location).Path }
if (Test-Path (Join-Path $ScriptDir "scripts\runner.py")) {
    $AlgoRoot = $ScriptDir
} else {
    $AlgoRoot = if ($env:TETS_INSTALL_DIR) { $env:TETS_INSTALL_DIR } else { Join-Path $HOME "evan-algo" }
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        Write-Host "Installing Git via winget..." -ForegroundColor Yellow
        winget install --id Git.Git -e --source winget --accept-package-agreements --accept-source-agreements
        $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
    }
    if (-not (Test-Path (Join-Path $AlgoRoot ".git"))) {
        Write-Host "Cloning https://github.com/evanmotovich1-web/tets.git -> $AlgoRoot ..." -ForegroundColor Cyan
        git clone https://github.com/evanmotovich1-web/tets.git $AlgoRoot
    } else {
        Push-Location $AlgoRoot
        git pull --rebase origin main
        Pop-Location
    }
}

Set-Location $AlgoRoot

# 2. Ensure uv is installed for fast Python 3.14 + dependency provisioning
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "Installing uv Python manager..." -ForegroundColor Cyan
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    $env:Path = "$HOME\.local\bin;$HOME\.cargo\bin;" + $env:Path
}

# 3. Create Python 3.14 virtual environments & sync dependencies
$VenvDir = Join-Path $AlgoRoot "desk\backtest\.venv"
$ShadowVenvDir = Join-Path $AlgoRoot "desk\runtime\.state\shadow\.venv"
$PaperVenvDir = Join-Path $AlgoRoot "desk\runtime\.state\paper\.venv"

if (-not (Test-Path $VenvDir)) {
    Write-Host "Creating Python 3.14 environment..." -ForegroundColor Cyan
    uv venv --python 3.14 $VenvDir
}
$PyBin = Join-Path $VenvDir "Scripts\python.exe"
if (-not (Test-Path $PyBin)) {
    $PyBin = Join-Path $VenvDir "bin\python"
}

Write-Host "Installing locked Python packages (numpy, pandas, backtesting, yfinance, rfc8785)..." -ForegroundColor Cyan
uv pip install --python $PyBin numpy pandas backtesting yfinance rfc8785

foreach ($SubVenv in @($ShadowVenvDir, $PaperVenvDir)) {
    if (-not (Test-Path $SubVenv)) {
        New-Item -ItemType Directory -Force -Path (Split-Path $SubVenv -Parent) | Out-Null
        uv venv --python 3.14 $SubVenv
        $SubPy = Join-Path $SubVenv "Scripts\python.exe"
        if (-not (Test-Path $SubPy)) { $SubPy = Join-Path $SubVenv "bin\python" }
        uv pip install --python $SubPy numpy pandas backtesting yfinance rfc8785
    }
}

# 4. Create Windows CLI shims (algo.cmd, adw.cmd, tets.cmd) in ~/.local/bin
$LocalBin = Join-Path $HOME ".local\bin"
New-Item -ItemType Directory -Force -Path $LocalBin | Out-Null
Set-Content -Path (Join-Path $LocalBin "algo.cmd") -Value "@echo off`r`n`"$PyBin`" `"$AlgoRoot\scripts\harness2_evolve.py`" %*"
Set-Content -Path (Join-Path $LocalBin "adw.cmd") -Value "@echo off`r`nif `"%~1`"==`"algo`" shift`r`n`"$PyBin`" `"$AlgoRoot\scripts\harness2_evolve.py`" %*"
Set-Content -Path (Join-Path $LocalBin "tets.cmd") -Value "@echo off`r`n`"$PyBin`" `"$AlgoRoot\scripts\runner.py`" %*"

# 5. Run Initial Setup & Hermes-Style Interactive AI Model & Subscription Connect Wizard
New-Item -ItemType Directory -Force -Path (Join-Path $AlgoRoot "desk\runtime\.state\tets") | Out-Null
& $PyBin (Join-Path $AlgoRoot "scripts\onboarding.py")
if ($env:TETS_NO_MODEL_WIZARD -ne "1") {
    & $PyBin (Join-Path $AlgoRoot "scripts\harness2_evolve.py") model wizard
}

# 6. Pre-seed Trade Workflow, 6 Presets, LangGraph State-Graph, 12-Account Fleet & 10-Day Backtest Explorer
Write-Host "Seeding 6-Agent Trade Workflow, LangGraph State-Graph & 10-Day Backtest Explorer..." -ForegroundColor Cyan
& $PyBin (Join-Path $AlgoRoot "scripts\harness2_evolve.py") visual | Out-Null

# 7. Launch the Full-Screen Terminal UI
if ($env:TETS_NO_START -ne "1") {
    Write-Host "Launching Evan Algo Full-Screen Terminal..." -ForegroundColor Green
    & $PyBin (Join-Path $AlgoRoot "scripts\runner.py") dashboard
} else {
    Write-Host "Installed! Run: & '$PyBin' '$AlgoRoot\scripts\runner.py' dashboard" -ForegroundColor Green
}
