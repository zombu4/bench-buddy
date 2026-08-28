# Keithley DMM7510 — candidate SCPI command set

> ## NOTHING IN THIS DOCUMENT HAS TOUCHED HARDWARE
>
> Every command below is **UNVERIFIED**. It was transcribed from Keithley's own
> reference manual, not measured on an instrument. No DMM7510 has been on this
> network, so not one line here has the standing that `SPEC.md` has.
>
> **The 34461A's never-send list does not transfer.** `SPEC.md` section 2.2
> lists commands that hang *that* instrument's socket. It is a property of
> Truevolt firmware and says nothing about a DMM7510. The DMM7510 will have its
> own never-send list, and **it is currently empty because nobody has looked** —
> not because it is short.
>
> **Before any of this is trusted, probe it:**
>
> ```
> python tools/probe_instrument.py <address> --profile keithley-dmm7510 --dry-run
> python tools/probe_instrument.py <address> --profile keithley-dmm7510
> ```

**Sources.** `[RM p. N]` refers to the *Model DMM7510 7½ Digit Graphical
Sampling Multimeter Reference Manual*, document **DMM7510-901-01 Rev. C /
September 2019** (Keithley Instruments / Tektronix). `[6500 RM]` refers to
DMM6500-901-01 Rev. A / April 2018, cited where the two manuals disagree.

**Read `docs/keithley-dmm6500.md` first.** The DMM7510 and the DMM6500 are the
same firmware generation and share the great majority of their SCPI. Rather than
duplicate it, this document states what is **the same**, then covers what is
**different** in full. Every caveat in the DMM6500 document — the graphical
"Functions" applicability tables that do not extract, the absent screen-capture
command, the unmeasured crash safety — applies here identically.

---

## 1. What is identical to the DMM6500

Verified identical by diffing the two manuals' SCPI command headings. All still
**UNVERIFIED against hardware**; "identical" means the manuals agree.

- **Function selection** — `:SENSe:FUNCtion "<string>"`, quoted, with the same
  fifteen function strings, and the same separate `:SENSe:DIGitize:FUNCtion`
  node with the same mutual-exclusion rule (`NONE` from whichever is inactive)
  [RM p. 11-118; 6500 RM p. 13-151].
- **Range** — `:SENSe:<f>:RANGe[:UPPer]` and `:RANGe:AUTO`, with the
  `<DEF|MIN|MAX>` set and query forms [RM p. 11-81 area].
- **NPLC and aperture** — `:SENSe:<f>:NPLCycles`, range **0.0005 to 15 at 60 Hz
  (12 at 50 Hz)**, and `:SENSe:<f>:APERture`, with the same "changing one
  changes the other" relationship [RM p. 11-76 area].
- **Digitize sample rate** — `:SENSe:DIGitize:<f>:SRATe`, **1,000 to 1,000,000
  readings per second** [RM p. 6-2].
- **The whole buffer model** — `defbuffer1` / `defbuffer2` at 100,000 readings
  initially, `:TRACe:ACTual?`, `:ACTual:STARt?`, `:ACTual:END?`,
  `:TRACe:DATA? <start>, <end>, "<buf>", <elements>`, `:TRACe:CLEar`,
  `:TRACe:POINts`, `:TRACe:MAKE`, `:TRACe:FILL:MODE`, and the
  `:TRACe:STATistics:*` family. Same non-destructive indexed read, same
  cursor-tracking requirement.
- **The trigger-block model** — `:TRIGger:LOAD "SimpleLoop"|"DurationLoop"|
  "LoopUntilEvent"|"ConfigList"|"GradeBinning"|"SortBinning"|"LogicTrigger"|
  "Empty"`, `:INITiate`, `:ABORt`, `:TRIGger:STATe?`, `:TRIGger:BLOCk:LIST?`,
  `:TRIGger:PAUSe` / `:RESume`.
- **Math and limits** — `:CALCulate[1]:<f>:MATH:*` (`MXB|PERCent|RECiprocal`)
  and `:CALCulate2:<f>:LIMit<Y>:*`.
- **Null/relative** — `:SENSe:<f>:RELative`, `:RELative:STATe`,
  `:RELative:ACQuire`, `:RELative:METHod`.
