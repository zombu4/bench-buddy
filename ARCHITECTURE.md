# Bench Buddy — how the application is built

This describes the structure of the desktop application and the reasoning behind
it: where the threads are, how the readout knows what to dim, what happens on
overload, and how the window is put together.

See also `SPEC.md` for the instrument itself — the SCPI command set, the
never-send list, BMP geometry, streaming pattern, timings and range enumeration
— and `IO-DISCIPLINE.md` for the hardware-safety findings that shaped the
transport, the idle keepalive and the withdrawal of continuous screen capture.

## 0. The modules

The backend modules were verified against the physical instrument across 76
checks, and are the part of the codebase that changes least:

    app/scpi.py        SCPI transport, reconnect-on-timeout, block reads
    app/screen.py      BMP16 (X1R5G5B5, top-down) -> image
    app/specs.py       function metadata, runtime range enumeration
    app/models.py      the instrument models this application is written for
    app/instrument.py  Dmm34461A: state, config, streaming thread, logging

The Qt application sits on top of them:

    app/ui/__init__.py
    app/ui/main.py        QApplication bootstrap, window, dark palette
    app/ui/theme.py       design tokens, fonts, stylesheet
    app/ui/readout.py     the resolution-aware readout widget (the signature)
    app/ui/strip.py       function rail + softkey control strips
    app/ui/chart.py       live strip chart widget (QPainter)
    app/ui/histogram.py   histogram widget
    app/ui/mirror.py      single-shot screen capture (see IO-DISCIPLINE rule 4)
    app/ui/console.py     SCPI console
    app/ui/logtab.py      data log table + CSV export
    app/ui/system.py      system / LAN / calibration panel
    app/ui/connect.py     saved-instrument library and connection dialog
    app/ui/bridge.py      Qt<->instrument threading bridge

## 1. Engineering standards the code follows

- There are no stubs, placeholder widgets, `TODO` markers or mocked data paths.
- There are no silent excepts. Every instrument failure reaches the UI with its
  real text.
- Nothing on `SPEC.md` section 2.2's never-send list is ever sent.
- The dependencies are **PySide6, numpy, Pillow** and the stdlib. There is no
  web framework; `fastapi` and `uvicorn` are not in `requirements.txt`.
- Python 3.14 is the development interpreter, but no syntax newer than 3.11 is
  used, so the frozen builds stay portable across the CI runners.

## 2. Threading model — where a Qt port usually breaks

`Dmm34461A` is blocking and thread-confined. Calling it from the Qt GUI thread
would freeze the window for the length of a 0.16 s screen grab or a 3 s SCPI
timeout, so it is never called from there.

- One `QThread` owns all instrument I/O (`app/ui/bridge.py`). The `Dmm34461A`
  object lives on it.
- The GUI talks to it exclusively through queued signals/slots. Every method
  that touches the instrument is a slot on the worker; every result comes back
  as a signal.
- The streaming and idle-keepalive threads inside `instrument.py` (there is no
  screen-capture thread — `IO-DISCIPLINE.md` rule 4) marshal to Qt with
  `QMetaObject.invokeMethod` with `Qt.QueuedConnection`, or by emitting from a
  `QObject` that lives on the worker thread. They never touch a widget
  directly.
- Readings arrive in batches at up to ~740/s, which is far more often than
  anything should repaint. Incoming samples are buffered and the chart and
  readout repaint on a `QTimer` at 30 Hz. The coalescing drops nothing from the
  data buffer, only from the paint rate.
- Shutdown is clean: stop streaming, stop the idle keepalive, restore the user's
  trigger setup, return the instrument to local, quit and join the worker
  thread, close the socket. No orphaned threads on window close.

## 3. Signature element — the resolution-aware readout

Unchanged in intent from `SPEC.md` section 5, drawn with `QPainter`.

`<func>:RES?` is the instrument's own reported resolution, available only for
VOLT:DC, VOLT:AC, CURR:DC, CURR:AC, RES, FRES. The reading is rendered so digits
at or above that resolution are solid warm phosphor and digits below it are
dimmed to about 35% opacity, with one guard digit shown below resolution so the
boundary is legible. Beneath the number is the resolution band.

