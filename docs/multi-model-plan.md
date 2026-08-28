# Supporting three instruments — integration note

Scope: what the driver layer would have to become to carry a **Keysight 34461A**,
a **Keithley DMM6500** and a **Keithley DMM7510**, what is 34461A-specific in the
code today, and the order to do it in.

**This is a plan, not an implementation.** Nothing under `app/` is touched by it.
The candidate command sets live in `docs/keithley-dmm6500.md` and
`docs/keithley-dmm7510.md` and are entirely **unverified** — no Keithley has been
on the network. Step 3 therefore depends on step 1: until a probe report exists,
there is nothing to build a driver against but guesswork, which is exactly what
this project's command sets were written to avoid.

---

## 1. What is 34461A-specific today

`app/models.py` already states the principle and is the seam the rest of this
builds on: one `ModelSupport` entry, `verified` only when probed, and a refusal
to infer support from a family name. What follows is the list of places that
would actually have to change, and *why each one is a real fork rather than a
parameter*.

| Where | What is 34461A-specific | Why it is not a parameter |
|---|---|---|
| `app/specs.py` | `FUNCS`, twelve functions with `CONF:`-based selection, sense-node prefixes, `NPLC_OPTIONS = [0.02, 0.2, 1, 10, 100]`, `BAND_OPTIONS = [3, 20, 200]`, `AZERO_OPTIONS` including `ONCE`, `DISPLAY_VIEWS` | The Keithleys select functions with a **quoted string** to `SENSe:FUNCtion`, have **fifteen** functions including two digitize modes and a ratio mode, treat NPLC as a **continuous** 0.0005–15 range rather than five values, use **3/30/300 Hz** AC bandwidth, have **no `ONCE`** on autozero, and have a completely different display-screen enumeration. Every constant in this file is wrong for them. |
| `app/scpi.py` `FORBIDDEN` (line 133) and `check_allowed()` | 28 never-send entries from `SPEC.md` §2.2 | Model-specific in both directions. The Keithleys' never-send list is **empty because nobody has looked**, not because it is short. Shipping an empty list as if it were a verified one would be the exact failure `IO-DISCIPLINE.md` was written after. |
| `app/instrument.py` screen capture (≈ line 2441) and `app/screen.py` | `HCOP:SDUM:DATA:FORM BMP` + `HCOP:SDUM:DATA?`, and a decoder for 480×289 16 bpp X1R5G5B5 top-down BMP | **Neither Keithley documents any SCPI screen-capture command.** The screen exists only behind the web interface's virtual front panel, at an undocumented URL. There is nothing to parameterise. |
| `app/instrument.py` streaming and keepalive (`TRIG:COUN INF`, `SAMP:COUN`, `INIT`, `DATA:POIN?`, `R? <n>`) | The whole acquisition model | The Keithleys have no `TRIG:COUN`, no `SAMP:COUN` and no `R?`. They have named reading buffers and a trigger-block model loaded from templates. `R?` is **destructive** (it drains); `TRACe:DATA? <start>, <end>` is **indexed and idempotent**, so the driver has to hold its own cursor and handle the buffer wrapping. Different shape, not different strings. |
| `app/scpi.py` `Vxi11Transport.crash_safe = True` (line 707) and `ScpiLink.crash_safe` (line 1069) | The claim that VXI-11 makes a client crash-safe | Measured on the 34461A only (`IO-DISCIPLINE.md`). It is a property of *that* instrument's RPC server, not of VXI-11. `crash_safe` currently reads as a transport constant; it must become a **per-model, per-transport measured fact**, defaulting to `False`. Today, pointing the app at a Keithley would silently assert crash safety that has never been observed — and `crash_safe` selects between `TRIG:COUN INF` and a finite renewed count, so a wrong answer there is a runaway acquisition. |
| `app/instrument.py` return-to-local | `SYST:LOC`, and the rule that it is the last command on the link | Neither Keithley has `SYSTem:LOCal`. The candidate is `:TRIGger:CONTinuous RESTart` — documented for the DMM7510, release-note-inferred for the DMM6500 — possibly followed by `logout`. Unverified. |
| `app/scpi.py` `query_block()` | IEEE-488.2 **definite**-length `#<n><len><bytes>` | Keithley binary responses "start with `#0` and end with a new line" — **indefinite** length. Both shapes need parsing. (`tools/probe_instrument.py` already reads both; that reader is the reference.) |
| `app/instrument.py` math/stats/histogram | `CALC:AVER:*`, `CALC:SCAL:*`, `CALC:TRAN:HIST:*` | Keithley statistics are a property of a **named buffer** (`:TRACe:STATistics:*`), null is `:SENSe:<f>:RELative`, dB/dBm live on `:SENSe:<f>:UNIT`, and there is **no readable histogram at all** — the app would have to bin its own data. |
| The resolution-aware readout (`SPEC.md` §5, `<p>:RES?`) | `<p>:RES?` | **No Keithley equivalent exists.** `:DISPlay:<f>:DIGits?` is a user display preference, not a reported measurement resolution. `SPEC.md` §5 forbids fabricating an uncertainty figure, so on a Keithley the `resolution` field must be `null` and the band hidden — the behaviour the spec already defines for CAP/FREQ/PER/TEMP. **This is the app's signature element and it degrades on two of the three instruments. Decide deliberately, do not discover it in the UI.** |

---

## 2. What the abstraction has to look like