- **Filter** — `:SENSe:<f>:AVERage:*` with `TCONtrol REPeat|MOVing`.
- **`:SENSe:<f>:INPutimpedance`** — `MOHM10` or `AUTO` [RM p. 11-71 area].
- **`:SENSe:<f>:DETector:BANDwidth`** — **3, 30 or 300 Hz**, not the 34461A's
  3/20/200 [RM p. 11-70 area].
- **`:FORMat[:DATA] ASCii|REAL|SREal`**, with binary responses that "start with
  `#0` and end with a new line" — an *indefinite*-length block, not the
  34461A's definite-length `#<n><len>`.
- **Display** — `:DISPlay:SCReen`, `:DISPlay:USER<n>:TEXT`, `:DISPlay:CLEar`,
  `:DISPlay:LIGHt:STATe`, `:DISPlay:<f>:DIGits`, `:DISPlay:BUFFer:ACTive`,
  `:DISPlay:READing:FORMat`. `:DISPlay:SCReen` is still command-only, no query.
- **Errors and status** — `:SYSTem:ERRor[:NEXT]?`, `:ERRor:CODE?`,
  `:ERRor:COUNt?`, `:SYSTem:EVENtlog:*`, `:STATus:OPERation:CONDition?`,
  `:STATus:QUEStionable:CONDition?`, `:SYSTem:LFRequency?`,
  `:SYSTem:VERSion?`, `:SYSTem:ACCess`, `:SYSTem:BEEPer <freq>, <seconds>`.
- **`*IDN?` format** — `KEITHLEY INSTRUMENTS,MODEL nnnn,xxxxxxxx,yyyyyy`.
- **No `*OPT?`** in either manual.
- **LAN ports** — Telnet 23, VXI-11 **1024**, raw socket 5025, dead socket
  termination 5030 [RM p. 2-20]. Identical table to the DMM6500's, and again
  **no HiSLIP anywhere in the manual**.
- **One controlling interface at a time**, and multiple ethernet connections
  permitted but only one in control [RM p. 2-20].
- **No SCPI screen-capture command.** Only "Save screen captures to a USB flash
  drive" via HOME+ENTER on the front panel [RM p. 3-52].

---

## 2. What is different — DMM7510 only

### 2.1 `:TRIGger:CONTinuous` — the return-to-local answer, documented here

This is the most valuable difference for this project, and unlike the DMM6500 it
is **fully documented in the 7510's own manual** [RM p. 11-191]:

```
:TRIGger:CONTinuous <OFF|AUTO|RESTart>       UNVERIFIED  [RM p. 11-191]
:TRIGger:CONTinuous?                         UNVERIFIED
```

- `OFF` — do not start continuous measurements after boot-up.
- `AUTO` — start continuous measurements after boot-up. **This is the default.**
- `RESTart` — "Place the instrument into local control and start continuous
  measurements after bootup."

The details section is explicit about what `RESTart` does immediately: "the
instrument is placed in local mode, aborts any running scripts, and aborts any
trigger models that are running… The restart parameter is not stored in
nonvolatile memory, so it does not affect start up behavior" [RM p. 11-191].

That is exactly the hand-back `IO-DISCIPLINE.md` rule 2 asks for — abort, back
to local, front panel free-running — in one command. It is the DMM7510's
`SYST:LOC`.

**It is still UNVERIFIED and must be tested in isolation on a throwaway session
first**, exactly as `SYST:LOC` was on the 34461A (`IO-DISCIPLINE.md` rule 6).
It is the single highest-value probe on this instrument. Note also that `OFF`
and `AUTO` **are stored in nonvolatile memory** [RM p. 11-191], so an app must
never write them casually — it would change how the user's meter behaves at
power-on, permanently, which is not the app's business.

### 2.2 Autocalibration (ACAL)

The DMM7510 has an autocalibration subsystem the DMM6500 does not
[RM p. 4-98, p. 2-37]. All **UNVERIFIED**:

```
:ACAL:RUN                                    [RM p. 11-8 area]
:ACAL:COUNt?
:ACAL:LASTrun:TIME?
:ACAL:LASTrun:TEMPerature:INTernal?
:ACAL:LASTrun:TEMPerature:DIFFerence?
:ACAL:NEXTrun:TIME?
:ACAL:REVert
:ACAL:SCHedule <RUN|NOTIFY|NONE>[, <HOUR8|HOUR16|DAY1|DAY7>[, <hour>]]
:ACAL:SCHedule?
```

