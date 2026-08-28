# Bench Buddy

A desktop control application for the **Keysight 34461A** Truevolt bench
digital multimeter, speaking SCPI over VXI-11.

It gives the instrument a resolution-aware readout that shows exactly the
digits the meter is actually resolving, a live strip chart and histogram, a
data log with CSV export, a SCPI console, and single-shot capture of the
instrument's own screen.

Every installer below is **self-contained**. The target machine does not need
Python, Qt, NumPy, Pillow, or any font installed. Everything ships inside.

---

## Install

### Windows

1. Download `bench-buddy-<version>-windows-x64-setup.exe`.
2. Run it. It installs **per user**, into
   `%LOCALAPPDATA%\Programs\bench-buddy`, so it needs **no administrator
   rights** and raises no UAC prompt. (If you are an administrator and prefer
   an all-users install, the wizard offers it.)
3. Launch **Bench Buddy** from the Start Menu.

Uninstall from Settings > Apps, or via `unins000.exe` in the install folder.
The only registry the installer touches is its own Add/Remove Programs entry
under `HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall`. No file
associations, no shell extensions, no autostart entries. The application
itself remembers your saved instruments under `HKCU\Software\bench-buddy`;
delete that key to forget them.

The installer is **not** code-signed, so SmartScreen may show
"Windows protected your PC" the first time. Click **More info** then
**Run anyway**.

### macOS

1. Download `bench-buddy-<version>-macos-<arch>.dmg` and open it.
2. Drag **Bench Buddy** onto the **Applications** folder.

#### First launch on macOS — this step is required

> **This build is not signed with an Apple Developer ID and is not notarised.**
> Double-clicking it the first time **will fail**, with a message like
> *"Bench Buddy cannot be opened because the developer cannot be
> verified."* This is expected. It is not a sign that the download is broken
> or malicious.
>
> To open it the first time:
>
> 1. Open **Applications** in Finder.
> 2. **Right-click** (or Control-click) **Bench Buddy**.
> 3. Choose **Open** from the context menu.
> 4. Click **Open** in the dialog that appears.
>
> You only do this **once**. Every launch after that is a normal double-click.
>
> **On macOS 15 (Sequoia) and later**, right-click > Open may no longer be
> offered. Instead: try to open the app once and let it be blocked, then go to
> **System Settings > Privacy & Security**, scroll to the **Security** section,
> and click **Open Anyway** next to the blocked app.
>
> Please do **not** work around this with `xattr -d com.apple.quarantine`.
> It trains a habit that disables the check for every future download, and the
> two clicks above are all that is actually needed.

The app also asks once for **local network** permission, because that is how it
reaches the instrument. Allow it, or the app cannot connect.

Signing status is stated honestly rather than implied: the build applies an
**ad-hoc signature** (`codesign -s -`). That is a technical requirement, not a
trust decision — on Apple silicon an unsigned arm64 binary will not execute at
all. It confers no Gatekeeper trust, which is why the steps above exist. If a
Developer ID is ever obtained, `packaging/build_macos.sh` will sign and notarise
with it automatically once `CODESIGN_IDENTITY` and `NOTARY_PROFILE` are set,
and this section can be deleted.

### Debian / Ubuntu

```sh
sudo apt install ./bench-buddy_<version>_amd64.deb
bench-buddy
```

It installs to `/opt/bench-buddy`, with a symlink at `/usr/bin/bench-buddy`,
a desktop entry, and hicolor icons, so it also appears in the applications
menu. Remove it with `sudo apt remove bench-buddy`.

**Minimum glibc.** The package bundles Python, Qt, NumPy and Pillow, but it
cannot bundle the C library. glibc is backwards compatible and *not* forwards
compatible, so the package requires at least the glibc of the machine it was
built on. `packaging/build_debian.sh` measures this from the binaries it just
produced — the highest `GLIBC_x.y` symbol version referenced anywhere in the
payload — and writes it into the package's `Depends: libc6 (>= x.y)`, so `apt`
refuses the install rather than letting it fail at runtime.

