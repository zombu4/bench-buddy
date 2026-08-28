# Keithley DMM6500 — candidate SCPI command set

> ## NOTHING IN THIS DOCUMENT HAS TOUCHED HARDWARE
>
> Every command below is **UNVERIFIED**. It was transcribed from Keithley's own
> reference manual, not measured on an instrument. No DMM6500 has been on this
> network, so not one line here has the standing that `SPEC.md` has.
>
> `SPEC.md` earned its authority by being probed against a physical 34461A.
> This document has not earned that, and must not be treated as if it had.
>
> **The 34461A's never-send list does not transfer.** `SPEC.md` section 2.2
> lists commands that make *that* instrument go silent and poison the socket.
> That list is a property of Truevolt firmware. It says nothing about a
> DMM6500, whose command set does not even share the same subsystem names. A
> DMM6500 will have its own never-send list, and **it is currently empty because
> nobody has looked** — not because it is short.
>
> **Before any of this is trusted, probe it:**
>
> ```
> python tools/probe_instrument.py <address> --profile keithley-dmm6500 --dry-run
> python tools/probe_instrument.py <address> --profile keithley-dmm6500
> ```
>
> The probe's report supersedes this document the moment it exists.

**Sources.** Everything cited as `[RM p. N]` comes from the *Model DMM6500
6½-Digit Bench/System Digital Multimeter with Scanning Reference Manual*,
document **DMM6500-901-01 Rev. A / April 2018** (Keithley Instruments /
Tektronix). Page numbers are the manual's own section-relative numbers, e.g.
`13-183` is section 13 page 183. Anything else is cited in place. Where a claim
rests on a forum post or a firmware release note rather than the manual, that is
said explicitly.

---

## 0. What is different about this instrument, before any command

Four structural facts that change how a driver has to be written. All four are
from the manual, none is verified here.

1. **It is a dual-language instrument.** `*LANG?` returns one of `SCPI`, `TSP`,
   `SCPI2000` or `SCPI34401`; the factory default is `SCPI` [RM p. 13 of the
   common-command section, and RM p. 2-29]. `*LANG <set>` changes it, **and the
   instrument must be rebooted afterwards** [RM p. 2-29]. Detect the language;
   never switch it without the user asking. The probe tool requires an explicit
   `--set-language` flag for exactly this reason.
   - `SCPI34401` is a Keysight 34401A emulation mode. It is tempting and it is a
     trap: the manual says that in `SCPI2000` or `SCPI34401` "you will not have
     access to some of the extended ranges and other features" [RM p. 2-29].
     It also emulates the **34401A**, not the 34461A, so the app's existing
     command set would not work unmodified anyway.

2. **One controlling interface at a time.** "You can only use one remote
   interface at a time. Although multiple ethernet connections to the instrument
   can be opened, only one can be used to control the instrument at a time"
   [RM p. 2-16]. The 34461A allowed two independent raw sessions and at least
   five concurrent VXI-11 links (`SPEC.md` section 1). Any design that assumes a
   second observer session — including the way crash safety was measured on the
   34461A — has to be re-derived here. **This is a probe item, not a conclusion:
   the manual's sentence may describe a policy rather than a hard refusal.**

3. **There is a dead-socket termination port.** Port **5030**: "All existing
   ethernet connections will be terminated and closed when the connection to the
   dead socket termination port is closed" [RM p. 2-16, p. 2-17]. This is a
   recovery mechanism the 34461A does not have, and it is directly relevant to
   the crash-safety problem — see section 9.

4. **There is no SCPI screen capture.** Searching the whole reference manual for
   a screenshot command finds only "Saving screen captures to a USB flash drive"
   via the front-panel HOME+ENTER keys [RM p. 3-64]. There is no `HCOPy`
   subsystem and no `DISPlay:DATA?`. See section 8 for what there is instead.

---

## 1. Transports

| Port | Protocol | Source |
|---|---|---|
| 23 | Telnet | [RM p. 2-16] |
| 1024 | VXI-11 | [RM p. 2-16] |
| 5025 | Raw socket | [RM p. 2-16] |
| 5030 | Dead socket termination | [RM p. 2-16] |
| 80 | HTTP — web interface, incl. a virtual front panel | [RM p. 2-23, 2-26] |

**HiSLIP is not listed anywhere in the manual.** The 34461A has it on 4880 and
this project does not use it. Treat "the DMM6500 has no HiSLIP" as *likely but
unverified*; `tools/probe_instrument.py` performs a HiSLIP `Initialize`
handshake on 4880 and reports the answer either way.

Note the VXI-11 port is documented as **1024 directly**, whereas the 34461A's
core channel is found through the portmapper on 111. The probe tool tries the
portmapper first and falls back to 1024, and records which one answered.

---

## 2. Function selection and the function list

The DMM6500 does **not** have `CONFigure:`. Functions are selected by writing a
**quoted string** to `SENSe:FUNCtion` [RM p. 13-151]:

```
:SENSe:FUNCtion "VOLTage:DC"        UNVERIFIED  [RM p. 13-151]
:SENSe:FUNCtion?                    UNVERIFIED  [RM p. 13-151]
```

The documented function strings [RM p. 13-151] — 15 of them, against the
34461A's 12:

| String | What it is | Notes |
|---|---|---|
| `VOLTage[:DC]` | DC voltage | default at reset |
| `VOLTage:AC` | AC voltage | |
| `CURRent[:DC]` | DC current | |
| `CURRent:AC` | AC current | |
| `RESistance` | 2-wire resistance | |
| `FRESistance` | 4-wire resistance | |
| `DIODe` | diode | |
| `CAPacitance` | capacitance | |
| `TEMPerature` | temperature | |
| `CONTinuity` | continuity | |
| `FREQuency[:VOLTage]` | frequency | |
| `PERiod[:VOLTage]` | period | |
| `VOLTage[:DC]:RATio` | DC voltage ratio | **no 34461A equivalent** |
| `DIGitize:VOLTage` | digitize voltage | selected via `:SENSe:DIGitize:FUNCtion`, not `:SENSe:FUNCtion` |
| `DIGitize:CURRent` | digitize current | as above |

