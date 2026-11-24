# VibeForma one-command installer bootstrap.
#
# Default install (PowerShell, Windows 10+):
#   irm https://raw.githubusercontent.com/FormaMindPro/vibeforma/main/install.ps1 | iex
# With options:
#   iex "& { $(irm https://raw.githubusercontent.com/FormaMindPro/vibeforma/main/install.ps1) } -Model large-v3 -NoRun"
# From cmd.exe:
#   powershell -c "irm https://raw.githubusercontent.com/FormaMindPro/vibeforma/main/install.ps1 | iex"
#
# The bootstrap downloads the latest release zip, unpacks it and runs
# install.py, which does the real work (venv, pip, model, shortcuts,
# launch). Only a released build is installed: when no release zip is
# available the bootstrap stops and explains why (no main-branch fallback).
# Without -Model the model is chosen inside install.py (a choice dialog on
# an interactive run).
#
# The zip is verified against SHA256SUMS before anything is unpacked:
#  - normal path: both files are downloaded from the release;
#  - -ZipPath/-SumsPath: the app passes an already downloaded pair, so no
#    network is needed and -SumsPath is required (fail closed).
param(
    [string]$Model = "",
    [string]$Target = "",
    [string]$ZipPath = "",
    [string]$SumsPath = "",
    [switch]$NoRun,
    [switch]$Cpu,
    [switch]$DryRun
)
$ErrorActionPreference = "Stop"
try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor 3072 } catch {}

function Write-Step($m) { Write-Host "[*] $m" }

function Get-ExpectedHash($sumsFile, $name) {
    if (-not (Test-Path -LiteralPath $sumsFile -PathType Leaf)) { return $null }
    foreach ($line in Get-Content -LiteralPath $sumsFile) {
        $text = "$line".Trim()
        if ($text -eq "") { continue }
        $parts = $text -split '\s+', 2
        if ($parts.Count -ne 2) { continue }
        $fileName = $parts[1].Trim()
        if ($fileName.StartsWith("*")) { $fileName = $fileName.Substring(1) }
        if ($fileName -eq $name) { return $parts[0].Trim().ToLowerInvariant() }
    }
    return $null
}

function Test-ArchiveHash($archive, $sumsFile) {
    $name = Split-Path -Leaf $archive
    $expected = Get-ExpectedHash $sumsFile $name
    if (-not $expected) {
        Write-Host "[X] SHA256SUMS has no line for $name - nothing was unpacked."
        return $false
    }
    $sha = [System.Security.Cryptography.SHA256]::Create()
    $stream = [System.IO.File]::OpenRead($archive)
    try {
        $actual = ([System.BitConverter]::ToString($sha.ComputeHash($stream))).Replace("-", "").ToLowerInvariant()
    } finally {
        $stream.Dispose()
        $sha.Dispose()
    }
    if ($actual -ne $expected) {
        Write-Host "[X] SHA256 mismatch for $name - nothing was unpacked."
        Write-Host "    expected $expected"
        Write-Host "    actual   $actual"
        return $false
    }
    Write-Step "SHA256SUMS verified for $name"
    return $true
}

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

# -- release zip and its checksum --------------------------------------------
$tmp = Join-Path $env:TEMP ("VibeForma-install-" + [guid]::NewGuid().ToString("N").Substring(0, 8))
New-Item -ItemType Directory -Path $tmp | Out-Null
$archivePath = ""

if ($ZipPath -ne "") {
    if (-not (Test-Path -LiteralPath $ZipPath -PathType Leaf)) {
        Write-Host "[X] -ZipPath file not found: $ZipPath"
        exit 1
    }
    if ($SumsPath -eq "") {
        Write-Host "[X] -ZipPath requires -SumsPath: the archive is never unpacked unchecked."
        exit 1
    }
    if (-not (Test-Path -LiteralPath $SumsPath -PathType Leaf)) {
        Write-Host "[X] -SumsPath file not found: $SumsPath"
        exit 1
    }
    $archivePath = Join-Path $tmp (Split-Path -Leaf $ZipPath)
    Copy-Item -LiteralPath $ZipPath -Destination $archivePath
    if (-not (Test-ArchiveHash $archivePath $SumsPath)) { exit 1 }
} else {
    $zipUrl = $null
    $sumsUrl = $null
    $relError = $null
    try {
        $rel = Invoke-RestMethod -Uri "https://api.github.com/repos/FormaMindPro/vibeforma/releases/latest"
        $asset = $rel.assets | Where-Object { $_.name -match '^VibeForma-.*\.zip$' } | Select-Object -First 1
        $sumsAsset = $rel.assets | Where-Object { $_.name -eq "SHA256SUMS" } | Select-Object -First 1
        if ($asset) {
            $zipUrl = $asset.browser_download_url
            Write-Step "Downloading release $($rel.tag_name)"
        } else {
            $relError = "the latest release has no VibeForma zip asset"
        }
        if ($sumsAsset) {
            $sumsUrl = $sumsAsset.browser_download_url
        } elseif ($asset) {
            $relError = "the latest release has no SHA256SUMS asset"
            $zipUrl = $null
        }
    } catch {
        $relError = $_.Exception.Message
    }
    if (-not $zipUrl) {
        Write-Host "[X] No released VibeForma build was found. The bootstrap installs released versions only; there is no main-branch fallback."
        if ($relError) { Write-Host "    Reason: $relError" }
        Write-Host "    Check https://github.com/FormaMindPro/vibeforma/releases or run install.cmd from a cloned repository."
        exit 1
    }
    if (-not $sumsUrl) {
        Write-Host "[X] The release has no SHA256SUMS asset - nothing was unpacked."
        exit 1
    }
    $archivePath = Join-Path $tmp $asset.name
    Invoke-WebRequest -Uri $zipUrl -OutFile $archivePath -UseBasicParsing
    $sumsFile = Join-Path $tmp "SHA256SUMS"
    Invoke-WebRequest -Uri $sumsUrl -OutFile $sumsFile -UseBasicParsing
    if (-not (Test-ArchiveHash $archivePath $sumsFile)) { exit 1 }
}

Write-Step "Unpacking"
$extract = Join-Path $tmp "extract"
if (Test-Path -LiteralPath $extract) { Remove-Item -LiteralPath $extract -Recurse -Force }
Expand-Archive -Path $archivePath -DestinationPath $extract
$src = Get-ChildItem -Path $extract -Directory |
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
if ($Target -ne "") { $installArgs += @("--target", $Target) }
if ($Cpu) { $installArgs += "--cpu" }
if ($DryRun) { $installArgs += "--dry-run" }
if ($NoRun) { $installArgs += "--no-run" }
& $pyCmd @pyArgs @installArgs
$rc = $LASTEXITCODE
Pop-Location

if ($rc -eq 0) {
    Remove-Item -Path $tmp -Recurse -Force
    Write-Host ""
    if ($DryRun) {
        Write-Host "Dry run complete - nothing was installed."
    } else {
        Write-Host "VibeForma is installed. Hotkey: Ctrl+Alt+Space (by default)."
    }
} else {
    Write-Host ""
    Write-Host "[X] Installer exited with code $rc. Files kept at: $srcPath"
    exit $rc
}