| Built on | Produces a package needing | Installs on |
|---|---|---|
| **`ubuntu-22.04` — the CI default** | **glibc 2.35** | **Ubuntu 22.04+, Debian 12+** |
| Debian 12 (bookworm) | glibc 2.36 | Debian 12+, Ubuntu 22.10+ |
| `ubuntu-latest` (24.04) | glibc 2.39 | Ubuntu 24.04+ only — excludes Debian 12 |

The Linux job in `.github/workflows/build.yml` is **pinned to `ubuntu-22.04` on
purpose**, because building on `ubuntu-latest` would produce a package that
refuses to install on Debian 12. Do not change it to `ubuntu-latest` without
intending to drop Debian 12 and Ubuntu 22.04 users.

The declared dependencies are only what genuinely cannot travel inside a
relocatable bundle: `libc6`, `libstdc++6`/`libgcc-s1`, and the X11, xcb,
xkbcommon, fontconfig, freetype, glib, D-Bus and GL/EGL libraries that Qt's
`xcb` platform plugin loads from the system. Qt itself, Python, NumPy, Pillow
and the four typefaces are all inside `/opt/bench-buddy`.

---

## Run

```
bench-buddy [--instrument 192.0.2.50] [--transport auto|vxi11|socket]
            [--finite-trigger-count]
```

**Every argument is optional, and there is no built-in default address.** The
app keeps a small library of saved instruments — a name, an address, a
transport and the finite-count flag for each — and picks what to open in this
order:

1. `--instrument` on the command line, or the `DMM_HOST` environment variable.
   Either wins over anything saved, and the address is added to the library so
   it also appears in the Instruments menu afterwards.
2. otherwise the instrument that was connected last time.
3. otherwise nothing is opened and the connection dialog is shown.

`--transport` and `--finite-trigger-count` apply to whichever instrument is
opened at startup, whether that came from the command line or from the saved
selection; given neither, the saved settings for that entry stand.

Once running, the connection is managed from the window: a **Connect /
Disconnect** button and an instrument picker sit next to the link state in the
top bar, **File ▸ Connect…** opens the dialog where instruments are added,
renamed, duplicated and removed, and the **Instruments** menu lists the saved
meters so switching between them is one click. Disconnecting — and switching
away to another meter — restores the trigger setup and returns the instrument
to local with `SYST:LOC`, so its front panel free-runs again.