Digitize functions are selected through a **separate node** [RM p. 13-150]:

```
:SENSe:DIGitize:FUNCtion "VOLTage"   UNVERIFIED  [RM p. 13-150]
:SENSe:DIGitize:FUNCtion?            UNVERIFIED
```

and the two nodes are mutually exclusive: "If you send the query when a
digitize measurement function is selected, this returns `NONE`" [RM p. 13-151],
and vice versa [RM p. 13-150]. **A driver has to query both to know what the
instrument is doing.** That is a genuine structural difference from
`SENS:FUNC?` on the 34461A, which always answers.

### 2.1 A gap I cannot close from the manual

Every per-function command in section 13 prints a "Functions" table listing
which functions it applies to. In the PDF these tables are **graphical** — the
applicable functions are shaded — and text extraction returns all fifteen
entries for every command. So the manual, as I can read it, does **not** tell me
which sub-nodes are legal on which function.

This matters because it is exactly the axis on which the 34461A bit: `SPEC.md`
records that `VOLT:AC:ZERO:AUTO?` and `FRES:ZERO:AUTO?` *hang the socket*, and
that this was got wrong once before being measured.

**I am not going to guess it.** The candidate list below sends every
per-function node against every function and records which combinations answer.
That sweep is the deliverable; the applicability table is its output.

---

## 3. Range control

```
:SENSe:<function>:RANGe[:UPPer] <n>            UNVERIFIED  [RM p. 13-114]
:SENSe:<function>:RANGe[:UPPer] <DEF|MIN|MAX>  UNVERIFIED  [RM p. 13-114]
:SENSe:<function>:RANGe[:UPPer]?               UNVERIFIED
:SENSe:<function>:RANGe[:UPPer]? <DEF|MIN|MAX> UNVERIFIED  [RM p. 13-114]
:SENSe:<function>:RANGe:AUTO <ON|OFF|1|0>      UNVERIFIED  [RM p. 13-112]
:SENSe:<function>:RANGe:AUTO?                  UNVERIFIED
```

**The `MIN`/`MAX` query form is documented** [RM p. 13-114], and the manual
explains the general convention: "If the command has MINimum, MAXimum, and
DEFault options, you can use the query command to determine what the minimum,
maximum, and default values are… the `?` is placed *before* the MINimum,
MAXimum, or DEFault parameter" [RM p. 12-3]. So `SPEC.md` section 2.4's
runtime range-enumeration rule — never hardcode a table, ask `? MIN` and
`? MAX` and multiply by ten — **transfers in principle**. Whether it produces
the right set on this instrument is a measurement, and the probe tool performs
it: it derives the decade list, then *sets each range and reads back what the
instrument actually adopted*.

The manual does print a range table [RM p. 13-115]. It is reproduced here **only
so a probe result can be sanity-checked against it**, and must never be copied
into code:

| Function | Documented ranges |
|---|---|
| DC voltage | 100 mV, 1 V, 10 V, 100 V, 1000 V |
| AC voltage | 100 mV, 1 V, 10 V, 100 V, **750 V** |
| DC current | 10 µA … 3 A (10 A on rear terminals) |
| AC current | 1 mA … 3 A (10 A on rear terminals) |
| 2-wire resistance | 10 Ω … 100 MΩ |
| 4-wire resistance, offset comp. **off** | 1 Ω … 100 MΩ |
| 4-wire resistance, offset comp. **on** | 1 Ω … 10 kΩ |
| Continuity | 1 kΩ fixed |
| Diode | 10 V fixed |
| Capacitance | 1 nF … 1 mF |
| Digitize voltage | 100 mV … 1000 V |
| Digitize current | 10 µA … 3 A (10 A rear) |

Note the trap in row 7: **the available 4-wire range set depends on whether
offset compensation is on.** A cached range list is wrong the moment
`:SENSe:FRESistance:OCOMpensated` changes. Also note [RM p. 13-113]: with the
TERMINALS switch on REAR, autorange is limited to 3 A and never selects 10 A.

Overrange reads back as `9.9e+37` [RM p. 13-114] — not the 34461A's
`+9.9E+37` convention, and worth confirming.

Frequency and period do not have a `RANGe`; they have a **threshold** range
[RM p. 13-136]:

```
:SENSe:FREQuency:THReshold:RANGe <n>        UNVERIFIED  [RM p. 13-136]
:SENSe:FREQuency:THReshold:RANGe:AUTO <b>   UNVERIFIED  [RM p. 13-137]
```

This is the analogue of the 34461A's `FREQ:VOLT:RANG`. **Unverified: I have not
confirmed the exact node name applies to `PERiod` as well as `FREQuency`.**

There is also `:SENSe:<function>:SENSe:RANGe[:UPPer]?` [RM p. 13-130], which
"displays the positive full-scale range that is being used for the sense
measurement" — relevant to ratio and 4-wire. Purpose unclear to me; probe it.

---

## 4. Integration time: NPLC and aperture

```
:SENSe:<function>:NPLCycles <n>              UNVERIFIED  [RM p. 13-108]
:SENSe:<function>:NPLCycles <DEF|MIN|MAX>    UNVERIFIED
:SENSe:<function>:NPLCycles?                 UNVERIFIED
:SENSe:<function>:NPLCycles? <DEF|MIN|MAX>   UNVERIFIED
:SENSe:<function>:APERture <n>               UNVERIFIED  [RM p. 13-83]
:SENSe:<function>:APERture? <DEF|MIN|MAX>    UNVERIFIED
```

