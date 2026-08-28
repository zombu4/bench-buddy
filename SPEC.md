# Keysight 34461A — instrument reference

Every fact below was verified against the physical instrument at 192.0.2.50 on
2026-08-27 by probing it directly rather than by trusting the programming guide:
each command was sent on a real socket and its answer, its timing and its
failure mode recorded. Where the documentation and the instrument disagreed the
instrument won, and the measurement is what is written down here. Where a fact
recorded here later turned out to be wrong, it is corrected in place and the
correction noted.

See also `ARCHITECTURE.md` for how the application is built on top of these
facts, and `IO-DISCIPLINE.md` for the hardware-safety findings that govern how
it talks to the instrument.

## 0. Engineering standards the code follows

- There are no stubs, placeholder functions, `TODO` markers or mocked data
  paths in the shipped code.
- SCPI syntax is never guessed. Only the commands in section 2 are sent. The
  "unsupported" list is not theoretical: those commands make the instrument go
  silent and poison the socket until it is reconnected.
- There is no silent `except: pass`. Every failure surfaces to the UI with a
  real message.
- There are no hardcoded range tables. Ranges are enumerated at runtime
  (section 2.4).
- Python 3.14 on Windows. The runtime dependencies are **PySide6, numpy,
  Pillow** and the stdlib, and nothing more — the app ships as three
  self-contained installers, so every dependency has to bundle cleanly.
  (`fastapi` and `uvicorn` belonged to the withdrawn web delivery and are gone.)

## 1. Verified instrument facts

| Fact | Value |
|---|---|
| IDN | `Keysight Technologies,34461A,MY12345678,A.03.03-03.15-03.03-00.52-04-03` |
| Firmware | A.03.03 — already the newest Keysight ships. No update available. |
| SCPI raw socket | 192.0.2.50:5025 |
| VXI-11 | `inst0`, core channel found via the portmapper on port 111. **This is the default transport** — see IO-DISCIPLINE.md rule 1 for why. |
| HiSLIP | `hislip0` on 4880. Works, but does *not* stop an acquisition when the session closes, so it is not used. |
| Concurrent sockets | **Supported.** Two independent raw sessions verified working; at least 5 concurrent VXI-11 links. |
| Reading memory | ~10,400 readings. Saturates rather than erroring: overflow shows as bit 14 of `STAT:QUES:COND?` and never enters the error queue. |
| Line frequency | 60 Hz (`SYST:LFR?`) |
| Cal | count 70, date 2025-10-22 |
| Options | none (`*OPT?` -> `0,0,0`) |
| Terminals | `ROUT:TERM?` -> `FRON`. Read-only, physical switch. |

Screen capture (`HCOP:SDUM:DATA?`):
- `HCOP:SDUM:DATA:FORM BMP` -> 277494 bytes in **0.156–0.180 s** (~6 fps)
- `HCOP:SDUM:DATA:FORM PNG` -> 7337 bytes but **2.56 s**. Never use PNG format.
- BMP header verified: 480 x 289, **16 bpp, BI_RGB (compression 0), top-down**
  (the height field is negative), pixel data at offset 54, row stride 960 bytes.
  16bpp BI_RGB is X1R5G5B5. 480*289*2 + 54 = 277494 exactly.

Streaming throughput (verified end to end):
- NPLC 0.2 -> 142 readings/s ; NPLC 0.02 -> 718 readings/s
- Reconfiguring mid-run works: `ABOR` -> change -> `INIT`.

## 2. SCPI command set

### 2.1 SUPPORTED — verified to answer

Identity/system: `*IDN?` `*OPT?` `*RST` `*CLS` `*ESR?` `*STB?` `*TST?`
`SYST:ERR?` `SYST:UPT?` `SYST:LFR?` `SYST:DATE?` `SYST:TIME?` `SYST:BEEP`
`SYST:BEEP:STAT?|<b>` `SYST:CLIC:STAT?|<b>` `SYST:LOCK:OWN?` `SYST:LOCK:REQ?`
`SYST:LOCK:REL` `SYST:SEC:COUN?` `ROUT:TERM?`
`STAT:QUES:COND?` `STAT:OPER:COND?`

LAN: `SYST:COMM:LAN:HOSTname?` `:IPADdress?` `:MAC?` `:DHCP?` `:SMAS?` `:GAT?`
`:DNS?` `:DOM?` `:TELN:WMES?` and `LXI:IDEN:STAT?|<b>`

