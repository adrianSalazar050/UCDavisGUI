# Reconnect, ROI editor, and printer-model check — design

> **STATUS: SHIPPED (2026-07-21).** Written in the future tense before implementation; all three features now exist.
>
> Where to find them: `registry.reconnect()` + `POST /api/printers/{serial}/reconnect`; `frontend/src/components/detection/{RoiEditor.jsx,roiGeometry.js}`; `store.model_id`/`MODEL_NAMES`/`guess_model_id`/`model_mismatch`.
>
> Two details it got wrong in advance: vitest was added (it exists now, over `roiGeometry.js`), and the test count it quotes is stale — run `python -m pytest -q`.
>
> Historical record, not maintained. **`master.md` is authoritative wherever this file disagrees with it.**
>
> Task checkboxes below were never ticked during execution; read the status line above, not the boxes.

Date: 2026-07-21

Three independent features, agreed in brainstorming. Build order **1 → 3 → 2**:
reconnect is smallest, the model check is backend-heavy and fully testable, the
ROI editor is the one that can only be verified by eye.

---

## 1. Reconnect button (per printer, Overview)

### Why

`PrinterService._connect_loop` already retries every `RETRY_S` (10 s) until it
connects, and once paho has connected once it handles reconnects itself. So
this is **not** about adding retry. It is about two things the automatic path
never does:

* force an immediate attempt instead of waiting out the 10 s window (after
  enabling Developer Mode, or powering the printer on);
* tear down and rebuild a wedged client.

It does **not** help when the IP has changed — that needs the Edit form, which
already rebuilds the service on a host change.

### Design

`PrinterRegistry.reconnect(serial) -> dict | None` reuses exactly what
`update()` does on a host change: stop the old service, rebuild via
`service_factory`, start it, return the new summary. `None` on unknown serial.

**Must not hold `_lock` across `stop()`/`start()`** — the registry's existing
invariant, and the reason `summaries()` stays responsive for every WS tick.
Follow `update()`'s locking shape rather than inventing a new one.

Route: `POST /api/printers/{serial}/reconnect` → 200 with the summary, 404 on
unknown serial. No body.

Frontend: a `Reconnect` button on `PrinterCard`, busy while in flight; the WS
push delivers the new state. When `summary.last_error` is set, show it beside
the button so "Unreachable — check the IP" appears where you would act on it.

### Safety

MQTT here is telemetry only. Rebuilding the connection cannot disturb a running
print — it sends no command and does not touch the job.

### Tests

* `reconnect()` stops the old service and starts a new one (fake factory).
* `reconnect()` on an unknown serial returns `None`.
* Route returns 200 + summary; 404 on unknown.
* The registry lock is not held across the rebuild (assert via a factory that
  calls `summaries()` re-entrantly, mirroring the existing update test).

---

## 2. ROI drag editor (Detection page)

### Why

The ROI is currently four `%` text fields, and the only visual feedback is a
rectangle **burned into the JPEG by `detect.py`**. That box cannot change until
the config saves, the supervisor respawns the detector, and a new frame
arrives — 5–10 s later. So editing the region is blind.

This matters more than convenience: master.md §4.1 records that a wrong ROI
crops the bed out of frame entirely and is a *silent* failure.

### Design

New `frontend/src/components/detection/RoiEditor.jsx`:

* the live detection frame as an `<img>` inside a `position: relative` wrapper;
* an overlay rectangle plus 8 handles (4 corners, 4 edges);
* state held in **fractions**, positioned with CSS `%`, so there is no pixel
  math and it survives any display size or aspect-preserving resize.

Pure geometry lives in `frontend/src/components/detection/roiGeometry.js` —
no DOM, no React:

* `clampRoi(roi)` — into `[0,1]`, enforcing `MIN_SIZE` (0.05) on w/h;
* `applyHandleDrag(roi, handle, dx, dy)` — handle id + fractional delta → new
  roi, clamped;
