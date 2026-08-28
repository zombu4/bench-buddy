#!/usr/bin/env bash
#
# Builds the Bench Buddy .deb for Debian/Ubuntu (amd64).
#
#   ./packaging/build_debian.sh
#
# Produces
#   build/dist/bench-buddy/                     the self-contained bundle
#   build/installer/bench-buddy_<ver>_amd64.deb
#
# Layout inside the package:
#   /opt/bench-buddy/                           PyInstaller one-folder payload
#   /usr/bin/bench-buddy                        symlink, created by postinst
#   /usr/share/applications/bench-buddy.desktop
#   /usr/share/icons/hicolor/<size>/apps/bench-buddy.png
#   /usr/share/doc/bench-buddy/copyright
#   /usr/share/doc/bench-buddy/LICENSE      the GPL-3.0 text this program is under
#
# Dependencies: the package bundles Python, Qt 6, NumPy, Pillow and its fonts.
# It declares only what genuinely cannot be shipped inside a relocatable
# bundle -- the C library and the X11/xcb/EGL libraries Qt's platform plugin
# dlopen()s.  The minimum glibc is computed from the binaries actually built,
# not guessed; see resolve_glibc() below.  Because glibc is backwards but not
# forwards compatible, build on the OLDEST distribution you intend to support.

set -euo pipefail

PACKAGING_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$PACKAGING_DIR")"
BUILD_DIR="$ROOT/build"
DIST_DIR="$BUILD_DIR/dist"
WORK_DIR="$BUILD_DIR/work"
STAGE_DIR="$BUILD_DIR/deb"
INSTALLER_DIR="$BUILD_DIR/installer"
APP_DIR="$DIST_DIR/bench-buddy"

PYTHON="${PYTHON:-python3}"
SKIP_PACKAGE=0
if [ "${1:-}" = "--skip-package" ]; then SKIP_PACKAGE=1; fi

step() { printf '==> %s\n' "$1"; }
fail() { printf 'ERROR: %s\n' "$1" >&2; exit 1; }

# ----------------------------------------------------- version, single source
VERSION="$(sed -n 's/^__version__[[:space:]]*=[[:space:]]*["'"'"']\([^"'"'"']*\)["'"'"'].*/\1/p' \
    "$ROOT/app/__init__.py")"
[ -n "$VERSION" ] || fail 'app/__init__.py does not define __version__'

echo
echo 'Bench Buddy - Debian build'
echo "  version : $VERSION"
echo "  root    : $ROOT"
echo

# ---------------------------------------------------------------- toolchain
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
command -v dpkg-deb >/dev/null 2>&1 || fail 'dpkg-deb not found. Install dpkg-dev.'

# -------------------------------------------------------------------- freeze
step 'Running PyInstaller (one-folder)'
rm -rf "$APP_DIR"
"$PYTHON" -m PyInstaller --noconfirm --clean \
    --distpath "$DIST_DIR" --workpath "$WORK_DIR" \
    "$PACKAGING_DIR/bench-buddy.spec"

[ -x "$APP_DIR/bench-buddy" ] || fail "PyInstaller did not produce $APP_DIR/bench-buddy"

# ------------------------------------------------ payload sanity, not faith
step 'Verifying the bundle carries its dependencies'
FONT_DIR="$APP_DIR/_internal/app/ui/fonts"
for name in MartianMono-Regular.ttf IBMPlexSans-Regular.ttf \
            IBMPlexMono-Regular.ttf IBMPlexMono-Medium.ttf \
            OFL-IBMPlex.txt OFL-MartianMono.txt; do
    [ -f "$FONT_DIR/$name" ] || fail "font payload missing: $name"
done
echo '    fonts        4 TTF + 2 OFL licences at _internal/app/ui/fonts'

INTERNAL="$APP_DIR/_internal"
for lib in libQt6Core.so.6 libQt6Gui.so.6 libQt6Widgets.so.6; do
    [ -f "$INTERNAL/PySide6/Qt/lib/$lib" ] || [ -f "$INTERNAL/$lib" ] \
        || fail "Qt payload missing: $lib"