Cal: `CAL:DATE?` `CAL:COUN?` `CAL:STR?`
These disagree by a day on this unit — `CAL:DATE?` gives `+2025,+10,+22` and
`CAL:STR?` gives `"10/21/2025"`. Expose both separately rather than reconciling.

Function select: `CONF:VOLT:DC [<rng>|AUTO]` and likewise `CONF:VOLT:AC`
`CONF:CURR:DC` `CONF:CURR:AC` `CONF:RES` `CONF:FRES` `CONF:FREQ` `CONF:PER`
`CONF:CAP` `CONF:CONT` `CONF:DIOD` `CONF:TEMP`. Read back with `CONF?` and
`SENS:FUNC?`.

Per-function sense nodes (prefix `<p>` = one of
`VOLT:DC` `VOLT:AC` `CURR:DC` `CURR:AC` `RES` `FRES` `FREQ` `PER` `CAP` `TEMP`):
- `<p>:RANG?` `<p>:RANG <v>` `<p>:RANG:AUTO?` `<p>:RANG:AUTO <b>`
  (`FREQ`/`PER` use `FREQ:VOLT:RANG`; `CONT`/`DIOD` have no range)
- `<p>:RANG? MIN` / `<p>:RANG? MAX` — used for range enumeration
- `<p>:NULL:STAT?|<b>` `<p>:NULL:VAL?|<v>` `<p>:NULL:VAL:AUTO?|<b>`
- `<p>:RES?` — **only** for `VOLT:DC` `VOLT:AC` `CURR:DC` `CURR:AC` `RES` `FRES`
- `VOLT:DC:NPLC?|<v>`, also on `CURR:DC` `RES` `FRES` `TEMP`.
  Legal values: 0.02 0.2 1 10 100
- `VOLT:DC:APER?` (read only — reports integration seconds for current NPLC)
- `VOLT:DC:IMP:AUTO?|<b>` (0 = 10 Mohm, 1 = >10 Gohm for ranges <= 10 V)
- `VOLT:DC:ZERO:AUTO?|<ON|OFF|ONCE>`, same node on `CURR:DC` `RES` `TEMP`
- `VOLT:AC:BAND?|<3|20|200>`, same on `CURR:AC`
- `FREQ:APER?|<v>` (0.001 0.01 0.1 1), `PER:APER?`, `FREQ:RANG:LOW?`
- `TEMP:TRAN:TYPE?|<FRTD|RTD|FTH|THER>` `TEMP:TRAN:RTD:RES?|<v>`
  `TEMP:TRAN:THER:TYPE?|<5000>` and `UNIT:TEMP?|<C|F|K>`

Trigger/sampling: `TRIG:SOUR?|<IMM|BUS|EXT>` `TRIG:DEL?|<v>` `TRIG:DEL:AUTO?|<b>`
`TRIG:COUN?|<n|INF>` `TRIG:SLOP?|<POS|NEG>` `SAMP:COUN?|<n>`
`INIT` `ABOR` `READ?` `FETC?` `*TRG`

Readings: `DATA:POIN?` `DATA:LAST?` `R? <n>` (returns a definite-length block)
`DATA:REM? <n>`

`DATA:LAST?` returns the value **with a unit suffix**, e.g. `+2.55526268E-04  VDC`.
Strip the suffix before parsing; a bare `float()` on the raw reply fails.

Math (Truevolt subsystems — all verified present):
- `CALC:SCAL:STAT?|<b>` `CALC:SCAL:FUNC?` — **query returns `NULL|DB|DBM`, but
  only `DB` and `DBM` may be written.** Measured twice on this unit, error queue
  drained first, with scaling both on and off: `CALC:SCAL:FUNC NULL` is refused
  with `-224,"Illegal parameter value"` and the previous value stands. It does
  not hang the socket, so it is not a section 2.2 hazard — it just always fails.
  Nulling is done through the per-function `<p>:NULL:*` nodes, which are the only
  null path this firmware implements.
  `CALC:SCAL:DB:REF?|<v>` `CALC:SCAL:DBM:REF?|<v>`
  Note the instrument may adopt its own auto-acquired reference instead of the
  one requested (observed: a requested `1.0` became `600.0`). Display what it
  actually adopted, as everywhere else.