**NPLC is continuous, not enumerated.** The documented range is **0.0005 to 15**
at 60 Hz (12 at 50 Hz or 400 Hz) [RM p. 13-108]. The 34461A takes exactly five
values — 0.02, 0.2, 1, 10, 100 — and `app/specs.py` hardcodes them as
`NPLC_OPTIONS`. **That list is meaningless here.** A DMM6500 driver has to treat
NPLC as a continuous quantity bounded by `? MIN` and `? MAX`, or offer its own
presets. This is one of the clearest places the current abstraction breaks.

Aperture and NPLC are two views of one setting: "Changing the NPLC value changes
the aperture time and changing the aperture time changes the NPLC value"
[RM p. 13-109]. On the 34461A `VOLT:DC:APER?` is read-only; here aperture is
settable. Documented aperture ranges by function [RM p. 13-83] are given as
8.333 µs–0.25 s (60 Hz) for most measure functions, 2 ms–273 ms for capacitance,
and `AUTO` or 1 µs–1 ms in 1 µs steps for the digitize functions. **The
extracted table is mangled enough that I do not trust my reading of which row
belongs to which function** — probe `? MIN` and `? MAX` per function instead.

For digitizing there is a sample rate rather than an NPLC [RM p. 13-129]:

```
:SENSe:DIGitize:<VOLTage|CURRent>:SRATe <n>   UNVERIFIED  1000 to 1,000,000 rdg/s
:SENSe:DIGitize:<VOLTage|CURRent>:APERture AUTO
```
"Set the sample rate before setting the aperture" [RM p. 13-129].

### Other per-function settings

All **UNVERIFIED**; applicability per function is unknown (section 2.1).

| Node | Values | Source |
|---|---|---|
| `:SENSe:<f>:AZERo[:STATe]` | `OFF\|0\|ON\|1` — **no `ONCE`** | [RM p. 13-98] |
| `:SENSe:AZERo:ONCE` | separate command, no function prefix | [RM p. 13-140] |
| `:SENSe:<f>:INPutimpedance` | `MOHM10` or `AUTO` — **not the 34461A's 0/1** | [RM p. 13-106] |
| `:SENSe:<f>:DETector:BANDwidth` | **3, 30 or 300 Hz** — **not** the 34461A's 3/20/200 | [RM p. 13-105] |
| `:SENSe:<f>:RELative` / `:RELative:STATe` / `:RELative:ACQuire` | the null function | [RM p. 13-116 – 13-120] |
| `:SENSe:<f>:AVERage[:STATe]` / `:COUNt` / `:TCONtrol` (`REPeat\|MOVing`) / `:WINDow` | a digital filter the 34461A does not have | [RM p. 13-92 – 13-97] |
| `:SENSe:<f>:OCOMpensated` | 4-wire offset compensation — changes the legal range set | [RM p. 13-110] |
| `:SENSe:<f>:LINE:SYNC` | line synchronisation | [RM p. 13-107] |
| `:SENSe:<f>:UNIT` | `VOLT\|DB\|DBM`; temperature `KELVin\|CELSius\|FAHRenheit` | [RM p. 13-139] |
| `:SENSe:<f>:DB:REFerence`, `:DBM:REFerence` | dB/dBm references | [RM p. 13-101, 13-102] |
| `:SENSe:<f>:TRANsducer` | `TCouple\|THERmistor\|RTD\|TRTD\|FRTD\|CJC2001` | [RM p. 13-138] |
| `:SENSe:<f>:TCouple:TYPE`, `:THERmistor`, `:RTD:*` | probe configuration | [RM p. 13-121 – 13-135] |
| `:SENSe:<f>:DELay:AUTO`, `:DELay:USER<n>` | settling delay | [RM p. 13-103, 13-104] |
| `:SENSe:COUNt <n>` | 1 to 1,000,000 readings per measurement request | [RM p. 13-148] |

There is **no `<function>:RES?`** node. The 34461A's signature UI element — the
resolution-aware readout of `SPEC.md` section 5, which dims digits below
`<p>:RES?` — has no direct equivalent I can find. The nearest thing is
`:DISPlay:<function>:DIGits` (`3` to `6` on this model) [RM p. 13-37], which is
a *display* setting the user chooses, not the instrument's reported measurement
resolution. **This is the single hardest thing to port, and I could not resolve
it from the manual.** Flagged again in `docs/multi-model-plan.md`.

---

## 5. The reading buffer model

This is the largest structural difference from the 34461A. There is no
`DATA:POIN?` / `R?` pair. There are named reading buffers.

```
:TRACe:ACTual?                                 UNVERIFIED  [RM p. 13-175]
:TRACe:ACTual? "defbuffer1"                    UNVERIFIED
:TRACe:ACTual:STARt? "defbuffer1"              UNVERIFIED  [RM p. 13-177]
:TRACe:ACTual:END?   "defbuffer1"              UNVERIFIED  [RM p. 13-176]
:TRACe:DATA? <startIndex>, <endIndex>          UNVERIFIED  [RM p. 13-183]
:TRACe:DATA? <start>, <end>, "defbuffer1"      UNVERIFIED
:TRACe:DATA? <start>, <end>, "defbuffer1", READing, RELative   UNVERIFIED
:TRACe:CLEar "defbuffer1"                      UNVERIFIED  [RM p. 13-182]
:TRACe:POINts <n>, "defbuffer1"                UNVERIFIED  [RM p. 13-193]
:TRACe:POINts? "defbuffer1"                    UNVERIFIED
:TRACe:MAKE "<name>", <size>[, STANdard|FULL|WRITable|FULLWRitable]  [RM p. 13-188]
:TRACe:DELete "<name>"                         UNVERIFIED  [RM p. 13-186]
:TRACe:FILL:MODE <CONTinuous|ONCE>[, "<buf>"]  UNVERIFIED  [RM p. 13-186]
```

Facts that shape a driver, all from the manual:

