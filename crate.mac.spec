# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Crate on macOS — builds Crate.app (windowed).

Unlike the Windows spec there's no conda-DLL workaround (the CI runner uses a
clean python.org Python). The bundled yt-dlp / ffmpeg are the macOS (arm64)
builds the GitHub Actions workflow downloads into build_tools/bin_mac.
"""

import os
from PyInstaller.utils.hooks import collect_all, collect_submodules

CRATE = os.path.abspath(SPECPATH)

datas = [
    ("templates", "templates"),
    ("build_tools/bin_mac", "bin"),
    ("crate.png", "."),
]
binaries = []
# No pystray here on purpose: its macOS menu-bar backend needs pyobjc, which is
# awkward to bundle. The app degrades gracefully (launcher catches the missing
# import) and quitting is handled by the in-app Quit button / Dock / Cmd-Q.
hiddenimports = [
    "app", "downloader", "fitz",
    "waitress", "PIL", "PIL.Image",
]
hiddenimports += collect_submodules("waitress")

for pkg in ("playwright", "pymupdf"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    ["launcher.py"],
    pathex=[CRATE],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Crate",
    debug=False,
    strip=False,
    upx=False,
    console=False,
    icon="crate.icns",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="Crate",
)

app = BUNDLE(
    coll,
    name="Crate.app",
    icon="crate.icns",
    bundle_identifier="com.cratetool.crate",
    info_plist={
        "CFBundleName": "Crate",
        "CFBundleDisplayName": "Crate",
        "CFBundleShortVersionString": "1.0.0",
        "CFBundleVersion": "1.0.0",
        "NSHighResolutionCapable": True,
        # Keep a normal Dock icon (not a background agent) so there's always a
        # visible way to quit even if the optional tray icon doesn't appear.
        "LSUIElement": False,
    },
)