done
ls "$INTERNAL"/libpython3*.so* >/dev/null 2>&1 || fail 'libpython missing from bundle'
[ -d "$INTERNAL/numpy" ] || fail 'numpy missing from bundle'
[ -d "$INTERNAL/PIL" ] || fail 'Pillow missing from bundle'
echo '    runtime      Qt6 Core/Gui/Widgets, libpython3.so, numpy, PIL'

for banned in fastapi uvicorn starlette; do
    if [ -e "$INTERNAL/$banned" ]; then
        fail "excluded package present in bundle: $banned"
    fi
done
echo '    excluded     no fastapi / uvicorn / starlette'

# --------------------------------------------- minimum glibc, measured not told
# Every GLIBC_x.y version tag referenced by anything in the bundle; the highest
# is the floor.  This is what the built binaries actually require, so it moves
# with the build host rather than being an aspiration written in the control
# file.
resolve_glibc() {
    local highest
    # `|| true` because grep exits 1 when nothing matches, which under
    # `set -e` with `set -o pipefail` would abort the build instead of
    # falling through to the default below.
    highest="$(
        {
            find "$APP_DIR" -type f \( -name '*.so' -o -name '*.so.*' -o -perm -u+x \) -print0 2>/dev/null \
            | xargs -0 -r objdump -T 2>/dev/null \
            | grep -oE 'GLIBC_[0-9]+\.[0-9]+' \
            | sed 's/GLIBC_//' \
            | sort -V \
            | tail -n1
        } || true
    )"
    printf '%s' "${highest:-2.31}"
}
if command -v objdump >/dev/null 2>&1; then
    GLIBC="$(resolve_glibc)"
    echo "    min glibc    $GLIBC  (highest GLIBC_* symbol version in the payload)"
else
    GLIBC=2.31
    echo "    min glibc    $GLIBC  (ASSUMED - objdump not installed, could not measure)"
fi

if [ "$SKIP_PACKAGE" -eq 1 ]; then
    echo
    echo "Application folder: $APP_DIR"
    exit 0
fi

# ----------------------------------------------------------------- stage tree
step 'Staging the package tree'
rm -rf "$STAGE_DIR"
mkdir -p "$STAGE_DIR/DEBIAN" \
         "$STAGE_DIR/opt/bench-buddy" \
         "$STAGE_DIR/usr/share/applications" \
         "$STAGE_DIR/usr/share/doc/bench-buddy"

cp -a "$APP_DIR/." "$STAGE_DIR/opt/bench-buddy/"
cp "$PACKAGING_DIR/debian/bench-buddy.desktop" "$STAGE_DIR/usr/share/applications/"

for size in 512 256 128 64 48 32 16; do
    icon="$PACKAGING_DIR/icons/icon-$size.png"
    [ -f "$icon" ] || continue
    dest="$STAGE_DIR/usr/share/icons/hicolor/${size}x${size}/apps"
    mkdir -p "$dest"
    cp "$icon" "$dest/bench-buddy.png"
done