The manual gives this exact block of five queries as the way to "get information
about how many times autocalibration has been done, autocalibration temperature,
and last and scheduled autocalibration dates" [RM p. 2-37], which is unusually
good evidence for their syntax.

Two related system queries, also 7510-only:

```
:SYSTem:TEMPerature:INTernal?                UNVERIFIED
:SYSTem:FAN:LEVel <...>                      UNVERIFIED
```

**This is the natural replacement for the 34461A's `CAL:DATE?` / `CAL:COUN?` /
`CAL:STR?` panel in the app's system view** — and it is richer, because it also
carries the temperature drift since the last ACAL, which is what actually
determines whether the 7½ digits mean anything. Note [RM p. 2-37]: "The firmware
build, memory available, and factory calibration date are **not** available when
using SCPI commands." So the *factory* calibration date the 34461A exposes has
no SCPI equivalent here at all.

### 2.3 7.5 digits

```
:DISPlay:<function>:DIGits <3|4|5|6|7>       UNVERIFIED  [RM p. 11-38]
```

`7` (i.e. 7½) is available, and "not available for digitize functions"
[RM p. 11-38]. The DMM6500 tops out at `6` [6500 RM p. 13-37].

This matters for the app's hero readout: the digit-slot count is per model and
per function, and the `resolution` field in `SPEC.md` section 4.1 has no
instrument-reported equivalent on either Keithley (see the DMM6500 document,
section 4). `:DISPlay:<f>:DIGits?` is the nearest available signal and it is a
*display preference*, not a measurement resolution. **Do not present it as an
uncertainty figure** — `SPEC.md` section 5 is explicit that the app must never
fabricate one.

### 2.4 Ranges that differ

From [RM p. 11-83] against [6500 RM p. 13-115]:

| Function | DMM7510 | DMM6500 |
|---|---|---|
| AC voltage, top range | **700 V** | 750 V |
| 2-wire resistance, top | **1 GΩ** | 100 MΩ |
| 4-wire resistance | **1 Ω … 1 GΩ** | 1 Ω … 100 MΩ (10 kΩ with offset comp. on) |
| DC voltage | 100 mV … 1000 V | same |
| DC current | 10 µA … 3 A (10 A rear) | same |
| Capacitance | 1 nF … 1 mF | same |

The 7510's range table as extracted does **not** show the DMM6500's
"4-wire resistance with offset compensation on → 1 Ω to 10 kΩ" restriction.
**Unverified whether that restriction exists here.** Probe
`:SENSe:FRESistance:OCOMpensated` and re-enumerate ranges with it on and off.

### 2.5 Digitizing

Both models digitize, but the DMM7510 is the one built around it: "18-bit
current and voltage digitizing" [RM p. 1-2] and "The digitize functions can
provide 1,000,000 readings per second at 4½ digits" with "separate internal
signal paths that are optimized for fast response to signal changes"
[RM p. 6-2]. The DMM6500's `STAT_ORIGIN` buffer status bit documents its
digitizer as a second ADC alongside the main one [6500 RM p. 13-184].

7510-only digitizer controls, all **UNVERIFIED**:

```
:SENSe:DIGitize:<VOLTage|CURRent>:COUPling <AC|DC>          [RM p. 11-63 area]
:SENSe:DIGitize:<VOLTage|CURRent>:COUPling:AC:FILTer <...>
:SENSe:<function>:AC:FREQuency <3 Hz .. 1 MHz>
:SENSe:<function>:AC:FREQuency?
:SENSe:<function>:DCIRcuit <ON|OFF>     "available for 1 Ω to 10 kΩ ranges"
:SENSe:<function>:BIAS:ACTual?
:SENSe:<function>:SENSe:RANGe[:UPPer] <0.1|1|10>   settable here, query-only on the 6500
:SENSe:<function>:SENSe:RANGe:AUTO <ON|OFF>
:SENSe:<function>:THReshold:LEVel <-700 .. 700 V>
```

`COUPling AC` means "the instrument only measures the AC components of the
signal" while `DC` measures both [RM p. 3-46 area] — an oscilloscope-style
control with no 34461A analogue.

Richer analog triggering, 7510-only:

