#!/usr/bin/env bash
#
# Wraps "Bench Buddy.app" in a distributable .dmg.
#
#   packaging/macos/make_dmg.sh <app-bundle> <version> <output.dmg>
#
# Called by build_macos.sh; usable on its own if you already have a bundle.
#
# The disk image is a plain read-only compressed UDZO image containing the
# .app and a symlink to /Applications, which is the drag-to-install idiom every
# Mac user already knows.  No licence agreement resource, no custom background
# -- both need extra tooling and neither makes the install any clearer.

set -euo pipefail

APP="${1:?usage: make_dmg.sh <app-bundle> <version> <output.dmg>}"
VERSION="${2:?usage: make_dmg.sh <app-bundle> <version> <output.dmg>}"
OUT="${3:?usage: make_dmg.sh <app-bundle> <version> <output.dmg>}"

VOLNAME="Bench Buddy $VERSION"

[ -d "$APP" ] || { echo "ERROR: no such app bundle: $APP" >&2; exit 1; }
command -v hdiutil >/dev/null 2>&1 || { echo 'ERROR: hdiutil not found (macOS only)' >&2; exit 1; }

STAGE="$(mktemp -d -t bench-buddy-dmg)"
cleanup() { rm -rf "$STAGE"; }
trap cleanup EXIT

echo "==> Staging disk image contents"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"

# The OFL requires the licence to travel with the fonts.  It is already inside
# the bundle; putting a copy at the top level of the image means a user can read
# it without going through "Show Package Contents".
# PyInstaller 6 splits the payload across Contents/Frameworks (binaries) and
# Contents/Resources (data), cross-symlinked, so try both rather than assume.
APP_NAME="$(basename "$APP")"
for sub in Resources Frameworks; do
    licences="$STAGE/$APP_NAME/Contents/$sub/app/ui/fonts"
    if [ -d "$licences" ]; then
        mkdir -p "$STAGE/Licences"
        cp "$licences"/OFL-*.txt "$STAGE/Licences/" 2>/dev/null || true
        break
    fi
done
if [ ! -d "$STAGE/Licences" ]; then
    echo 'ERROR: font licences not found inside the app bundle' >&2
    exit 1
fi

cat > "$STAGE/READ ME FIRST.txt" <<'EOF'
Bench Buddy
====================

Install
    Drag "Bench Buddy" onto the Applications folder in this window.

First launch -- this step is required
    This build is NOT signed with an Apple Developer ID and is NOT notarised.
    Double-clicking it the first time will fail with a message such as
    "cannot be opened because the developer cannot be verified".

    To open it:
        1. Open your Applications folder in Finder.
        2. RIGHT-CLICK (or Control-click) "Bench Buddy".
        3. Choose "Open" from the menu.
        4. Click "Open" in the dialog that appears.

    You only have to do this once. Afterwards it launches normally.

    On macOS 15 (Sequoia) and later, right-click Open may no longer be offered.
    In that case open System Settings > Privacy & Security, scroll to the
    Security section, and click "Open Anyway" next to the blocked app.

Local network access
    The app talks to the instrument over TCP port 5025 on your local network.
    macOS will ask for permission once; allow it, or the app cannot connect.

Everything it needs is inside the bundle -- Python, Qt, NumPy, Pillow and its
typefaces. Nothing else has to be installed.
EOF

echo "==> Building $OUT"
rm -f "$OUT"
mkdir -p "$(dirname "$OUT")"
hdiutil create \
    -volname "$VOLNAME" \
    -srcfolder "$STAGE" \
    -ov \
    -format UDZO \
    -imagekey zlib-level=9 \
    -fs HFS+ \
    "$OUT"

echo "==> Verifying"
hdiutil verify "$OUT"
echo "    $(du -h "$OUT" | cut -f1)  $OUT"
