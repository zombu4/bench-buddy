#!/usr/bin/env bash
#
# Builds Bench Buddy for macOS: a .app bundle inside a .dmg.
#
#   ./packaging/build_macos.sh
#
# Produces
#   build/dist/Bench Buddy.app
#   build/installer/bench-buddy-<ver>-macos-<arch>.dmg
#
# SIGNING -- read this before shipping the result.
#
# This script does NOT sign with an Apple Developer ID and does NOT notarise.
# No such account or certificate is configured for this project, and none was
# found in the repository.  What it does instead is an *ad-hoc* signature
# (`codesign -s -`), which is not a trust decision -- it is a technical
# requirement: on Apple silicon an unsigned arm64 binary will not execute at
# all, so the ad-hoc signature is what makes the bundle runnable, not what
# makes it trusted.
#
# The consequence, which the README documents prominently and the disk image
# repeats in "READ ME FIRST.txt": on first launch the user must right-click the
# app and choose Open (or approve it under System Settings > Privacy &
# Security).  Do not tell users to run `xattr -d com.apple.quarantine`; it
# teaches a habit that defeats the check for every future download.
#
# If a Developer ID ever becomes available, set these and the script will use
# them instead:
#     export CODESIGN_IDENTITY="Developer ID Application: Name (TEAMID)"
#     export NOTARY_PROFILE="bench-buddy"    # from `xcrun notarytool store-credentials`

set -euo pipefail

PACKAGING_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$PACKAGING_DIR")"
BUILD_DIR="$ROOT/build"
DIST_DIR="$BUILD_DIR/dist"
WORK_DIR="$BUILD_DIR/work"
INSTALLER_DIR="$BUILD_DIR/installer"
APP_NAME='Bench Buddy.app'
APP="$DIST_DIR/$APP_NAME"

PYTHON="${PYTHON:-python3}"
CODESIGN_IDENTITY="${CODESIGN_IDENTITY:-}"
NOTARY_PROFILE="${NOTARY_PROFILE:-}"
SKIP_DMG=0
if [ "${1:-}" = "--skip-dmg" ]; then SKIP_DMG=1; fi

step() { printf '==> %s\n' "$1"; }
fail() { printf 'ERROR: %s\n' "$1" >&2; exit 1; }

[ "$(uname -s)" = "Darwin" ] || fail 'this script only runs on macOS'

# ----------------------------------------------------- version, single source
VERSION="$(sed -n 's/^__version__[[:space:]]*=[[:space:]]*["'"'"']\([^"'"'"']*\)["'"'"'].*/\1/p' \
    "$ROOT/app/__init__.py")"
[ -n "$VERSION" ] || fail 'app/__init__.py does not define __version__'
ARCH="$(uname -m)"

echo
echo 'Bench Buddy - macOS build'
echo "  version : $VERSION"
echo "  arch    : $ARCH"
echo "  root    : $ROOT"
echo

# ------------------------------------------------------------------ toolchain
step 'Checking build tools'
command -v "$PYTHON" >/dev/null 2>&1 || fail "$PYTHON not found"
echo "    python       $("$PYTHON" -c 'import sys;print(".".join(map(str,sys.version_info[:3])))')"
"$PYTHON" -c 'import PyInstaller' 2>/dev/null \
    || fail "PyInstaller is not installed. Run: $PYTHON -m pip install pyinstaller"
echo "    pyinstaller  $("$PYTHON" -m PyInstaller --version)"
for module in PySide6 numpy PIL; do
    "$PYTHON" -c "import $module" 2>/dev/null \
        || fail "$module is not installed. Run: $PYTHON -m pip install -r requirements.txt"
done
echo '    PySide6, numpy, Pillow present'
for tool in sips iconutil hdiutil codesign plutil; do
    command -v "$tool" >/dev/null 2>&1 || fail "$tool not found (install the Xcode command line tools)"
done

# ---------------------------------------------------------------------- icon
step 'Building icon.icns'
ICONSET="$WORK_DIR/bench-buddy.iconset"
MASTER="$PACKAGING_DIR/icons/icon.png"
[ -f "$MASTER" ] || fail "missing $MASTER -- run: $PYTHON packaging/make_icons.py"
rm -rf "$ICONSET"
mkdir -p "$ICONSET"
for size in 16 32 128 256 512; do
    sips -z "$size" "$size" "$MASTER" --out "$ICONSET/icon_${size}x${size}.png" >/dev/null
    double=$((size * 2))
    sips -z "$double" "$double" "$MASTER" --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$PACKAGING_DIR/icons/icon.icns"
echo "    $PACKAGING_DIR/icons/icon.icns"

# -------------------------------------------------------------------- freeze
step 'Running PyInstaller (one-folder .app bundle)'
rm -rf "$APP" "$DIST_DIR/bench-buddy"
"$PYTHON" -m PyInstaller --noconfirm --clean \
    --distpath "$DIST_DIR" --workpath "$WORK_DIR" \
    "$PACKAGING_DIR/bench-buddy.spec"