When `resolution` is null (CAP, FREQ, PER, TEMP, CONT, DIOD) every digit is
solid and the band is hidden, with a caption naming why.

**No accuracy or uncertainty figure is ever synthesised.** The only numbers
shown are ones the instrument reported.

These details were derived from the instrument's real behaviour and carried over
from the earlier implementation because they are correct:

- Digit slots are computed from configuration only (range, resolution, unit),
  never from the live value, so no digit ever changes width or position.
- SI prefix comes from the range when the range node is in the reading's unit,
  and from the reading otherwise — FREQ and PER carry a `FREQ:VOLT:RANG`
  *voltage* range, so scaling from it would be wrong.
- Volts and amps are capped at exponent 0: this meter reads to 1000 V and 3 A
  and never displays kV or kA. The 1000 V range reads `231.4567 V`.
- Decimals are grouped in threes with a thin space, mirroring the instrument.
- `QFontMetrics` is used with tabular figures and a fixed advance per digit
  cell. Digits are drawn into fixed slots rather than relying on string layout
  to hold position.

## 4. Overload handling

`9.91E37` is the instrument's overload / no-reading sentinel. `instrument.py`
converts these: overloaded samples are published as `None`, with a companion
list of overloaded indices, and histogram bounds carrying the sentinel are
nulled.

The UI then:
- renders `OVLD` in place of the digits, keeping the same slot geometry
- excludes `None` samples from chart autoscale and from any statistic
- shows a gap in the chart trace rather than interpolating across the overload
- writes an explicit `OVLD` cell, not an empty one, in the CSV export

## 5. Design system

The tokens are identical to `SPEC.md` section 6 — the palette and its logic
carry over unchanged. Cool blue-black instrument bezel, warm phosphor readout.
That tension is the point of the palette, and neutralising it would lose it.

    ink       #0E1419   window ground
    panel     #151D25   raised panel
    panel2    #1C2731   control surface
    rule      #293742   hairline borders
    text      #C4D2DD   body text
    dim       #6B7C8C   labels, secondary
    phosphor  #F2EFE6   the reading (warm, near-white)
    signal    #4FC3E8   live / active / accent
    warn      #E8A33D   limits, caution
    fail      #E5604D   limit failure, errors
    ok        #6FCF7F   pass

The fonts are **bundled with the application** rather than fetched or assumed
present on the target machine, because the app has to run with no network and no
system font installs. The TTFs ship in `app/ui/fonts/` and are loaded with
`QFontDatabase.addApplicationFont` at startup:

- **Martian Mono** — the big readout and headline numerics only. Never body
  copy, never button labels.
- **IBM Plex Sans** — all UI text, labels, buttons.
- **IBM Plex Mono** — SCPI console, data tables, numeric values in panels.

All three are open licensed (SIL OFL), and their licence files are part of the
installer payload.

The palette is set explicitly with `QPalette` and a stylesheet rather than
inherited from the host theme, so the app looks identical on Windows, macOS and
Debian and does not follow the OS light/dark setting.

## 6. Layout

A single main window, three zones:

- **Top bar** — model, serial, firmware, IP, live indicator, connection state.
- **Left rail** — the function selector, the rotary-switch equivalent: a
  vertical list of the twelve functions with their mnemonics (DCV, ACV, DCI,
  ACI, 2W, 4W, FREQ, PER, CAP, CONT, DIODE, TEMP).
- **Centre** — the hero readout with its resolution band, then a horizontal
  control strip beneath it mirroring the instrument's real softkey row, then a
  `QTabWidget`: Chart / Histogram / Log / SCPI / System.
- **Right rail** — the screen capture panel, statistics, limits.

Every control carries its SCPI node as a small caption (RANGE with
`VOLT:DC:RANG` beneath). Captions are generated per function, so selecting 4W
relabels the range caption to `FRES:RANG`. Softkeys appear only when the
corresponding state field exists for the active function.

The minimum window is 1100x720. Rails collapse below 1000 px width. The window
is resizable and does not clip the readout.

## 7. Chart widget

`app/ui/chart.py`, drawn with `QPainter` on a `QWidget`. No QtCharts dependency.