- Two buffers exist from the factory: `defbuffer1` and `defbuffer2`. **Initial
  capacity 100,000 readings each** [RM p. 7-7]. Compare the 34461A's ~10,400
  measured readings. Overflow is a very different problem at this size.
- `defbuffer1` defaults to fill mode **`CONT`inuous** [RM p. 13-186] — the
  oldest data is overwritten once it fills. It does not saturate the way the
  34461A does, and it does not error. **This is the closest thing to a free
  safety net for a runaway acquisition, and it is a very different failure mode
  from `-365`.**
- A user-defined buffer defaults to fill mode `ONCE` [RM p. 13-188], and when a
  fill-once buffer is full "event code 4915, 'Attempting to store past capacity
  of reading buffer'" is generated [RM p. 7-7].
- Maximum user buffer is 7,000,020 readings in `STANdard` style [RM p. 13-188];
  to reach 7,000,000 you must power-cycle and shrink the default buffers to 10
  [RM p. 7-7]. Not something an app should do.
- **Reading data does not remove it.** `:TRACe:DATA?` takes explicit
  `startIndex`/`endIndex` and is idempotent. The 34461A's `R?` is destructive —
  it drains. So a streaming driver here has to **track its own cursor** against
  `:TRACe:ACTual:END?`, and cope with the start index moving when a continuous
  buffer wraps (`:TRACe:ACTual:STARt?` exists precisely for this).
- Up to 14 comma-delimited buffer elements per data point; `READing` is the
  default; `RELative` gives relative timestamps [RM p. 13-183/184].
- `:FORMat[:DATA] <ASCii|REAL|SREal>` [RM p. 13-44] affects `READ?`, `FETCh?`,
  `MEASure:<f>?` and `TRACe:DATA?`. Two things matter:
  - in `REAL`/`SREal` only `READing`, `RELative` and `EXTRa` are legal elements,
    and asking for others gives error 1133 [RM p. 13-183];
  - **binary responses "start with `#0` and end with a new line"** [RM p. 13-44].
    That is an *indefinite-length* IEEE-488.2 block. The app's
    `ScpiLink.query_block` parses the 34461A's *definite*-length
    `#<n><len><bytes>`. Both shapes have to be understood. (The probe tool's
    block reader already handles both.)

Statistics come from the buffer rather than a `CALCulate` subsystem
[RM p. 13-198 – 13-203], all **UNVERIFIED**:

```
:TRACe:STATistics:AVERage? "defbuffer1"
:TRACe:STATistics:MINimum? "defbuffer1"
:TRACe:STATistics:MAXimum? "defbuffer1"
:TRACe:STATistics:PK2Pk?   "defbuffer1"
:TRACe:STATistics:STDDev?  "defbuffer1"
:TRACe:STATistics:SPAN?    "defbuffer1"
:TRACe:STATistics:CLEar
```

This is a clean structural win over `CALC:AVER:*`: statistics are a property of a
named buffer, not of a global math block.

---

## 6. Triggering: the trigger-block model

There is no `TRIG:SOUR` / `TRIG:COUN` / `SAMP:COUN` triple. There is a
programmable trigger model built from numbered blocks, plus a set of
**templates** loaded by name [RM p. 13-259 – 13-269].

```
:TRIGger:LOAD "SimpleLoop", <count>[, <delay>[, "<buffer>"]]     UNVERIFIED  [RM p. 13-267]
:TRIGger:LOAD "DurationLoop", <duration>[, <delay>[, "<buffer>"]] UNVERIFIED [RM p. 13-260]
:TRIGger:LOAD "LoopUntilEvent", <event>, <position>[, <clear>][, <delay>][, "<buffer>"]  [RM p. 13-265]
:TRIGger:LOAD "ConfigList", "<list>"[, <delay>[, "<buffer>"]]    UNVERIFIED  [RM p. 13-259]
:TRIGger:LOAD "GradeBinning", ...                                UNVERIFIED  [RM p. 13-262]
:TRIGger:LOAD "SortBinning", ...                                 UNVERIFIED  [RM p. 13-268]
:TRIGger:LOAD "LogicTrigger", ...                                UNVERIFIED  [RM p. 13-264]
:TRIGger:LOAD "Empty"                                            UNVERIFIED  [RM p. 13-261]
:INITiate                                                        UNVERIFIED  [RM p. 13-45, 13-212]
:ABORt                                                           UNVERIFIED  [RM p. 13-45, 13-212]
:TRIGger:STATe?                                                  UNVERIFIED  [RM p. 13-271]
:TRIGger:BLOCk:LIST?                                             UNVERIFIED  [RM p. 13-234]
:TRIGger:PAUSe / :TRIGger:RESume                                 UNVERIFIED  [RM p. 13-270]
```

Parameter limits, from the manual: `SimpleLoop` `<count>` is "the number of
measurements the instrument will make" with no documented ceiling;
`DurationLoop` `<duration>` is 500 ns to 100 ks; `<delay>` on both is 167 ns to
10 ks and defaults to 0 [RM p. 13-260, 13-267].

`:TRIGger:STATe?` "returns the trigger state and the block that the trigger
model last executed", e.g. `IDLE;IDLE;9`, and the documented states are Idle,
Running, Waiting, Empty, Paused, Building, Failed, Aborting, Aborted
[RM p. 13-271]. **This is strictly better than anything the 34461A offers** —
the app currently infers "is it running" from whether readings arrive.

### 6.1 How continuous acquisition is actually done

**I do not have a confident answer, and I am not going to invent one.**

Here is what the manual supports and what it does not:

- `TRIG:COUN INF` **has no equivalent**. There is no infinite count parameter
  documented on any template.
- `"DurationLoop"` runs for a **specified time**, up to 100 ks (about 27 hours)
  [RM p. 13-260]. That is the closest documented thing to "run until I stop
  you", and it has a natural deadman built in — it ends on its own.