```
:SENSe:<function>:ATRigger:HFReject <ON|OFF>
:SENSe:<function>:ATRigger:PULSe:CONDition <...>
:SENSe:<function>:ATRigger:PULSe:LEVel <n>
:SENSe:<function>:ATRigger:PULSe:POLarity <...>
:SENSe:<function>:ATRigger:PULSe:WIDTh <n>
:SENSe:TRIGger:MEASure:STIMulus <event>
:SENSe:TRIGger:DIGitize:STIMulus <event>
```

(The `ATRigger:EDGE:*` and `ATRigger:WINDow:*` families exist on both models.)

### 2.6 `:TRACe:MAKE` gains a COMPact style

```
:TRACe:MAKE "<name>", <size>[, COMPact|STANdard|FULL|WRITable|FULLWRitable]
```

`COMPact` stores "readings with reduced accuracy (6.5 digits) with no formatting
information, 1 s accurate timestamp" [RM p. 11-160 area], and `<bufferSize>` may
be **0 to maximise the buffer size** — neither is in the DMM6500 Rev. A manual.
For a 1 MS/s digitizer, `COMPact` is how you fit a long capture in memory.

### 2.7 What the DMM6500 has that the DMM7510 does not

Useful as a fingerprint, and as a warning against writing one driver that
assumes either:

- The entire scanning-card surface: `:ROUTe:SCAN:*`, `:ROUTe[:CHANnel]:*`,
  `:SYSTem:CARD1:*`, `:SYSTem:PCARd1`, `:TRACe:CHANnel:MATH`,
  `:DISPlay:WATCh:CHANnels`, and the `(@<channelList>)` parameter that appears
  on nearly every DMM6500 `SENSe` command. **The DMM7510's `SENSe` commands have
  no channel-list parameter at all.**