# Debian expects a copyright file.  It states the GPL-3.0-or-later the program
# itself is under, and carries the OFL notice the bundled fonts require to be
# distributed with them.
{
    cat <<'EOF'
Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: bench-buddy
Comment: This package bundles its entire runtime.  The components below are
 redistributed unmodified inside /opt/bench-buddy.
 .
 Python           Python Software Foundation License
 Qt 6 (PySide6)   GNU LGPL v3 - unmodified shared objects in
                  /opt/bench-buddy/_internal/PySide6, replaceable in place
 NumPy            BSD 3-Clause
 Pillow           MIT-CMU
 Martian Mono     SIL Open Font License 1.1
 IBM Plex         SIL Open Font License 1.1
 .
 The full OFL texts are installed at
 /opt/bench-buddy/_internal/app/ui/fonts/ and reproduced below.

Files: *
Copyright: 2026 zombu4
License: GPL-3.0-or-later

Files: _internal/app/ui/fonts/*
Copyright: The Martian Mono Project Authors; IBM Corp.
License: OFL-1.1

License: GPL-3.0-or-later
 Bench Buddy is free software: you can redistribute it and/or modify it under
 the terms of the GNU General Public License as published by the Free Software
 Foundation, either version 3 of the License, or (at your option) any later
 version.
 .
 This program is distributed in the hope that it will be useful, but WITHOUT
 ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
 FOR A PARTICULAR PURPOSE.  See the GNU General Public License for more
 details.
 .
 On Debian systems the complete text of version 3 of the GNU General Public
 License is at /usr/share/common-licenses/GPL-3.  This package also installs
 its own copy at /usr/share/doc/bench-buddy/LICENSE.

License: OFL-1.1
EOF
    sed 's/^/ /; s/^ $/ ./' "$ROOT/app/ui/fonts/OFL-MartianMono.txt"
} > "$STAGE_DIR/usr/share/doc/bench-buddy/copyright"

# Ship the GPL text itself, so the package carries the licence it is under even
# where /usr/share/common-licenses is not populated.
[ -f "$ROOT/LICENSE" ] || fail 'LICENSE (GPL-3.0 text) missing from the source tree'
cp "$ROOT/LICENSE" "$STAGE_DIR/usr/share/doc/bench-buddy/LICENSE"

# ------------------------------------------------------------------- control
step 'Writing DEBIAN/control'
INSTALLED_SIZE="$(du -sk "$STAGE_DIR" | cut -f1)"
sed -e "s/@VERSION@/$VERSION/" \
    -e "s/@INSTALLED_SIZE@/$INSTALLED_SIZE/" \
    -e "s/@GLIBC@/$GLIBC/" \
    "$PACKAGING_DIR/debian/control" > "$STAGE_DIR/DEBIAN/control"

for script in postinst prerm postrm; do
    cp "$PACKAGING_DIR/debian/$script" "$STAGE_DIR/DEBIAN/$script"
    chmod 0755 "$STAGE_DIR/DEBIAN/$script"
done

# dpkg is strict about permissions; normalise rather than inherit whatever the
# checkout had.
find "$STAGE_DIR" -type d -exec chmod 0755 {} +
find "$STAGE_DIR/opt/bench-buddy" -type f -exec chmod 0644 {} +
find "$STAGE_DIR/opt/bench-buddy" -type f -name '*.so*' -exec chmod 0755 {} +
chmod 0755 "$STAGE_DIR/opt/bench-buddy/bench-buddy"
chmod 0644 "$STAGE_DIR/usr/share/applications/bench-buddy.desktop"

# ------------------------------------------------------------------- package
step 'Building the .deb'
mkdir -p "$INSTALLER_DIR"
DEB="$INSTALLER_DIR/bench-buddy_${VERSION}_amd64.deb"
rm -f "$DEB"
dpkg-deb --root-owner-group --build "$STAGE_DIR" "$DEB"

# Lint if lintian is available.  Not fatal: a bundled-runtime package trips
# several tags by design (embedded libraries, non-standard /opt layout).
if command -v lintian >/dev/null 2>&1; then
    step 'lintian (advisory only)'
    lintian --no-tag-display-limit "$DEB" || true
fi

step 'Verifying the built package'
dpkg-deb --info "$DEB" | sed 's/^/    /'
echo "    contents: $(dpkg-deb --contents "$DEB" | wc -l) entries"

SIZE_MB="$(du -m "$DEB" | cut -f1)"
echo
echo 'Build complete.'
echo "  application : $APP_DIR"
echo "  package     : $DEB  (${SIZE_MB} MB)"
echo
echo "  Install with : sudo apt install $DEB"
echo "  Run with     : bench-buddy"
echo "  Minimum glibc: $GLIBC"