- `"SimpleLoop"` with a large `<count>` is the other candidate. `SENSe:COUNt`
  is documented up to 1,000,000 [RM p. 13-148], which suggests counts of that
  order are acceptable, but **the manual does not state a maximum for
  `SimpleLoop`'s count and I did not find one.**
- The front panel's own "Continuous Measurement" mode is explicitly **not
  available remotely**: "The continuous measurement method is only available
  when you are controlling the instrument locally (through the front panel)"
  [RM p. 5-41].

So the most likely streaming shape is:

```
:ABORt
*CLS
:SENSe:FUNCtion "VOLTage:DC"
:SENSe:VOLTage:NPLCycles 1
:TRACe:CLEar "defbuffer1"
:TRIGger:LOAD "DurationLoop", <seconds>, 0, "defbuffer1"
:INITiate
loop:  end = :TRACe:ACTual:END? "defbuffer1"
       if end >= cursor:  :TRACe:DATA? cursor, end, "defbuffer1", READing
       cursor = end + 1
       re-arm before <seconds> elapses
```

**Every line of that is a hypothesis.** In particular I do not know: whether
re-arming requires an `ABORt` first (the 34461A refuses `TRIG:COUN` while
measuring — error `+263` — and something similar is plausible here); whether the
buffer index resets on re-arm; or how large a `SimpleLoop` count is accepted.
The probe tool measures throughput with `SimpleLoop, 20000` deliberately —
finite, well under the 100,000-reading buffer, and disposable if it turns out to
be the wrong shape.

---

## 7. Math, limits, display

```
:CALCulate[1]:<function>:MATH:STATe <ON|OFF>          UNVERIFIED  [RM p. 13-31]
:CALCulate[1]:<function>:MATH:FORMat <MXB|PERCent|RECiprocal>  UNVERIFIED  [RM p. 13-25]
:CALCulate[1]:<function>:MATH:MBFactor <b>            UNVERIFIED  [RM p. 13-26]
:CALCulate[1]:<function>:MATH:MMFactor <m>            UNVERIFIED  [RM p. 13-28]
:CALCulate[1]:<function>:MATH:PERCent <n>             UNVERIFIED  [RM p. 13-29]
:CALCulate2:<function>:LIMit<Y>:STATe <ON|OFF>        UNVERIFIED  [RM p. 13-22]  Y is 1 or 2
:CALCulate2:<function>:LIMit<Y>:LOWer[:DATA] <n>      UNVERIFIED  [RM p. 13-20]
:CALCulate2:<function>:LIMit<Y>:UPPer[:DATA] <n>      UNVERIFIED  [RM p. 13-24]
:CALCulate2:<function>:LIMit<Y>:FAIL?                 UNVERIFIED  [RM p. 13-19]
:CALCulate2:<function>:LIMit<Y>:CLEar[:IMMediate]     UNVERIFIED  [RM p. 13-18]
:CALCulate2:<function>:LIMit<Y>:CLEar:AUTO <ON|OFF>   UNVERIFIED  [RM p. 13-16]
:CALCulate2:<function>:LIMit<Y>:AUDible <...>         UNVERIFIED  [RM p. 13-15]
```

Notes:
- The math offering is `mx+b`, percent and reciprocal. The 34461A's dB/dBm live
  on `SENSe:<f>:UNIT` and `:DB:REFerence` instead — a different place entirely.
- Null is `:SENSe:<f>:RELative`, not `CALC:SCAL`.
- Statistics are `:TRACe:STATistics:*` (section 5), not `CALC:AVER:*`.
- **There is no histogram command.** The 34461A's `CALC:TRAN:HIST:*` has no
  counterpart. The DMM6500 draws a histogram on its own screen
  (`:DISPlay:SCReen HISTogram`) but I found no way to read the bins back. The
  app would have to compute its own histogram from buffer data — which it can,
  since it already holds the readings.

Display [RM p. 13-36 – 13-41]:

```
:DISPlay:SCReen <HOME|HOME_LARGe_reading|READing_table|GRAPh|HISTogram|
                 SWIPE_FUNCtions|SWIPE_GRAPh|SWIPE_SECondary|SWIPE_SETTings|
                 SWIPE_STATistics|SWIPE_USER|SWIPE_CHANnel|SWIPE_NONSwitch|
                 SWIPE_SCAN|CHANNEL_CONTrol|CHANNEL_SETTings|CHANNEL_SCAN|
                 PROCessing>                          UNVERIFIED  [RM p. 13-40]
:DISPlay:USER1:TEXT "<up to 20 chars>"                UNVERIFIED  [RM p. 13-41]
:DISPlay:USER2:TEXT "<up to 32 chars>"                UNVERIFIED  [RM p. 13-41]
:DISPlay:CLEar                                        UNVERIFIED  [RM p. 13-37]
:DISPlay:LIGHt:STATe <ON100|ON75|ON50|ON25|OFF|BLACkout>  UNVERIFIED [RM p. 13-38]
:DISPlay:<function>:DIGits <3|4|5|6>                  UNVERIFIED  [RM p. 13-37]
:DISPlay:READing:FORMat <...>                         UNVERIFIED  [RM p. 13-39]
:DISPlay:BUFFer:ACTive "<bufferName>"                 UNVERIFIED  [RM p. 13-36]
```

`:DISPlay:SCReen PROCessing` — "Go to a screen that uses minimal CPU resources"
[RM p. 13-40] — is worth noting: it is an explicit lever for reducing display
load during fast acquisition. The 34461A has nothing like it.

`:DISPlay:SCReen` is **command only** [RM p. 13-40] — there is no query. The app
cannot read back which screen is showing, only set it.

---

## 8. Screen capture — what replaces `HCOP:SDUM:DATA?`

