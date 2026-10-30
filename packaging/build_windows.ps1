<#
    One-command Windows build.

    Usage (PowerShell, from the repo root):
        powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1

    Produces dist\FpsTranslator\FpsTranslator.exe plus a start-menu-free folder
    you can copy anywhere. Run packaging\make_installer.ps1 afterwards if you
    want a single setup .exe.
#>
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

Write-Host "==> checking python" -ForegroundColor Cyan
$py = "py"
if (-not (Get-Command py -ErrorAction SilentlyContinue)) { $py = "python" }
& $py -c "import sys; assert sys.version_info[:2] in ((3,11),(3,12)), 'need 3.11 or 3.12'; print(sys.version)"

Write-Host "==> creating venv" -ForegroundColor Cyan
if (-not (Test-Path ".venv-build")) { & $py -m venv .venv-build }
$venvPy = ".\.venv-build\Scripts\python.exe"

Write-Host "==> installing dependencies" -ForegroundColor Cyan
& $venvPy -m pip install --upgrade pip
& $venvPy -m pip install -e ".[screen,ocr,translate,ui]"
& $venvPy -m pip install -r packaging\requirements-build.txt

# rapidocr depends on opencv-python (the GUI build), which drags in a whole
# private Qt5 (libQt5Gui/Widgets/Core, ~30 MB) that a subtitle overlay never
# uses. Swapping to the headless wheel drops it. Both wheels ship the same `cv2`
# package, so this must be an uninstall followed by a reinstall.
Write-Host "==> switching OpenCV to the headless wheel" -ForegroundColor Cyan
& $venvPy -m pip uninstall -y opencv-python
& $venvPy -m pip install --force-reinstall opencv-python-headless
& $venvPy -c "import cv2; print('cv2', cv2.__version__)"

Write-Host "==> smoke test (engines must register)" -ForegroundColor Cyan
& $venvPy -m fpst doctor

Write-Host "==> building" -ForegroundColor Cyan
& $venvPy -m PyInstaller --noconfirm --clean --distpath dist --workpath build packaging\fpst.spec

Write-Host ""
Write-Host "done: dist\FpsTranslator\FpsTranslator.exe" -ForegroundColor Green
Write-Host "first run: the wizard asks for a translation key, then you drag the chat area."
Write-Host "logs: %APPDATA%\FpsTranslator\logs\fpst.log"