- `:TRACe:STATistics:SPAN?`
- `:SENSe:<f>:RTD:TWO` and `:SENSe:<f>:TCouple:RJUNction:RSELect`. The DMM7510's
  `:SENSe:<f>:TRANsducer` list as extracted reads `TCouple | THERmistor | TRTD |
  FRTD` where the DMM6500's reads `TCouple | THERmistor | RTD | TRTD | FRTD |
  CJC2001` — i.e. **2-wire RTD may be absent on the 7510**. My extraction was
  truncated; treat this as a probe item, not a fact.
- `*LANG SCPI2000` and `*LANG SCPI34401`. **The DMM7510's `*LANG` accepts only
  `SCPI` and `TSP`** [RM p. 14-5]. There is no 34401A emulation mode on this
  model.
- `:DISPlay:SCReen` values `SWIPE_CHANnel`, `SWIPE_NONSwitch`, `SWIPE_SCAN`,
  `CHANNEL_CONTrol`, `CHANNEL_SETTings`, `CHANNEL_SCAN` — the DMM7510's list
  stops at `SWIPE_USER` and `PROCessing` [RM p. 11-41].

### 2.8 Virtual front panel client limit

"The DMM7510 allows fewer than three clients to open the virtual front panel web
page at the same time. Only the first successfully connected client can operate
the instrument. Other clients can view the virtual front panel" [RM p. 2-30].

The same page documents the right-click menu offering **Download screenshot**,
and the 800×480 / 400×240 resolution switch. As with the DMM6500, **the URL is
not published**, and the honest way to get it is one look at the browser network
tab. See `docs/keithley-dmm6500.md` section 8.

---

## 3. Mapping onto what this app needs

Identical to the DMM6500 in every respect except where noted. Restating the four
questions the task asks, for this model:

**How would streaming work?** The same `:TRIGger:LOAD "DurationLoop"` or
`"SimpleLoop"` + `:INITiate` + cursor-tracked `:TRACe:DATA?` shape, with the same
open question about how to run indefinitely — see
`docs/keithley-dmm6500.md` section 6.1. **Low confidence.** The DMM7510 adds a
second regime the app has never had to handle: at 1 MS/s digitizing, 100,000
buffer readings last **0.1 seconds**. Any streaming design that polls at a few
hertz is simply wrong for the digitize functions, and the app should either
refuse them or drive them as bounded captures rather than as a live strip chart.

**How would an idle keepalive work?** Unknown whether one is needed — the
premise of `IO-DISCIPLINE.md` rule 1 is a Truevolt behaviour (the panel freezes
in remote), and it has to be **measured** here before anything is built around
it. If it is needed, `:TRIGger:CONTinuous RESTart` looks like a much cleaner
lever than the 34461A's drained-acquisition trick, because it is documented to
put the instrument back into local *and* start continuous measurements — i.e.
exactly the state the rule wants. Whether an app can sit in that state and still
read values is untested.

**Is there a crash-safe transport?** Unknown. VXI-11 is available and the same
last-link device-clear behaviour is plausible, but plausible is what
`IO-DISCIPLINE.md` exists to reject. `tools/probe_instrument.py` measures it by
hard-killing a child process and observing from a fresh session. Note two
DMM7510/6500-specific mitigations that the 34461A lacks: dead-socket termination
on port 5030, and `"DurationLoop"`, which gives the instrument an intrinsic
deadman.

**What replaces `HCOP:SDUM:DATA?`** Nothing over SCPI. The screen exists as an
HTTP resource behind the virtual front panel at 800×480; the URL is
undocumented. Until it is found, the DMM7510 driver should report screen capture
as unsupported rather than offer a button that fails.

---

## 4. Ready-to-run candidate list

Built into the probe tool as profile `keithley-dmm7510`:

```
python tools/probe_instrument.py <address> --profile keithley-dmm7510 --dry-run
python tools/probe_instrument.py <address> --profile keithley-dmm7510
```

It is the DMM6500 candidate list (see `docs/keithley-dmm6500.md` section 11)
with the DMM6500-only scanning queries removed and these added. To build your
own, take that document's JSON and replace the `queries` additions with:

```json
[
  ":ACAL:COUNt?",
  ":ACAL:LASTrun:TIME?",
  ":ACAL:LASTrun:TEMPerature:INTernal?",
  ":ACAL:LASTrun:TEMPerature:DIFFerence?",
  ":ACAL:NEXTrun:TIME?",
  ":ACAL:SCHedule?",
  ":SYSTem:TEMPerature:INTernal?",
  ":SYSTem:FAN:LEVel?",
  ":TRIGger:CONTinuous?",
  ":DISPlay:VOLTage:DIGits?",
  ":DISPlay:VOLTage:DIGits? MAX",
  ":SENSe:VOLTage:AC:FREQuency?",
  ":SENSe:DIGitize:VOLTage:COUPling?",
  ":SENSe:DIGitize:VOLTage:DCIRcuit?",
  ":SENSe:DIGitize:VOLTage:SRATe? MAX",
  ":SENSe:FRESistance:OCOMpensated?",
  ":SENSe:FRESistance:RANGe? MAX",
  ":SENSe:RESistance:RANGe? MAX",
  ":SENSe:VOLTage:AC:RANGe? MAX"
]
```

and change `core.local` to `[":TRIGger:CONTinuous RESTart", "logout"]`
(unchanged — but on this model the first command is manual-documented rather
than release-note-inferred).

The last four queries are deliberate: they are the ones whose answers
distinguish a DMM7510 from a DMM6500 by measurement rather than by `*IDN?`
(1 GΩ vs 100 MΩ, 700 V vs 750 V), and `:DISPlay:VOLTage:DIGits? MAX` should
answer `7` here and `6` there.

---

## 5. Confidence, honestly stated

| Area | Confidence in the *command syntax* | Confidence in the *behaviour* |
|---|---|---|
| Shared command set with the DMM6500 | high — manuals agree line for line | medium |
| `:TRIGger:CONTinuous` | **high — fully documented in this model's own manual** | **none — unsent** |
| ACAL subsystem | high — the manual gives the exact query block | none |
| 7.5-digit `DISPlay:DIGits` | high | medium |
| Range differences (1 GΩ, 700 V) | high | medium |
| Digitize coupling / DCIRcuit / pulse trigger | medium — extracted from mangled tables | low |
| `TRANsducer` list missing 2-wire RTD | **low — my extraction was truncated** | none |
| 4-wire offset-compensation range restriction | **unknown — absent from the 7510 table, present in the 6500's** | none |
| **Per-function applicability of sub-nodes** | **none — the PDF tables are graphical** | none |
| **Screen capture** | **none — no documented command exists** | none |
| **Crash safety** | n/a | **none — entirely unmeasured** |
| **Continuous/streaming pattern** | medium | **low** |

The weakest rows are 6, 7, 8 and the bottom four. Rows 6–8 are things I read out
of a badly-extracting PDF and would not bet on; the bottom four are things no
document can answer and only the probe can.