Not "a driver interface with a config dict". Two of the differences above are
*shapes*, not values, so the seam has to be at the level of operations.

**A `Driver` protocol, with a per-model implementation.** The right split, from
the table above:

- **Stays shared** — `app/scpi.py`'s transports, rate limiter, timeout-rebuild
  discipline, and the `ScpiLink` surface; the rate ceiling; the gate; the UI;
  the chart; logging. None of that is model-specific and none of it should move.
- **Becomes per-model** — everything that names a SCPI node. Concretely:

```
Driver
  identity()            -> model, serial, firmware, language, options
  functions()           -> the function table  (replaces app/specs.py as a constant)
  select(func)          / read_config(func)
  ranges(func)          -> enumerated at runtime, never a table
  set_range / set_nplc / set_aperture / set_autozero / ...
  stream_start(config)  -> opaque session handle
  stream_drain(handle)  -> list[float]        (drain vs cursor is the driver's problem)
  stream_stop(handle)
  idle_keepalive_*      (or a declaration that the model needs none)
  statistics()          -> avg/min/max/ptp/sdev, or None
  histogram()           -> bins, or None      (None means the app bins its own)
  capture_screen()      -> PNG bytes, or None (None means the UI hides the button)
  restore_and_release() -> abort, restore, local, free-running
  capabilities          -> a set of feature flags the UI reads
```

**Three rules that matter more than the shape of the protocol:**

1. **`crash_safe` becomes a measured fact, per model *and* transport, defaulting
   to `False`.** Not a transport class attribute. Where the app currently reads
   `ScpiLink.crash_safe` to choose `TRIG:COUN INF` over a finite renewed count,
   it must read the driver's measured value, and an unmeasured model gets the
   conservative branch.
2. **The never-send list moves into the driver**, and an empty list is a
   *refusal to run unverified commands*, not permission. `app/models.py`'s
   `verified` flag already carries the right semantics — extend it so the
   passthrough console's short-timeout expert path is the only way to send an
   unlisted command on an unverified model.
3. **`capabilities` drives the UI, not `if model ==`.** Screen capture,
   histogram, resolution band and AC bandwidth are each absent on at least one
   of the three. The UI should hide what the driver does not offer, so a missing
   feature is a missing control rather than an error dialog.

The function table stops being a module-level constant and becomes something a
driver returns, built partly from what the probe measured. `app/specs.py` keeps
the `FunctionSpec` dataclass and loses the `FUNCS` dict.

---

## 3. Order of work

**Nothing below step 2 may start before step 1 has produced a report.** That is
the whole point.

1. **Probe both meters.** `python tools/probe_instrument.py <addr> --profile
   keithley-dmm6500` (then `--profile keithley-dmm7510`). Do the `--dry-run`
   first, read what it intends to send, then run it. Output: a JSON + Markdown
   report shaped like `SPEC.md` §§1–2. This is the input to everything else.
2. **Write `SPEC-DMM6500.md` / `SPEC-DMM7510.md` from the reports.** Same
   structure and same standard as `SPEC.md`: verified facts, supported list,
   never-send list, range rule. Delete from
   `docs/keithley-dmm6500.md` / `-7510.md` anything the probe contradicted, and
   keep those documents only as the record of what was guessed and how it fared.
3. **Land the two in-flight `app/` refactors first.** This plan touches
   `app/scpi.py`, `app/specs.py` and `app/instrument.py` — all three are being
   edited by other work right now. Rebase onto them; do not race them.
4. **Extract the `Driver` protocol with exactly one implementation:** the
   34461A, behaviour byte-for-byte unchanged. A pure refactor, verified against
   the existing instrument by the protocol in `IO-DISCIPLINE.md` §"Verification
   protocol". **If this step changes any observable behaviour, it is wrong.**
5. **Make `crash_safe` per-driver and measured.** Small, self-contained, and the
   one change that prevents a Keithley from being handed the `TRIG:COUN INF`
   branch on an untested assumption. Worth doing before any Keithley code
   exists.
6. **Add the DMM6500 driver**, from `SPEC-DMM6500.md` only. Streaming and the
   idle keepalive last, and each verified on the bench in the order
   `IO-DISCIPLINE.md` prescribes: isolated `:TRIGger:CONTinuous RESTart` probe on
   a throwaway session → 30 s idle → 30 s streaming → one capture → clean
   shutdown, with the health canary checked between every step.
7. **Add the DMM7510 driver** as a subclass of the DMM6500's, overriding only
   the differences in `docs/keithley-dmm7510.md` §2. Cheap once step 6 is done.
   Decide explicitly whether the digitize functions are offered at all: at
   1 MS/s the 100,000-reading buffer lasts 0.1 s, which is not a live strip
   chart and should not pretend to be one.
8. **UI: capability-driven controls.** Hide the screen-capture button and the
   resolution band where the driver reports them absent. Do this after a real
   driver exists to report absence, not before.

**Two open questions that step 1 must answer before step 6 can be designed:**

- How do you run a *continuous* acquisition on a Keithley? `TRIG:COUN INF` has
  no equivalent; `"DurationLoop"` and a large `"SimpleLoop"` are both plausible
  and both untested. See `docs/keithley-dmm6500.md` §6.1.
- Does a Keithley's front panel even freeze when a remote session connects? The
  entire premise of `IO-DISCIPLINE.md` rule 1 is a Truevolt behaviour. If it
  does not, the idle keepalive is unnecessary on these models and should not be
  built.
