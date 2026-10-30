# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the Windows build.

Build on Windows:  pip install -r packaging/requirements-build.txt
                   pyinstaller packaging/translator.spec

Notes that matter for this app:

* RapidOCR ships its ONNX models and a config.yaml *inside the package*. Our
  engine reads that yaml by path and copies it to a temp file, so both the
  models directory and config.yaml must be collected as data.
* The app is windowed (tray program): a console window would flash on start.
  Diagnostics go to %APPDATA%\\Translator\\logs\\translator.log instead.
* PySide6 pulls in a lot we do not use; the excludes below cut roughly 100 MB.
"""

from PyInstaller.utils.hooks import collect_all, collect_submodules

datas = []
binaries = []
hiddenimports = []

# RapidOCR models + config.yaml live inside the wheel.
for package in ("rapidocr_onnxruntime",):
    pkg_datas, pkg_binaries, pkg_hidden = collect_all(package)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden

# keyring picks its backend at runtime; the Windows one must be present.
hiddenimports += collect_submodules("keyring.backends")

hiddenimports += [
    "translator.translate.baidu",
    "translator.translate.llm",
    "translator.translate.mock",
    "translator.ocr.rapidocr_engine",
    "translator.ocr.mock",
    "translator.capture.mss_source",
    "translator.capture.dxgi",
]

excludes = [
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick",
    "PySide6.Qt3DCore",
    "PySide6.QtMultimedia",
    "PySide6.QtQuick",
    "PySide6.QtQml",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtDesigner",
    "PySide6.QtTest",
    "tkinter",
    "matplotlib",
    "torch",
    "torchaudio",
    "torchvision",
    "transformers",
    "pandas",
    "scipy",
    "pytest",
    "onnx",          # only needed to quantise models, not to run them
    "onnxruntime.quantization",
    "PyInstaller",
    "setuptools",
    "pip",
    # keyring's Windows backend is ctypes-based; cryptography is only pulled in
    # on Linux for SecretService.
    "cryptography",
    "secretstorage",
    "jeepney",
]

a = Analysis(
    ["launcher.py"],
    pathex=["../src"],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Translator",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX on Qt DLLs is a known source of crashes
    console=False,  # tray app: no console window
    disable_windowed_traceback=False,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="Translator",
)
