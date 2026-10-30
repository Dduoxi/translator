<#
    Wrap dist\Translator into a single setup .exe.

    Requires Inno Setup 6:  https://jrsoftware.org/isdl.php
    Usage:
        powershell -ExecutionPolicy Bypass -File packaging\make_installer.ps1
#>
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$iscc = @(
    "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    "C:\Program Files\Inno Setup 6\ISCC.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1

if (-not $iscc) {
    throw "Inno Setup 6 not found. Install it from https://jrsoftware.org/isdl.php"
}
if (-not (Test-Path "dist\Translator\Translator.exe")) {
    throw "dist\Translator missing. Run packaging\build_windows.ps1 first."
}

& $iscc "packaging\installer.iss"
Write-Host "done: dist\installer\Translator-Setup.exe" -ForegroundColor Green