**Probably nothing, over SCPI.** As stated in section 0, the reference manual
documents no screen-capture command. The only capture route it names is the
front panel writing a file to a USB stick [RM p. 3-64].

What does exist is the **virtual front panel** in the web interface. The manual
describes it for the DMM7510 in more detail than for the DMM6500, but the
feature is the same generation of firmware [DMM7510 RM p. 2-30]:

- it "allows you to control the instrument from a computer as if you were using
  the front panel";
- "The default screen display resolution of 800 x 480 is reduced to 400 x 240
  resolution when high resolution is cleared";
- "You can display the instrument display only … by right-clicking and selecting
  **Screen only**";
- "You can download a screen capture by right-clicking and selecting
  **Download screenshot**."

So the instrument **does** serve its real screen as an image over HTTP. **The URL
is not documented in either manual and I could not find it published anywhere.**
I am not going to guess it and present the guess as a command.

`tools/probe_instrument.py` therefore does two things:
1. sends `:HCOPy:SDUMp:DATA?` and `:DISPlay:DATA?` once each — the two shapes
   other vendors use — so the report can say "asked, no answer" rather than
   "assumed absent"; and
2. issues plain HTTP `GET`s against a short list of plausible paths
   (`/screenshot.png`, `/screen.png`, `/screen`, `/getScreen`, `/screenshot`,
   `/vfp/screen.png`, `/lxi/screenshot`) and reports which, if any, returned an
   image, with its content type and size.

**If none of them hit, the real answer is to open the virtual front panel in a
browser with the network tab open and read the URL off the wire.** That is a
two-minute job on the bench and it is the honest way to get this. Until then:
capture is unsupported on this model.

---

## 9. Crash safety and the idle keepalive

These are the two hardest requirements to port, because both were solved on the
34461A by measurement rather than documentation.

### 9.1 Does the acquisition stop when the client dies?

**Unknown.** `IO-DISCIPLINE.md` records that on the 34461A the raw socket and
HiSLIP both leave an acquisition running for at least 60 s after a hard client
kill, while VXI-11 aborts it and clears reading memory in 2 s — because the
instrument's RPC server performs a device clear when the last VXI-11 link is
destroyed. **Nothing in the DMM6500 manual says whether its VXI-11
implementation does the same.** It is a plausible bet, since it is standard
VXI-11 server behaviour, and it is only a bet.

Three DMM6500-specific things change the shape of the problem:

1. **Dead socket termination on port 5030** [RM p. 2-16] is a documented way for
   a *new* client to force-close a stale session. That is not a deadman — it
   does not fire on its own — but it is a recovery path the 34461A lacks, and it
   may make a stale session survivable rather than fatal.
2. **`defbuffer1` defaults to continuous fill** [RM p. 13-186], so a runaway
   acquisition wraps rather than overflowing. The `-365` class of hazard largely
   does not exist here.
3. **`:TRIGger:LOAD "DurationLoop", <seconds>`** gives the instrument an
   intrinsic deadman that `TRIG:COUN INF` never could: the acquisition ends by
   itself at a time chosen when it started.

`tools/probe_instrument.py` measures (1) directly, the same way it was measured
for the 34461A: a child process arms a `SimpleLoop` acquisition, is killed with
`TerminateProcess` so no handler of ours can run, and a *fresh* session then
looks at `:TRIGger:STATe?` and `:TRACe:ACTual?` twice, six seconds apart.

### 9.2 Keeping the front panel alive while connected

`IO-DISCIPLINE.md` rule 1 exists because a connected-but-idle app leaves the
34461A's panel frozen on a stale reading. **Whether the DMM6500 has the same
problem is unknown** — its display and its measurement engine are much less
coupled than Truevolt's, and its "remote" indicator may not freeze the reading
at all. Measure it before designing around it.

If it does, the same shape should work: a low-rate `DurationLoop` or
`SimpleLoop` that the app drains and renews.

### 9.3 Returning to local — the `SYST:LOC` equivalent

There is **no `:SYSTem:LOCal`** in the DMM6500 reference manual. What there is:

- **`:TRIGger:CONTinuous <OFF|AUTO|RESTart>`** — documented in the *DMM7510*
  manual [DMM7510 RM p. 11-191], where `RESTart` means "Place the instrument
  into local control and start continuous measurements". That is *exactly* the
  hand-back this project needs.
  - It is **absent from the DMM6500 Rev. A (April 2018) manual**, but the
    DMM6500 **firmware 1.7.0 release notes (November 2019)** list under
    Enhancements → New commands and options: *"Added remote commands to set
    continuous measurement"*
    (`DMM6500-FRP-V1.7.0`, download.tek.com).
  - A user on eevblog.com's *"Keithley DMM6500 SCPI — Run mode and release
    instrument"* thread reports `TRIG:CONT REST` followed by `logout` working on
    a DMM6500. **That is a forum report, not a manual, and not a measurement.**
- **`logout`** — a bare word, not a SCPI command. It appears in the
  `:SYSTem:ACCess` example [RM p. 13-158] as the counterpart of `login admin`,
  and only matters when access is not `FULL`.

So the candidate hand-back sequence, in the order `IO-DISCIPLINE.md` rule 6
demands (drain the error queue *before*, never after, because any command puts
the meter back into remote):

```
:ABORt
:SYSTem:ERRor?          <- drain here
:TRIGger:CONTinuous RESTart
logout
```

**`:TRIGger:CONTinuous RESTart` must be tested in isolation on a throwaway
session first**, exactly as `SYST:LOC` was on the 34461A (`IO-DISCIPLINE.md`
rule 6). It is the highest-value single probe on this instrument.

---

## 10. Errors, status, system

```
*IDN?     -> "KEITHLEY INSTRUMENTS,MODEL nnnn,xxxxxxxx,yyyyyy"  [RM p. 14, common commands]
*RST *CLS *OPC *OPC? *WAI *TRG *SAV *RCL *ESE *ESR? *SRE *STB? *TST? *LANG   UNVERIFIED
```