The saved instruments live under `HKCU\Software\bench-buddy` on Windows, and
in the platform's usual `QSettings` location elsewhere. An entry is named from
the instrument's own `*IDN?` on the first successful connection — `34461A
MY12345678` — unless you have typed a name yourself, which is never
overwritten.

The instrument must have its LAN interface enabled and be reachable on the
VXI-11 portmapper (port 111) or, for the fallback transport, the SCPI raw
socket (port 5025).

**Other models.** Every command this application sends was verified against a
34461A, and the list of commands that must never be sent is model-specific. If
`*IDN?` reports anything else, the app says so, names what it found, and lets
you go ahead at your own discretion — it does not adapt its command set, and
it does not refuse. `app/models.py` is the single place a future
hardware-verified model would be added.

**Why VXI-11 rather than a raw socket.** It is not a style preference — it is a
safety property, measured on the hardware. If this app is killed outright while
the meter is acquiring, a raw socket leaves the instrument acquiring
indefinitely: it has no idea the client died, and the front panel stays frozen
in Remote. A VXI-11 link is a real session, so the instrument device-clears
itself when the link dies, which the operating system does for us when the
process exits. Measured after a hard kill: raw socket still acquiring at 60
seconds; VXI-11 aborted and cleared at 2 seconds, panel free-running.

HiSLIP (port 4880) was tested and does **not** do this on firmware A.03.03 — the
acquisition survives even a clean session close — so it is not used.

`--transport auto` (the default) prefers VXI-11 and falls back to the raw socket
if the portmapper is unreachable, reporting when it has done so. On the fallback
that crash protection is gone, and the app says as much rather than pretending
otherwise. One caveat: the instrument clears itself when the **last** VXI-11
link closes, so the protection is suspended while another VXI-11 client
(Keysight Connection Expert, BenchVue) is also connected.

**What keeps the meter safe while it is idle.** The idle keepalive runs a
continuous acquisition so the front panel stays lit, and something has to be
able to stop it. On a VXI-11 link that is the instrument itself, so the
keepalive uses `TRIG:COUN INF` and the reading is never interrupted. On the raw
socket there is nothing to fall back on, so it uses a finite trigger count that
the drain renews and that expires by itself if this process dies — at the cost
of blanking the reading for one integration period every few seconds. The app
picks whichever the live link calls for and says which is active in the
keepalive tooltip. `--finite-trigger-count` forces the finite count on even
under VXI-11, if you would rather have both.

**A note about the instrument's front panel.** Any SCPI command over LAN puts a
Truevolt meter into Remote, where the panel stops free-running. This app keeps
a low-rate acquisition going the whole time it is connected so the panel stays
live, and hands the instrument back with `SYSTem:LOCal` when you close the
window, so it free-runs again afterwards. Use **Return to Local** if you want
the bench meter back without quitting. See `IO-DISCIPLINE.md` for why this
matters and what was measured.

---

## Build from source

### What you need everywhere

- Python 3.11 or newer (3.12 is what CI uses; 3.14 is the development
  interpreter). The application deliberately uses no syntax newer than 3.11.
- `pip install -r requirements.txt`
- `pip install "pyinstaller>=6.0"`

The version is single-sourced from `__version__` in `app/__init__.py`. Every
build script reads it from there; it is not typed into any installer script.

Icons are generated from `packaging/make_icons.py` into `packaging/icons/`
(committed, so a normal build does not regenerate them):

```sh
python packaging/make_icons.py
```

### Windows

```powershell
.\packaging\build_windows.ps1              # bundle + installer
.\packaging\build_windows.ps1 -SkipInstaller
```

Needs **Inno Setup 6**:

```powershell
winget install --id JRSoftware.InnoSetup
```

Output:

```
build\dist\bench-buddy\                                     the application folder
build\installer\bench-buddy-<ver>-windows-x64-setup.exe     the installer
```

### Debian / Ubuntu

```sh
sudo apt install dpkg-dev binutils
bash packaging/build_debian.sh
bash packaging/build_debian.sh --skip-package    # bundle only
```

`binutils` is not strictly required, but without `objdump` the script cannot
measure the glibc floor and falls back to a conservative assumption, which it
says out loud. `lintian`, if installed, runs advisory-only: a package with a
bundled runtime in `/opt` legitimately trips several tags.

Output: `build/installer/bench-buddy_<ver>_amd64.deb`

### macOS

```sh
bash packaging/build_macos.sh
bash packaging/build_macos.sh --skip-dmg    # .app only
```

Needs the Xcode command line tools (`sips`, `iconutil`, `hdiutil`, `codesign`,
`plutil`). Output: `build/installer/bench-buddy-<ver>-macos-<arch>.dmg`.

To sign and notarise properly, once a Developer ID exists:

```sh
export CODESIGN_IDENTITY="Developer ID Application: Your Name (TEAMID)"
export NOTARY_PROFILE="bench-buddy"      # xcrun notarytool store-credentials
bash packaging/build_macos.sh
```

### CI

`.github/workflows/build.yml` builds all three on a
`windows-latest` / `macos-latest` / `ubuntu-latest` matrix, smoke-tests each
frozen build (it must survive 25 s with no instrument present, which proves it
loaded its own fonts and stood up a Qt window), uploads the installers as
artifacts, and attaches them to a GitHub release on a `v*` tag.

---

## How the packaging works

```
packaging/
  bench-buddy.spec         PyInstaller spec, shared by all three platforms
  make_icons.py            renders icon.png / icon.ico from the app's own tokens
  icons/                   generated, committed
  build_windows.ps1
  build_debian.sh
  build_macos.sh
  windows/installer.iss    Inno Setup script
  windows/LICENSE-FONTS.txt
  debian/control           Depends, with @GLIBC@ filled in at build time
  debian/postinst          /usr/bin symlink, desktop + icon caches
  debian/prerm, postrm
  debian/bench-buddy.desktop
  macos/Info.plist         overlaid onto the .app at build time
  macos/entitlements.plist
  macos/make_dmg.sh