[ -d "$APP" ] || fail "PyInstaller did not produce $APP"

# ---------------------------------------------------------- the shipped plist
# Overlay the reviewable Info.plist from the repository so the bundle carries
# the file that was read in code review, not one assembled at build time.
step 'Installing Info.plist'
sed "s/@VERSION@/$VERSION/g" "$PACKAGING_DIR/macos/Info.plist" > "$APP/Contents/Info.plist"
plutil -lint "$APP/Contents/Info.plist"
plutil -lint "$PACKAGING_DIR/macos/entitlements.plist"

# ------------------------------------------------ payload sanity, not faith
step 'Verifying the bundle carries its dependencies'
FONT_DIR=""
for sub in Resources Frameworks; do
    if [ -d "$APP/Contents/$sub/app/ui/fonts" ]; then
        FONT_DIR="$APP/Contents/$sub/app/ui/fonts"
        break
    fi
done
[ -n "$FONT_DIR" ] || fail 'bundled fonts not found in the .app'
for name in MartianMono-Regular.ttf IBMPlexSans-Regular.ttf \
            IBMPlexMono-Regular.ttf IBMPlexMono-Medium.ttf \
            OFL-IBMPlex.txt OFL-MartianMono.txt; do
    [ -f "$FONT_DIR/$name" ] || fail "font payload missing: $name"
done
echo "    fonts        4 TTF + 2 OFL licences at ${FONT_DIR#"$APP/"}"

# NB: no `find ... | grep -q` here.  Under `set -o pipefail`, grep -q closes
# the pipe on its first match, find dies of SIGPIPE, and the pipeline reports
# failure precisely when the check succeeded.  Test the captured output instead.
FW="$APP/Contents/Frameworks"
for lib in QtCore QtGui QtWidgets; do
    found="$(find "$FW" \( -name "$lib" -o -name "lib$lib*" \) -print 2>/dev/null | head -n1)"
    [ -n "$found" ] || fail "Qt payload missing: $lib"
done
found="$(find "$FW" \( -name 'Python' -o -name 'libpython3*' \) -print 2>/dev/null | head -n1)"
[ -n "$found" ] || fail 'Python runtime missing from bundle'
[ -d "$FW/numpy" ] || fail 'numpy missing from bundle'
[ -d "$FW/PIL" ] || fail 'Pillow missing from bundle'
echo '    runtime      Qt6 Core/Gui/Widgets, Python, numpy, PIL'

for banned in fastapi uvicorn starlette; do
    if [ -e "$FW/$banned" ]; then
        fail "excluded package present in bundle: $banned"
    fi
done
echo '    excluded     no fastapi / uvicorn / starlette'

# ------------------------------------------------------------------ signing
step 'Signing'
if [ -n "$CODESIGN_IDENTITY" ]; then
    echo "    Developer ID: $CODESIGN_IDENTITY"
    codesign --force --deep --timestamp --options runtime \
        --entitlements "$PACKAGING_DIR/macos/entitlements.plist" \
        --sign "$CODESIGN_IDENTITY" "$APP"
    SIGNED='Developer ID, hardened runtime'
else
    echo '    no CODESIGN_IDENTITY set -- applying an AD-HOC signature only.'
    echo '    The result is NOT notarised. First launch needs right-click > Open.'
    codesign --force --deep --sign - "$APP"
    SIGNED='ad-hoc (unsigned for distribution purposes)'
fi
codesign --verify --deep --strict --verbose=2 "$APP" 2>&1 | sed 's/^/    /'

# ---------------------------------------------------------------------- dmg
if [ "$SKIP_DMG" -eq 1 ]; then
    echo
    echo "Application bundle: $APP"
    exit 0
fi

DMG="$INSTALLER_DIR/bench-buddy-${VERSION}-macos-${ARCH}.dmg"
step 'Building the disk image'
mkdir -p "$INSTALLER_DIR"
bash "$PACKAGING_DIR/macos/make_dmg.sh" "$APP" "$VERSION" "$DMG"

# ----------------------------------------------------------------- notarise
if [ -n "$NOTARY_PROFILE" ] && [ -n "$CODESIGN_IDENTITY" ]; then
    step 'Notarising'
    xcrun notarytool submit "$DMG" --keychain-profile "$NOTARY_PROFILE" --wait
    xcrun stapler staple "$DMG"
    SIGNED="$SIGNED, notarised and stapled"
else
    echo
    echo 'NOT NOTARISED. Set CODESIGN_IDENTITY and NOTARY_PROFILE to notarise.'
fi

echo
echo 'Build complete.'
echo "  application : $APP"
echo "  disk image  : $DMG  ($(du -h "$DMG" | cut -f1))"
echo "  signing     : $SIGNED"
echo
if [ -z "$CODESIGN_IDENTITY" ]; then
    echo 'FIRST LAUNCH: the user must right-click the app in Applications and'
    echo 'choose Open, once. Gatekeeper blocks a plain double-click on an'
    echo 'unsigned build. This is documented in the README and on the disk image.'
fi