- `CALC:AVER:STAT?|<b>` `CALC:AVER:AVER?` `:MIN?` `:MAX?` `:PTP?` `:SDEV?`
  `:COUN?` `CALC:AVER:CLE`
- `CALC:LIM:STAT?|<b>` `CALC:LIM:LOW?|<v>` `CALC:LIM:UPP?|<v>`
- `CALC:TRAN:HIST:STAT?|<b>` `CALC:TRAN:HIST:POIN?|<n>` `CALC:TRAN:HIST:COUN?`
  `CALC:TRAN:HIST:ALL?` `CALC:TRAN:HIST:CLE`
  `CALC:TRAN:HIST:RANG:AUTO?|<b>` `:RANG:LOW?|<v>` `:RANG:UPP?|<v>`
  `CALC:TRAN:HIST:ALL?` returns: lower, upper, count, then the bin counts, CSV.

Display: `DISP?|<b>` `DISP:TEXT?|<"s">` `DISP:TEXT:CLE`
`DISP:VIEW?|<NUM|TCH|HIST|MET>` — all four values verified to set cleanly.
This is how the app changes what the mirrored screen shows.

Screen: `HCOP:SDUM:DATA:FORM <BMP|PNG>` `HCOP:SDUM:DATA?`

### 2.2 UNSUPPORTED — NEVER SEND (each one hangs the socket)

`SAMP:SOUR?` `SAMP:TIM?` `SAMP:COUN:PRET?` `TRIG:LEV?` `RES:OCOM?`
`FRES:OCOM?` `CONT:THR?` `DIOD:THR?` `TEMP:UNIT?` (use `UNIT:TEMP?`)
`VOLT:DC:APER:ENAB?` `CALC:SCAL:REF?` `CALC:SCAL:GAIN?` `CALC:SCAL:OFFS?`
`CALC:SCAL:PCT?` `CALC:SCAL:UNIT?` `CALC:SCAL:UNIT:STAT?` `DISP:ANN:STAT?`
`DISP:DIG:MASK?` `SYST:PRES?` `SYST:LANG?` `SYST:IDN?` `SYST:HELP:HEAD?`
`CAP:RES?` `FREQ:RES?` `PER:RES?` `TEMP:RES?`
`FRES:ZERO:AUTO?` `VOLT:AC:ZERO:AUTO?` — autozero exists ONLY on `VOLT:DC`,
`CURR:DC`, `RES` and `TEMP`. Verified twice against the instrument: querying it
on 4-wire resistance or AC volts hangs the socket. Section 8's table originally
claimed FRES had autozero; that was wrong and is corrected below.

### 2.3 Continuous streaming pattern (verified working)

```
ABOR ; *CLS
CONF:<func> ... ; <per-function config>
TRIG:SOUR IMM ; TRIG:COUN INF ; SAMP:COUN 1
INIT
loop:  n = DATA:POIN?  ; if n > 0:  block = R? min(n, 4000)
```

`R?` returns a definite-length block of comma-separated floats and removes them
from instrument memory. To change configuration while running: `ABOR`, apply the
change, `INIT` again.

### 2.4 Range enumeration (no hardcoded tables)

Ranges are read from the instrument rather than transcribed from a datasheet.
For each function that has a range node, `<p>:RANG? MIN` and `<p>:RANG? MAX` are
queried once at startup and cached; the list is built by multiplying MIN by 10
while the value is < MAX, then appending MAX. This was verified to produce the
correct sets, e.g. CURR:DC MIN 1e-4 MAX 3 -> [1e-4, 1e-3, 1e-2, 1e-1, 1, 3].
`<p>:RANG?` is re-read after every set, and the value displayed is the one the
instrument actually adopted.

## 3. Signature UI behaviour: the resolution-aware readout

`<p>:RES?` is the instrument's own reported measurement resolution and is
available for VOLT:DC, VOLT:AC, CURR:DC, CURR:AC, RES, FRES only. The reading is
rendered so that digits at or above that resolution are solid warm phosphor and
digits below it are dimmed to about 35% opacity. Under the number sits a thin
horizontal band scaled to +/- one resolution step, labelled with the resolution.

For CAP / FREQ / PER / TEMP / CONT / DIOD the state field `resolution` is null,
so every digit is solid and the band is hidden. **No accuracy or uncertainty
figure is ever fabricated:** the only number shown is the one the instrument
reports.

This is the single element the app is remembered by, and everything around it
stays quiet.