```

Decisions worth knowing about:

**One-folder, not one-file.** A one-file build re-extracts its whole Qt payload
into a temp directory on *every* launch. That is slow, and "executable written
to `%TEMP%` and then run" is a signature several AV products act on. Each
installer hides the folder behind a shortcut, so the user never sees it.

**Fonts are bundled and are load-bearing.** All four typefaces (Martian Mono,
IBM Plex Sans, IBM Plex Mono Regular and Medium) ship as data files at
`app/ui/fonts` inside the bundle, which is exactly where
`app/ui/theme.resource_dir()` looks when `sys._MEIPASS` is set.
`theme.load_fonts()` **raises** rather than falling back to a system face,
because a substituted font would silently break the fixed digit geometry the
readout depends on. A frozen build that starts has therefore provably loaded
its own fonts. Both SIL Open Font License texts ship beside them, as the
licence requires.

**The deleted web stack is excluded by name.** `fastapi`, `uvicorn` and
`starlette` — along with what they used to drag in — are listed in the spec's
`excludes`, so no future transitive import can quietly reintroduce them. Each
build script fails the build if any of the three appears in the payload.

**numpy and Pillow binary extensions are collected explicitly**
(`collect_dynamic_libs`), on top of PyInstaller's stock hooks, so a hook
regression surfaces as a build failure rather than as an `ImportError` on
someone else's machine.

**Each build script verifies its own payload** before packaging: the four TTFs
and two licence files, the Qt Core/Gui/Widgets libraries, the Python runtime,
numpy, Pillow, and the absence of the excluded web stack.

---

## What has actually been verified

Stated honestly, because "it builds" and "it works" are different claims.

**Built and run — Windows.** Built on Windows 11 with Python 3.14.4,
PyInstaller 6.22.2, PySide6 6.11.2, NumPy 2.5.2, Pillow 12.3.0, packaged with
Inno Setup 6.7.3. The installer was installed per-user *from a non-elevated
shell*, launched from its Start Menu shortcut, connected to a real 34461A,
took live readings, streamed to the chart with statistics, captured and decoded
the instrument's own screen, shut down cleanly, and uninstalled with no files,
shortcuts or registry keys left behind. A check of the running process's loaded
modules showed **every** Python, Qt, NumPy and Pillow binary coming from the
install folder and nothing from the system Python — the "no external
dependencies" claim is measured, not assumed. As a negative control, renaming
the font directory inside the frozen bundle makes the same executable refuse to
start with an unhandled `FontError` instead of quietly substituting a system
face — which is what proves the fonts are genuinely being loaded from the
bundle. Restoring the directory restores normal startup.

**Written but not executed — Debian and macOS.** Both build scripts, the
`control` file and maintainer scripts, the `Info.plist`, the entitlements and
the `.dmg` script were written and statically checked: ShellCheck clean,
`bash -n`/`sh -n` clean, plists parse as well-formed property lists, and the
Debian control paragraph was rendered and checked field by field. They have
**not** been executed — no Debian or macOS machine was available — so no claim
is made that they produce a working package until CI or a human runs them.

**Validated only — the CI workflow.** `.github/workflows/build.yml` parses as
valid YAML with the required three-platform matrix. It has not been run.

---

## Licence

Bench Buddy is free software under the **GNU General Public License, version 3
or later** (`GPL-3.0-or-later`). The full text is in [`LICENSE`](LICENSE), and
every source file carries the short notice.

In plain terms: you may use, study, modify and redistribute this code, including
commercially and inside a business. What the licence asks in return is
reciprocity — if you distribute a modified version, or a program that
incorporates this one, you must release that work under the GPL-3.0 as well and
make its complete corresponding source available to whoever receives the binary.
Running it privately, or modifying it for your own bench without distributing
it, carries no such obligation.

The bundled typefaces are third-party assets and keep their own licence: Martian
Mono and IBM Plex are under the SIL Open Font License 1.1, with the full OFL
texts shipped alongside the fonts in `app/ui/fonts/` and reproduced in the
Debian package's copyright file. The Qt libraries bundled by PySide6 are
redistributed unmodified under the LGPL v3.
