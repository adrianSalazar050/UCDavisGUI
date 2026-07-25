# End-of-print "plate fully forward" for the A1 family — design

> **STATUS: SHIPPED (2026-07-23, commit `88a9e1c`).** Implemented as designed
> below. Verified by slicing a real cube: the produced gcode matches §3's
> block exactly, `M17` re-enabling the steppers before the move. **Not yet
> verified on hardware** — the gcode is confirmed, but whether the plate
> physically ends up where an automated lifter expects still needs an actual
> print.
>
> Historical record, not maintained. **`master.md` §6.8 is authoritative
> wherever this file disagrees with it.**

Date: 2026-07-23

---

## 1. The goal

Every `.gcode.3mf` sliced through the dashboard for an **A1** or **A1 mini**
should finish with the build plate slung **fully forward**, so an automated
plate lifter (or a human) can reach it without fighting the gantry. This is a
common print-farm modification.

Always on for those two models — no toggle, no new config field. It is a
property of the machine, not of a job.

## 2. What the stock gcode already does, measured 2026-07-23

| Model | `printable_area` max Y | Stock end-gcode park | Changes? |
|---|---|---|---|
| A1 | **256** | `G1 X-48 Y180 F3600` | **yes** — 180 of 256 is ~70% forward |
| A1 mini | **180** | `G1 X-13 Y180 F3600` | no — 180 *is* its max, already fully forward |

So in practice only the A1 moves further. The mini still gets the block; it
simply lands where it already was. Deriving the value (rather than special-casing
the A1) keeps the two models on one code path.

**The trap:** the stock end gcode's *last* two lines are

```gcode
M400
M18 X Y Z      ; disable steppers
```

A naive append therefore runs **after the motors are disabled** and does
nothing. Whatever we add must re-enable the steppers first.

## 3. The approach: append a self-contained block

Appended to the machine profile's `machine_end_gcode` before slicing:

```gcode
; --- move plate fully forward for removal ---
M17              ; re-enable steppers (the stock end gcode disabled them)
G90              ; absolute positioning
G1 Y<max> F3600  ; Y only -- leave the toolhead parked where stock left it
M400
M18 X Y Z        ; disable again
```

Only **Y** moves. The stock gcode parks the toolhead off to the side (X-48 on
the A1, X-13 on the mini); moving X would drag it back over the plate.
`F3600` matches the stock park move's feedrate.

### Alternatives rejected

- **String-replace the stock park line** (`Y180` → `Y256`). Cleaner output, but
  it depends on matching an exact vendor line that already differs per model and
  could change in any Bambu Studio update — and a failed match would *silently*
  do nothing. That is precisely the failure mode that caused the `include` bug
  (`master.md` §11).
- **Post-process `plate_1.gcode` inside the zip.** Independent of profiles, but
  it would invalidate `Metadata/plate_1.gcode.md5`, the checksum the printer
  verifies. Patching the *config* instead means the slicer generates the gcode
  and computes that checksum over our block for free.

## 4. Design

**`bed_forward_gcode(machine) -> str | None`** — a new pure function in
`server/slicer.py`. Dict in, string (or None) out, so it tests with no slicer.

- Gates on `machine["printer_model"]` being exactly `"Bambu Lab A1"` or
  `"Bambu Lab A1 mini"`. P1/X1 are CoreXY — their bed only moves in Z, so a Y
  move is meaningless and they must get nothing.
- Derives max Y by parsing `printable_area` (`['0x0','256x0','256x256','0x256']`
  → `256`).
- Returns `None` on an unknown model, a missing/malformed `printable_area`, or a
  non-positive max — degrading to "no change", never to a bad move.

**`run_slice`** applies it alongside the existing `enable_support` /
`support_type` / `curr_bed_type` patching, on a **copy** of the machine dict —
preserving the existing "never mutate the caller's cached profile" guarantee
(there is already a test for that on the process dict; this adds the same for
machine).

## 5. Testing

Unit (no slicer, no printer):

- A1 → block containing `G1 Y256`; A1 mini → `G1 Y180`.
- The block re-enables steppers (`M17`) before moving — the whole point.
- P1/X1/unknown model → `None`.
- Missing, malformed, or non-positive `printable_area` → `None`.
- `run_slice` appends it for an A1 machine and not for a P1, and does not mutate
  the caller's machine dict.

Integration, runnable offline on any dev box with Bambu Studio: slice a cube for
the A1 and assert the produced `plate_1.gcode` contains the move after the stock
park.

**Not verified:** that the plate physically ends up where the automated lifter
expects on real hardware. The gcode is verified; the mechanical outcome needs a
print. Per `master.md` §1.1's discipline that stays unverified until someone runs
it.