## 4. Design system

Ground: cool blue-black instrument bezel. Readout: warm phosphor. The tension
between the cool housing and the warm display is the whole point of the palette,
and neutralising it — a cool readout, or a warm ground — would lose the effect
entirely.

```
--ink:      #0E1419   page ground
--panel:    #151D25   raised panel
--panel-2:  #1C2731   control surface
--rule:     #293742   hairline borders
--text:     #C4D2DD   body text
--dim:      #6B7C8C   labels, secondary
--phosphor: #F2EFE6   the reading (warm, near-white)
--signal:   #4FC3E8   live / active / accent
--warn:     #E8A33D   limits, caution
--fail:     #E5604D   limit failure, errors
--ok:       #6FCF7F   pass
```

Type — three faces, each with a real fallback stack (the desktop application
bundles them rather than fetching them; see `ARCHITECTURE.md`):
- `Martian Mono` — the big readout and headline numerics only, used with
  restraint. It never appears in body copy or button labels.
- `IBM Plex Sans` — all UI text, labels, buttons.
- `IBM Plex Mono` — SCPI console, data tables, numeric values inside panels.

Layout — a bench, in three zones:
- Top bar: model, serial, firmware, IP, live indicator, connection state.
- Left rail: the function selector — the rotary-switch equivalent — a vertical
  list of the 12 functions with their mnemonics (DCV, ACV, DCI, ACI, 2W, 4W,
  FREQ, PER, CAP, CONT, DIODE, TEMP).
- Centre: the hero readout with its resolution band, then a horizontal control
  strip directly beneath it that mirrors the instrument's real softkey row,
  then a tabbed area: Chart / Histogram / Log / SCPI.
- Right rail: the live mirrored screen, statistics, limits.

Structural device that carries meaning: every control carries its SCPI node as a
small caption (e.g. RANGE with `VOLT:DC:RANG` beneath it). This is true to the
content and teaches the command set while the user works. There is no decorative
01 / 02 / 03 numbering, because the content is not a sequence.

Motion: restrained. The live dot pulses. Digits update in place with no layout
shift (tabular numerals, fixed-width slots). There is no scroll-reveal
animation, and `prefers-reduced-motion` is respected.

Copy: active voice, sentence case, things named the way an engineer at the bench
would name them. The button that starts streaming says "Run", and the state it
produces is labelled "Running". Errors state what happened and what to do, and
never apologise. The empty chart says what will fill it.

Quality floor: responsive down to a narrow window, visible keyboard focus on
every control, no horizontal page scroll, wide content scrolls inside its own
container.

## 5. Chart

The chart began as `web/chart.js`, a dependency-free canvas strip chart.
`app/ui/chart.py` is the `QPainter` port of it and keeps the same behaviour:
- ring buffer holding at least 200k points, decimated to min/max per pixel column
- autoscale with a manual override, pan by drag, zoom by wheel
- time axis in seconds relative to run start, value axis with SI prefixes
- crosshair readout on hover, limit lines drawn when limits are enabled
- devicePixelRatio aware, resizes with its container via ResizeObserver
- a separate histogram renderer for the instrument's `CALC:TRAN:HIST:ALL?` bins

## 6. Function table (`app/specs.py`)

Twelve functions. Each entry records the SCPI key, label, short mnemonic, unit,
whether it has a range node (and which prefix that node uses), whether NPLC
applies, whether autozero applies, whether AC bandwidth applies, whether aperture
applies, and whether `RES?` is queryable.

```
VOLT:DC  DC Voltage      DCV    V     range NPLC azero imped RES?
VOLT:AC  AC Voltage      ACV    V     range band              RES?
CURR:DC  DC Current      DCI    A     range NPLC azero        RES?
CURR:AC  AC Current      ACI    A     range band              RES?
RES      2-Wire Resistance 2W   ohm   range NPLC azero        RES?
FRES     4-Wire Resistance 4W   ohm   range NPLC              RES?   (NO azero)
FREQ     Frequency       FREQ   Hz    range(FREQ:VOLT:RANG) aperture
PER      Period          PER    s     range(FREQ:VOLT:RANG) aperture
CAP      Capacitance     CAP    F     range
CONT     Continuity      CONT   ohm   —
DIOD     Diode           DIODE  V     —
TEMP     Temperature     TEMP   deg   NPLC azero probe-config
```