* `roiToPct` / `pctToRoi` — the existing string⇄fraction conversions, moved.

**Two boxes on purpose.** The burned-in outline stays and is styled distinctly:

| Box | Means |
|---|---|
| burned-in (from `detect.py`) | what the detector is using **right now** |
| draggable (browser overlay) | what you are about to apply |

They converge on Apply. This turns a confusing artifact into a before/after.

The four numeric fields stay, bound to the same state, so an edge can still be
nudged by exactly 2%. Plus `Reset to A1 default`.

**No backend change.** The ROI already persists via `update_detection` and
already reaches `detect.py` as `--roi`.

### Tests

This repo has **no frontend test runner** — all 316 tests are Python. Add
`vitest` covering `roiGeometry.js` only (pure functions, no DOM):

* clamping keeps the box inside the frame;
* a corner drag past the opposite edge is clamped to `MIN_SIZE`, not inverted;
* each of the 8 handles moves the edges it should and no others.

The React component itself is verified by build + eye. Stated rather than
hidden.

---

## 3. Printer-model check

### Why

A `.gcode.3mf` sliced for one model can be uploaded to another. Printing an A1
file on an A1 mini can drive the head outside a smaller bed.

### What is actually available

* The 3mf **does** carry the model: `Metadata/slice_info.config` has
  `<metadata key="printer_model_id" value="N2S"/>`, in the file
  `parse_slice_info` already opens.
* The printer **does not** report its model. All 64 keys it publishes over MQTT
  were dumped on 2026-07-21; none identifies the model. So the printer side
  must be configured, not discovered.

### Design

**`threemf.parse_slice_info`** additionally returns `printer_model_id`
(`str | None`). Same contract: never raises; `None` when the key is absent or
the file is corrupt. Taken from the first `<plate>`.

**`store.PrinterConfig`** gains `model_id: str = ""`, validated in `from_dict`
like every other field. Two module-level tables:

```
MODEL_NAMES     = {"N2S": "A1", "N1": "A1 mini", ...}   # id -> friendly
SERIAL_PREFIXES = {"039": "N2S", "030": "N1", ...}      # prefix -> id
```

`guess_model_id(serial) -> str` returns `""` when the prefix is unknown.

> **Verification honesty.** `N2S` = A1 is confirmed (this repo's own
> `testing.gcode.3mf`, sliced for the A1 in use). `N1` = A1 mini and the P1/X1
> entries are community knowledge, **not** confirmed here. The table carries a
> comment saying exactly which entries are verified, consistent with how
> master.md §1.1 treats hardware claims.

**Enforcement:**

| Step | Behaviour |
|---|---|
| `POST .../files` | 201 + `warning` naming both models |
| `POST .../queue` | job stores `model_id`; the row shows a ⚠ badge |
| `POST .../queue/{id}/start` | **409**, job stays queued |

**Unknown never blocks.** If the file has no `printer_model_id` (every raw
`.gcode`), or the printer's `model_id` is unset, the check is skipped silently.
A wrong guess must never cost a print; it may only ever refuse a *confirmed*
mismatch. This is the single most important rule in the feature.

Frontend: model dropdown on Add/Edit printer (prefilled from the serial guess),
⚠ badge on the queue row, warning text after upload.

### Tests

* `parse_slice_info` returns the model id; `None` when absent/garbage.
* `guess_model_id` for known prefixes, `""` for unknown.
* `PrinterConfig.from_dict` validates/defaults `model_id`.
* Upload warns on mismatch, stays silent when either side is unknown.
* Queue job carries `model_id`.
* Start returns 409 on a confirmed mismatch and the job stays queued.
* Start succeeds when either side is unknown (both directions).

---

## Out of scope

* Re-verifying `file:///sdcard/` on the A1 (master.md §5.3) — separate.
* Re-collecting detection training data for the A1 (§11) — separate.
* Any change to the auto-stop state machine.