- ring buffer of at least 262144 points
- min/max decimation per pixel column, binary search into the time array
- autoscale with manual override; limit lines folded into the autoscale
- drag to pan, wheel to zoom X, shift+wheel to zoom Y
- crosshair readout following the cursor
- time axis in seconds relative to run start, value axis with SI prefixes
- gaps at overload samples, never interpolated across
- repaint driven by the 30 Hz timer, not by data arrival
- honours `devicePixelRatio` for crisp lines on HiDPI

A separate histogram widget renders the instrument's `CALC:TRAN:HIST:ALL?` bins.

## 8. Screen capture

Continuous mirroring is withdrawn — see `IO-DISCIPLINE.md` rule 4. There is no
capture thread and no timer. The ~6 fps polling this section originally
described was the heaviest single load on the instrument's LAN stack and is what
degraded the hardware during development. What remains is one grab per button
press, shown with its capture time and never presented as live.

`app/ui/mirror.py`. A user-initiated grab on the one link produces a frame in
~0.16 s, which is converted to a `QImage` and blitted.

- scaled to fit, preserving the 480x289 aspect ratio, with no smoothing (it is a
  pixel-exact instrument screen)
- the frame shows when it was captured; a snapshot is never labelled as live,
  and once it is more than a few seconds old the caption gives the time it was
  taken
- a control switches the instrument's own display view
  (`DISP:VIEW NUM|TCH|HIST|MET`) so the next capture shows that view
- a button saves the current frame as PNG
- a "Capture screen" button takes one grab per press, never a loop

## 9. Packaging — the actual deliverable

Three self-contained installers. No target machine needs Python, Qt, or any
other runtime installed.

    packaging/
      bench-buddy.spec       PyInstaller spec, shared by all platforms
      windows/installer.iss  Inno Setup script -> Setup .exe
      debian/               control, postinst, desktop entry -> .deb
      macos/                Info.plist, entitlements, dmg script -> .dmg
      build_windows.ps1
      build_debian.sh
      build_macos.sh
    .github/workflows/build.yml    matrix: windows-latest, macos-latest, ubuntu-latest

PyInstaller decisions that matter:
- one-folder mode, not one-file: one-file extracts to a temp dir on every
  launch, which is slow and breaks some AV policies. The installer hides the
  folder anyway.
- the web stack (`fastapi`, `uvicorn`, `starlette`) is excluded explicitly so it
  cannot be pulled in transitively
- the fonts are bundled as data files, and the build confirms they load in the
  frozen build
- Pillow and numpy both need their binary extensions collected; the build
  verifies the frozen app decodes a real BMP frame, not merely that it launches

Per-platform:
- **Windows**: Inno Setup, per-user install by default so it needs no admin
  rights, Start Menu shortcut, uninstaller, no registry beyond uninstall keys.
- **Debian**: `.deb` with a `.desktop` entry and icon, installing to
  `/opt/bench-buddy` with a symlink in `/usr/bin`. It declares no dependencies
  beyond `libc6` and the X/Wayland basics Qt needs — everything else ships
  inside — and states the minimum glibc the build produces.
- **macOS**: `.app` bundle in a `.dmg`. The README states honestly whether the
  build is signed and notarised; an unsigned build requires the user to
  right-click Open on first launch, which is documented rather than left to be
  discovered.

The version is single-sourced from `app/__init__.py` and read by every build
script.

## 10. What a working build is checked against

- The app launches from source and connects to the instrument.
- Every function, config control, trigger setting and math mode round-trips to
  the instrument and shows the value the instrument actually adopted.
- Streaming sustains its rate with a responsive UI: no frozen window, no
  unbounded memory growth over a multi-minute run.
- A screen capture returns the real instrument display, with its capture time.
- A forced overload renders `OVLD` and leaves a gap in the trace.
- An unsupported command typed into the SCPI console produces a clean error,
  recovers the link, and does not kill an in-progress run.
- Closing the window exits with no orphaned threads and no sockets left open.
- The **frozen** build does all of the above, not just the source run — a
  working source tree that breaks under PyInstaller is not a delivered app.

`README.md` records which of these have actually been executed, on which
platforms, and which have only been statically checked.
