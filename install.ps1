# VibeForma one-command installer bootstrap.
#
# Default install (PowerShell, Windows 10+):
#   irm https://raw.githubusercontent.com/FormaMindPro/vibeforma/main/install.ps1 | iex
# With options:
#   iex "& { $(irm https://raw.githubusercontent.com/FormaMindPro/vibeforma/main/install.ps1) } -Model large-v3 -NoRun"
# From cmd.exe:
#   powershell -c "irm https://raw.githubusercontent.com/FormaMindPro/vibeforma/main/install.ps1 | iex"
#
# The bootstrap downloads the latest release zip (or the main branch archive),
# unpacks it and runs install.py, which does the real work (venv, pip, model,
# shortcuts, launch). Without -Model the model is chosen inside install.py
# (a choice dialog on an interactive run).
param(
    [string]$Model = "",
    [switch]$NoRun,
    [switch]$Cpu,
    [switch]$DryRun
)
$ErrorActionPreference = "Stop"
try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor 3072 } catch {}

function Write-Step($m) { Write-Host "[*] $m" }

# -- python -----------------------------------------------------------------
$pyCmd = $null
$pyArgs = @()
foreach ($c in @("python", "py")) {
    try {
        & $c --version 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) { $pyCmd = $c; break }
    } catch {}
}
if (-not $pyCmd) {
    Write-Host "[X] Python not found. Install Python 3.10+ from https://www.python.org/downloads/ (tick 'Add python.exe to PATH') and run the command again."
    exit 1
}
if ($pyCmd -eq "py") { $pyArgs = @("-3") }
Write-Step "Python found: $pyCmd"

# -- download -----------------------------------------------------------------
$zipUrl = $null
try {
    $rel = Invoke-RestMethod -Uri "https://api.github.com/repos/FormaMindPro/vibeforma/releases/latest"
    $asset = $rel.assets | Where-Object { $_.name -match '^VibeForma-.*\.zip$' } | Select-Object -First 1
    if ($asset) {
        $zipUrl = $asset.browser_download_url
        Write-Step "Downloading release $($rel.tag_name)"
    }
} catch {}
if (-not $zipUrl) {
    $zipUrl = "https://codeload.github.com/FormaMindPro/vibeforma/zip/refs/heads/main"
    Write-Step "Downloading the main branch (no release asset found)"
}
$tmp = Join-Path $env:TEMP ("VibeForma-install-" + [guid]::NewGuid().ToString("N").Substring(0, 8))
New-Item -ItemType Directory -Path $tmp | Out-Null
$zipPath = Join-Path $tmp "vibeforma.zip"
Invoke-WebRequest -Uri $zipUrl -OutFile $zipPath -UseBasicParsing
Write-Step "Unpacking"
Expand-Archive -Path $zipPath -DestinationPath $tmp -Force
$src = Get-ChildItem -Path $tmp -Directory |
    Where-Object { Test-Path (Join-Path $_.FullName "install.py") } |
    Select-Object -First 1
if (-not $src) {
    Write-Host "[X] install.py not found in the downloaded archive."
    exit 1
}
$srcPath = $src.FullName

# -- run the real installer ------------------------------------------------------
Write-Step "Running install.py from $srcPath"
Push-Location $srcPath
$installArgs = @("install.py")
if ($Model -ne "") { $installArgs += @("--model", $Model) }
if ($Cpu) { $installArgs += "--cpu" }
if ($DryRun) { $installArgs += "--dry-run" }
if ($NoRun) { $installArgs += "--no-run" }
& $pyCmd @pyArgs @installArgs
$rc = $LASTEXITCODE
Pop-Location

if ($rc -eq 0) {
    Remove-Item -Path $tmp -Recurse -Force
    Write-Host ""
    Write-Host "VibeForma is installed. Hotkey: Ctrl+Alt+Space (by default)."
} else {
    Write-Host ""
    Write-Host "[X] Installer exited with code $rc. Files kept at: $srcPath"
    exit $rc
}
