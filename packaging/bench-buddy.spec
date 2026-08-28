# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Bench Buddy -- shared by all three builds.

    pyinstaller --noconfirm --clean packaging/bench-buddy.spec

Run it from the repository root; the spec locates the tree relative to itself,
not to the working directory.

Design notes that matter, per ARCHITECTURE.md section 9:

* **One-folder, not one-file.**  A one-file build re-extracts the whole Qt
  payload into a temp directory on every launch -- slow, and it trips the AV
  policies that treat "executable written to %TEMP% then run" as a signature.
  Each installer hides the folder behind a shortcut, so the user never sees it.
* **Everything ships.**  Python, Qt, numpy and Pillow are all inside the
  folder.  No target machine needs any of them installed.
* **The web stack is excluded by name.**  ``fastapi``/``uvicorn``/``starlette``
  were deleted from the app; they are listed in ``excludes`` so that no future
  transitive import can quietly drag them back in.
* **Fonts are data files.**  They land at ``app/ui/fonts`` inside the bundle,
  which is exactly where ``app/ui/theme.resource_dir()`` looks when
  ``sys._MEIPASS`` is set.  ``theme.load_fonts()`` raises rather than falling
  back to a system face, so a frozen build that starts has provably loaded
  them.
"""

import os
import re
import sys

from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules

# --------------------------------------------------------------------- layout

# SPECPATH is injected by PyInstaller and is the directory holding this file.
ROOT = os.path.abspath(os.path.join(SPECPATH, os.pardir))
APP = os.path.join(ROOT, "app")
ICONS = os.path.join(SPECPATH, "icons")

NAME = "bench-buddy"
DISPLAY_NAME = "Bench Buddy"
BUNDLE_ID = "com.github.benchbuddy"
PUBLISHER = "bench-buddy"


def read_version():
    """Single source of truth: ``__version__`` in ``app/__init__.py``.

    Parsed rather than imported so the spec never pulls the application (and
    therefore PySide6) into PyInstaller's own process.
    """
    source = open(os.path.join(APP, "__init__.py"), encoding="utf-8").read()
    match = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', source, re.M)
    if not match:
        raise SystemExit("app/__init__.py does not define __version__")
    return match.group(1)


VERSION = read_version()

# ---------------------------------------------------------------------- datas

# The four bundled faces and both OFL licence texts.  Shipping the licences is
# a condition of the SIL Open Font License, so they are part of the payload on
# every platform, not just a nicety.
FONT_DIR = os.path.join(APP, "ui", "fonts")
datas = [
    (os.path.join(FONT_DIR, name), os.path.join("app", "ui", "fonts"))
    for name in sorted(os.listdir(FONT_DIR))
    if name.endswith((".ttf", ".txt"))
]

# Bench Buddy's own licence.  The application is GPL-3.0-or-later, so the
# licence text travels with every binary distribution of it.
_LICENSE = os.path.join(ROOT, "LICENSE")
if not os.path.isfile(_LICENSE):
    raise SystemExit("LICENSE (GPL-3.0 text) missing from the source tree")
datas.append((_LICENSE, "."))

_ttf = [d for d in datas if d[0].endswith(".ttf")]
if len(_ttf) != 4:
    raise SystemExit(
        "expected 4 bundled TTFs in app/ui/fonts, found %d: %s"
        % (len(_ttf), [os.path.basename(d[0]) for d in _ttf])
    )

# ------------------------------------------------------------------- binaries

# numpy and Pillow are mostly compiled extensions.  PyInstaller's stock hooks
# find them, but collecting explicitly means a hook regression shows up as a
# build error here rather than as an ImportError on a user's machine.
binaries = collect_dynamic_libs("numpy") + collect_dynamic_libs("PIL")

hiddenimports = []
# PIL's plugin modules are imported by name at runtime by Image.open()/save(),
# so static analysis cannot see them.  The app decodes the instrument's BMP16
# screen dumps and saves PNG.
hiddenimports += [m for m in collect_submodules("PIL") if m.endswith("ImagePlugin")]
hiddenimports += ["PIL._imaging", "PIL.Image", "PIL.ImageFile"]

# ------------------------------------------------------------------- excludes

excludes = [
    # The withdrawn web delivery.  ARCHITECTURE.md section 9 excludes these by name.
    "fastapi",
    "uvicorn",
    "starlette",
    # ...and what they used to drag in with them.
    "pydantic",
    "pydantic_core",
    "anyio",
    "sniffio",
    "h11",
    "httptools",
    "websockets",
    "wsproto",
    "click",
    "watchfiles",
    "python_multipart",
    "jinja2",
    # Never used by this application.
    "tkinter",
    "test",
    "pytest",
    "setuptools",
    "pip",
    "matplotlib",
    "scipy",
    "pandas",
    "IPython",
    "notebook",
    # Other Qt bindings: PIL.ImageQt probes for these and would pull a second
    # toolkit into the bundle if one happened to be installed on the builder.
    "PyQt5",
    "PyQt6",
    "PySide2",
]

# The application imports exactly QtCore, QtGui and QtWidgets.  Excluding the
# rest keeps the payload to what is actually used; QtWebEngine alone is ~150 MB.
excludes += [
    "PySide6." + m
    for m in (
        "Qt3DAnimation", "Qt3DCore", "Qt3DExtras", "Qt3DInput", "Qt3DLogic",
        "Qt3DRender", "QtBluetooth", "QtCharts", "QtDataVisualization",
        "QtDesigner", "QtHelp", "QtLocation", "QtMultimedia",
        "QtMultimediaWidgets", "QtNetwork", "QtNfc", "QtOpenGL",
        "QtOpenGLWidgets", "QtPdf", "QtPdfWidgets", "QtPositioning", "QtQml",
        "QtQuick", "QtQuick3D", "QtQuickControls2", "QtQuickWidgets",
        "QtRemoteObjects", "QtScxml", "QtSensors", "QtSerialBus",
        "QtSerialPort", "QtSpatialAudio", "QtSql", "QtStateMachine", "QtSvg",
        "QtSvgWidgets", "QtTest", "QtTextToSpeech", "QtUiTools",
        "QtWebChannel", "QtWebEngineCore", "QtWebEngineQuick",
        "QtWebEngineWidgets", "QtWebSockets", "QtXml",
    )
]

# --------------------------------------------------------------------- analyse

a = Analysis(
    [os.path.join(ROOT, "main.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

# ---------------------------------------------------------------- executables

icon = None
version_resource = None

if sys.platform == "win32":
    icon = os.path.join(ICONS, "icon.ico")
    # A Windows version resource, built from the same __version__ string, so
    # right-click -> Properties -> Details agrees with the installer.
    from PyInstaller.utils.win32 import versioninfo as vi

    _parts = [int(p) for p in re.findall(r"\d+", VERSION)][:4]
    _parts += [0] * (4 - len(_parts))
    _tuple = tuple(_parts)
    version_resource = vi.VSVersionInfo(
        ffi=vi.FixedFileInfo(filevers=_tuple, prodvers=_tuple, mask=0x3F, flags=0x0,
                             OS=0x4, fileType=0x1, subtype=0x0, date=(0, 0)),
        kids=[
            vi.StringFileInfo([
                vi.StringTable("040904B0", [
                    vi.StringStruct("CompanyName", PUBLISHER),
                    vi.StringStruct("FileDescription", DISPLAY_NAME),
                    vi.StringStruct("FileVersion", VERSION),
                    vi.StringStruct("InternalName", NAME),
                    vi.StringStruct("OriginalFilename", NAME + ".exe"),
                    vi.StringStruct("ProductName", DISPLAY_NAME),
                    vi.StringStruct("ProductVersion", VERSION),
                ]),
            ]),
            vi.VarFileInfo([vi.VarStruct("Translation", [0x0409, 1200])]),
        ],
    )
elif sys.platform == "darwin":
    _icns = os.path.join(ICONS, "icon.icns")
    icon = _icns if os.path.isfile(_icns) else None

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,          # one-folder: binaries go in COLLECT
    name=NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                      # UPX-packed Qt DLLs are a common AV false positive
    console=False,                  # GUI app: no console window on Windows
    disable_windowed_traceback=False,
    argv_emulation=False,           # macOS: the app takes no file arguments
    target_arch=None,
    codesign_identity=None,
    entitlements_file=(
        os.path.join(SPECPATH, "macos", "entitlements.plist")
        if sys.platform == "darwin"
        else None
    ),
    icon=icon,
    version=version_resource,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=NAME,
)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name=DISPLAY_NAME + ".app",
        icon=icon,
        bundle_identifier=BUNDLE_ID,
        version=VERSION,
        info_plist={
            "CFBundleName": DISPLAY_NAME,
            "CFBundleDisplayName": DISPLAY_NAME,
            "CFBundleExecutable": NAME,
            "CFBundleIdentifier": BUNDLE_ID,
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "CFBundlePackageType": "APPL",
            "CFBundleSignature": "????",
            "LSMinimumSystemVersion": "11.0",
            "LSApplicationCategoryType": "public.app-category.utilities",
            "NSHighResolutionCapable": True,
            "NSPrincipalClass": "NSApplication",
            "NSRequiresAquaSystemAppearance": False,
            # The app opens a raw TCP socket to the instrument on the local
            # network; on macOS 15+ that prompts once unless declared.
            "NSLocalNetworkUsageDescription":
                "Connects to the Keysight 34461A multimeter on your local "
                "network over SCPI (TCP port 5025).",
            "NSHumanReadableCopyright":
                "Bundled fonts are licensed under the SIL Open Font License; "
                "see the OFL files beside the application.",
        },
    )