**There is no `*OPT?`.** Neither reference manual lists it. The app queries it
for the 34461A; a DMM6500 driver must not.

```
:SYSTem:ERRor[:NEXT]?          UNVERIFIED  [RM p. 13-164]
:SYSTem:ERRor:CODE[:NEXT]?     UNVERIFIED  [RM p. 13-165]
:SYSTem:ERRor:COUNt?           UNVERIFIED  [RM p. 13-165]
:SYSTem:EVENtlog:NEXT? <ERRor|WARNing|INFormational|ALL>   UNVERIFIED  [RM p. 13-167]
:SYSTem:EVENtlog:COUNt? <...>  UNVERIFIED  [RM p. 13-166]
:SYSTem:LFRequency?            UNVERIFIED  [RM p. 13-170]
:SYSTem:VERSion?               UNVERIFIED  [RM p. 13-175]
:SYSTem:TIME? / :SYSTem:TIME <...>          UNVERIFIED  [RM p. 13-174]
:SYSTem:BEEPer[:IMMediate] <freq 20..8000>, <seconds 0.001..100>  UNVERIFIED [RM p. 13-159]
:SYSTem:ACCess <FULL|EXCLusive|PROTected|LOCKout>  UNVERIFIED  [RM p. 13-158]
:SYSTem:COMMunication:LAN:MACaddress?       UNVERIFIED  [RM p. 13-163]
:SYSTem:CLEar                  UNVERIFIED  clears the event log [RM p. 13-162]
:STATus:OPERation:CONDition?   UNVERIFIED  [RM p. 13-152]
:STATus:QUEStionable:CONDition? UNVERIFIED [RM p. 13-155]
:ROUTe:TERMinals?              UNVERIFIED  front/rear switch [RM p. 13-81]
```

`:SYSTem:BEEPer` takes **two** parameters (frequency and duration) where the
34461A's `SYST:BEEP` takes none. An event *log* exists alongside the error
queue, with severity filtering — richer than `SYST:ERR?`, and worth using.

