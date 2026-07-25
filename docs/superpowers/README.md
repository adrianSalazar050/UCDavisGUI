# docs/superpowers — design specs and implementation plans

**Everything in this directory is a historical record.** Each file captures
what was believed and intended at the moment it was written, and none of them
is maintained afterwards.

**[`master.md`](../../master.md) is authoritative wherever these disagree with
it.** They are not corrected in place, because rewriting a plan after the fact
to look right destroys the only thing it is still good for: showing what was
actually known at the time, including the parts that turned out wrong.

Every file carries a status banner directly under its title.

## What's here

| Status | File | Notes |
|---|---|---|
| ⛔ **abandoned** | `specs/2026-07-16-basic-gui-pyqt6-design.md` | PyQt6 desktop GUI. Dropped in favour of the React dashboard. Only a package scaffold was ever written, and it lives on the abandoned `basic-gui` branch along with its 1920-line plan. Do not implement from it |
| ⚠ superseded | `specs/2026-07-16-bambu-dashboard-design.md` | v1, single printer + CLI flags. Replaced by the multi-printer spec |
| ⚠ superseded | `plans/2026-07-16-bambu-dashboard.md` | the v1 plan |
| ✅ shipped | `specs/2026-07-16-multi-printer-sd-browser-design.md` | runtime printer manager + FTPS SD browser |
| ✅ shipped | `plans/2026-07-16-multi-printer-sd-browser.md` | says the SD card is read-only; it is not, since upload shipped |
| ✅ shipped | `specs/2026-07-17-failure-detection-autostop-queue-design.md` | detection + auto-stop + queue. Has **no ROI concept**, which turned out to matter a lot |
| ✅ shipped | `plans/2026-07-17-failure-detection-autostop-backend.md` | |
| ✅ shipped | `plans/2026-07-17-a1-camera-source-backend.md` | titled "A1 Mini"; the same code now runs on an A1 |
| ✅ shipped | `plans/2026-07-17-detection-frontend.md` | says no JS test runner exists; vitest was added later |
| ✅ shipped | `plans/2026-07-17-print-queue.md` | queue schema predates `model_id` |
| ✅ shipped | `specs/2026-07-21-reconnect-roi-editor-model-check-design.md` | reconnect, ROI drag editor, printer-model check |
| ✅ shipped | `specs/2026-07-22-auto-slicing-design.md` | automatic slicing: STL → sliced, uploaded, queued |
| ✅ shipped | `plans/2026-07-22-auto-slicing.md` | Tasks 1–11 shipped 2026-07-22; Task 12 (hardware gate) executed and **PASSED** 2026-07-23 — a full clean 100-layer print |
| ✅ shipped | `specs/2026-07-22-electron-desktop-packaging-design.md` | the `desktop/` Electron installer. Windows verified; **the Linux AppImage has never been built or run** (no Docker on the dev box) |
| ✅ shipped | `specs/2026-07-23-bed-forward-eject-design.md` | park the plate fully forward at end of print (A1 family only). Gcode confirmed; the *mechanical* outcome is still unverified on hardware |
| ✅ shipped | `specs/2026-07-23-lan-serving-auth-design.md` | shared-password auth + `--host`, and the fail-closed rule. Plain HTTP, not TLS — that trade is recorded in the file |
| ✅ shipped | `specs/2026-07-23-onnx-detector-backend-design.md` | the torch-free ONNX inference backend. A **backend swap, not a model change** — it changes none of `master.md` §12's conclusions |
| 📐 partly built | `specs/2026-07-24-erp-traceability-design.md` | ERP traceability: local ledger, parts, inventory, Supabase sync, arm ingest. **Phases 1–3 are now shipped** (`master.md` §13–§15); Phases 4–5 are still design only |
| ✅ shipped | `plans/2026-07-24-erp-traceability-phase1-ledger.md` | Phase 1: the ledger + run recording. Verified on the real A1 (a `cube` print recorded start → FINISH) |
| ✅ shipped | `plans/2026-07-24-erp-traceability-phase2-parts.md` | Phase 2: parts catalogue + recipes + slice-from-part. Not yet verified on hardware end to end |
| ✅ shipped | `plans/2026-07-25-erp-traceability-phase3-spools.md` | Phase 3: filament spools + consumption. Not yet verified on hardware end to end |
| 📐 partly built | `specs/2026-07-25-slicing-preview-and-all-models-design.md` | two independent slicing improvements. **Both shipped**: Feature A (P1P/P1S presets) and Feature B (STL preview + reorient) |
| ✅ shipped | `plans/2026-07-25-stl-preview-reorient.md` | Feature B of the above: three.js preview, rotate, auto-drop, client-side bake |

## Things that are wrong in most of these files

Rather than repeat these in every banner:

- **Test counts.** The tree quotes 194, 228, 316 and ~100 at various points.
  All obsolete. Run `python -m pytest -q`.
- **The printer.** An A1 mini (`0300CA633005010`, `192.168.137.2`) until
  2026-07-19; an **A1** (`03919D531805572`) since 2026-07-21. The camera
  geometry differs enough that the detection ROI is close to inverted between
  them — see `master.md` §1.1.
- **Task checkboxes.** Never ticked during execution. The status banner is the
  truth, not the boxes.
- **Duplication.** The three largest plans inline whole implementations that
  `master.md` §3–§7 now documents properly. Read master.md first.