There is **no calibration query subsystem** equivalent to the 34461A's
`CAL:DATE?` / `CAL:COUN?` / `CAL:STR?` in the SCPI reference. The DMM6500 has no
autocalibration (the DMM7510 does — see that document's ACAL section).

Scanning-card commands (`:ROUTe:SCAN:*`, `:ROUTe[:CHANnel]:*`,
`:SYSTem:CARD1:*`, `:TRACe:CHANnel:MATH`, `:DISPlay:WATCh:CHANnels`) exist on
the DMM6500 and not on the DMM7510 [RM p. 13-45 – 13-81]. They are out of scope
for this app but their presence is a useful `*IDN?`-independent fingerprint.

---

## 11. Ready-to-run candidate list

The probe tool ships this as the built-in profile `keithley-dmm6500`:

```
python tools/probe_instrument.py <address> --profile keithley-dmm6500 --dry-run
python tools/probe_instrument.py <address> --profile keithley-dmm6500
```

To edit it without touching the tool, save the JSON below and pass
`--candidates dmm6500.json`. The schema is printed by
`python tools/probe_instrument.py --help`.

```json
{
  "name": "keithley-dmm6500-manual",
  "description": "DMM6500 candidates, hand-edited",
  "source": "DMM6500-901-01 Rev. A. UNVERIFIED.",
  "verified": false,
  "match": ["DMM6500"],
  "never_send": [],
  "core": {
    "idn": "*IDN?",
    "clear": "*CLS",
    "error": ":SYSTem:ERRor?",
    "abort": ":ABORt",
    "language": "*LANG?",
    "state": ":TRIGger:STATe?",
    "local": [":TRIGger:CONTinuous RESTart", "logout"],
    "reading_count": ":TRACe:ACTual? \"defbuffer1\""
  },
  "queries": [
    "*IDN?", "*LANG?", "*ESR?", "*STB?", "*OPT?",
    ":SYSTem:ERRor:COUNt?", ":SYSTem:ERRor?", ":SYSTem:VERSion?",
    ":SYSTem:LFRequency?", ":SYSTem:ACCess?", ":SYSTem:TIME? 1",
    ":SYSTem:COMMunication:LAN:MACaddress?", ":SYSTem:EVENtlog:COUNt? ALL",
    ":SYSTem:CARD1:IDN?", ":ROUTe:TERMinals?", ":ROUTe:SCAN:STATe?",
    ":STATus:OPERation:CONDition?", ":STATus:QUEStionable:CONDition?",
    ":FORMat:DATA?", ":FORMat:BORDer?", ":FORMat:ASCii:PRECision?",
    ":SENSe:FUNCtion?", ":SENSe:DIGitize:FUNCtion?", ":SENSe:COUNt?",
    ":SENSe:CONFiguration:LIST:CATalog?",
    ":TRACe:ACTual?", ":TRACe:ACTual:STARt?", ":TRACe:ACTual:END?",
    ":TRACe:POINts?", ":TRACe:FILL:MODE?", ":TRACe:LOG:STATe?",
    ":TRACe:STATistics:AVERage?", ":TRACe:STATistics:MINimum?",
    ":TRACe:STATistics:MAXimum?", ":TRACe:STATistics:PK2Pk?",
    ":TRACe:STATistics:STDDev?", ":TRACe:STATistics:SPAN?",
    ":TRIGger:STATe?", ":TRIGger:BLOCk:LIST?", ":TRIGger:CONTinuous?",
    ":DISPlay:BUFFer:ACTive?", ":DISPlay:LIGHt:STATe?",
    ":DISPlay:READing:FORMat?", ":DISPlay:VOLTage:DIGits?",
    ":CALCulate:VOLTage:MATH:STATe?", ":CALCulate:VOLTage:MATH:FORMat?",
    ":CALCulate2:VOLTage:LIMit1:STATe?", ":CALCulate2:VOLTage:LIMit1:LOWer?",
    ":CALCulate2:VOLTage:LIMit1:UPPer?", ":CALCulate2:VOLTage:LIMit1:FAIL?",
    ":SENSe:VOLTage:NPLCycles?", ":SENSe:VOLTage:NPLCycles? MIN",
    ":SENSe:VOLTage:NPLCycles? MAX", ":SENSe:VOLTage:APERture?",
    ":SENSe:VOLTage:APERture? MIN", ":SENSe:VOLTage:APERture? MAX",
    ":SENSe:VOLTage:RANGe?", ":SENSe:VOLTage:RANGe? MIN",
    ":SENSe:VOLTage:RANGe? MAX", ":SENSe:VOLTage:RANGe:AUTO?",
    ":SENSe:VOLTage:AZERo:STATe?", ":SENSe:VOLTage:INPutimpedance?",
    ":SENSe:VOLTage:LINE:SYNC?", ":SENSe:VOLTage:AVERage:STATe?",
    ":SENSe:VOLTage:AVERage:COUNt?", ":SENSe:VOLTage:AVERage:TCONtrol?",
    ":SENSe:VOLTage:RELative?", ":SENSe:VOLTage:RELative:STATe?",
    ":SENSe:VOLTage:DELay:AUTO?", ":SENSe:VOLTage:UNIT?",
    ":SENSe:VOLTage:DB:REFerence?", ":SENSe:VOLTage:DBM:REFerence?",
    ":SENSe:VOLTage:AC:DETector:BANDwidth?",
    ":SENSe:RESistance:OCOMpensated?", ":SENSe:FRESistance:OCOMpensated?",
    ":SENSe:FREQuency:APERture?", ":SENSe:FREQuency:THReshold:RANGe?",
    ":SENSe:FREQuency:THReshold:RANGe:AUTO?",
    ":SENSe:TEMPerature:TRANsducer?", ":SENSe:TEMPerature:UNIT?",
    ":SENSe:DIODe:BIAS:LEVel?",
    ":SENSe:DIGitize:VOLTage:SRATe?", ":SENSe:DIGitize:VOLTage:SRATe? MAX",
    ":SENSe:DIGitize:VOLTage:APERture?"
  ],
  "throughput": {
    "setup": [":ABORt", "*CLS", ":SENSe:FUNCtion \"VOLTage:DC\"",
              ":SENSe:VOLTage:RANGe:AUTO ON", ":TRACe:CLEar \"defbuffer1\""],
    "points": [
      {"label": "NPLC 1",      "commands": [":SENSe:VOLTage:NPLCycles 1"]},
      {"label": "NPLC 0.2",    "commands": [":SENSe:VOLTage:NPLCycles 0.2"]},
      {"label": "NPLC 0.0005", "commands": [":SENSe:VOLTage:NPLCycles 0.0005"]}
    ],
    "start": ":TRIGger:LOAD \"SimpleLoop\", 20000, 0, \"defbuffer1\"",
    "start_extra": [":INITiate"],
    "count_query": ":TRACe:ACTual? \"defbuffer1\"",
    "fetch": ":TRACe:DATA? {start}, {end}, \"defbuffer1\", READing",
    "fetch_indexed": true,
    "stop": [":ABORt", ":TRACe:CLEar \"defbuffer1\""]
  },
  "screen": {
    "scpi": [
      {"label": "HCOPy", "setup": [], "query": ":HCOPy:SDUMp:DATA?", "timeout": 10.0},
      {"label": "DISPlay:DATA", "setup": [], "query": ":DISPlay:DATA?", "timeout": 10.0}
    ],
    "http": ["/screenshot.png", "/screen.png", "/screen", "/getScreen",
             "/screenshot", "/vfp/screen.png", "/lxi/screenshot"]
  },
  "crash": {
    "arm": [":ABORt", "*CLS", ":SENSe:FUNCtion \"VOLTage:DC\"",
            ":SENSe:VOLTage:NPLCycles 1", ":TRACe:CLEar \"defbuffer1\"",
            ":TRIGger:LOAD \"SimpleLoop\", 100000, 0, \"defbuffer1\"",
            ":INITiate"],
    "observe": [":TRIGger:STATe?", ":TRACe:ACTual? \"defbuffer1\""],
    "stop": [":ABORt", "*CLS", ":TRACe:CLEar \"defbuffer1\""]
  }
}
```

The built-in profile additionally sweeps all fifteen functions and, for each,
`? MIN` / `? MAX` plus a set-and-read-back of every derived range, which is how
section 2.1's applicability gap gets closed.

---

## 12. Confidence, honestly stated

| Area | Confidence in the *command syntax* | Confidence in the *behaviour* |
|---|---|---|
| Function selection, `SENSe:FUNCtion` | high — quoted, well documented | high |
| Range, `RANGe`/`RANGe:AUTO`, `? MIN`/`? MAX` | high | medium — decade enumeration is an inference |
| NPLC / aperture | high | medium — the aperture table extracted badly |
| Buffer model, `TRACe:*` | high | medium — cursor and wrap behaviour untested |
| Trigger templates, `TRIG:LOAD` | high | **low for continuous acquisition** — see 6.1 |
| Math, limits | high | medium |
| Display | high | medium — `DISPlay:SCReen` has no query |
| Statistics | high | medium |
| **Per-function applicability of sub-nodes** | **none — unreadable from the PDF** | none |
| **Screen capture** | **none — no documented command exists** | none |
| **Crash safety** | n/a | **none — entirely unmeasured** |
| **Return to local** | medium — 7510-documented, 6500 by release note + forum | **none** |
| **Continuous/streaming pattern** | medium | **low** |

The bottom four rows are where a driver written from this document alone would
go wrong, and they are precisely what `tools/probe_instrument.py` measures.
