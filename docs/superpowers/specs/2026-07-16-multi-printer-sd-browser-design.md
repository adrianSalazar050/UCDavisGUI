# Multi-Printer Connection Manager + SD Card Browser (v2) — Design

**Date:** 2026-07-16
**Status:** Approved by user (conversation, 2026-07-16)
**Supersedes parts of:** `2026-07-16-bambu-dashboard-design.md` (v1). v1's single
printer, its `--host/--serial/--access-code` CLI flags, and its bare-summary
WebSocket payload are all replaced here. Everything else in v1 stands.

## Purpose

Turn the v1 single-printer dashboard into something you can point at printers
*at runtime*: type an IP, serial, and LAN access code in the browser and get a
connection. Show how many printers are connected and what each is doing. Browse
the filenames on each printer's microSD card.

v1 is single-printer by construction — `create_app(service, runs_dir, dist)`
closes over exactly one service and `--host/--serial/--access-code` are required
at startup. This is a reshaping of that, not a bolt-on.

## Decisions made

| Question | Decision | Why |
|---|---|---|
| Scale | 3–4 printers max; usually one | User's actual bench. No fleet machinery (no pagination, no filtering, no per-printer sockets). |
| Persistence | IP + serial + access code to `printers.json`, gitignored | Type once. Plaintext secret on a trusted LAN is the same trust model `bambu_link.py` already takes by disabling TLS verification. |
| CLI flags | `--host/--serial/--access-code` **retired**; `--mock` stays | The GUI is now the way printers get configured. |
| Camera | One designated "capture printer" | Physical reality: one webcam, one rig. `capture.py` stays unmodified. |
| SD scope | **Read-only listing** | Exactly what was asked. Nothing on the card can be harmed by a bug or a stray click. |
| Shell layout | Overview grid + drill-in (option C) | Answers "how many are connected?" literally, on a landing page. |
| Backend model | One `PrinterRegistry`, one multiplexed WebSocket | At 3–4 printers the payload is a rounding error and there's one socket to reconnect. |
| SD transport | FTPS, on-demand HTTP, never polled | MQTT cannot list files at all (see below). |

### Why SD listing needs a second connection

The Bambu MQTT protocol exposes **no file listing**. The only way to read the
microSD card is the printer's **FTPS server on port 990, implicit TLS**, with the
same `bblp` + access-code credentials as MQTT
([OpenBambuAPI/ftp.md](https://github.com/Doridian/OpenBambuAPI/blob/main/ftp.md)).
So each printer has two independent connections: MQTT for state, FTPS for files.
They fail independently and must be allowed to.

## Architecture

```
server/
  store.py      # NEW — printers.json read/write. Pure file I/O.
  registry.py   # NEW — owns {serial: service}, add/remove/lifecycle.
  sdcard.py     # NEW — FTPS implicit-TLS listing. Isolated so it fails alone.
  printer.py    # PrinterService gains identity + list_files(); MockPrinter matches
  main.py       # create_app(registry, ...) instead of create_app(service, ...)
  __main__.py   # printer CLI flags retired; --mock seeds 3 fake printers
  runs.py       # UNCHANGED
frontend/src/
  pages/Overview.jsx        # NEW — printer grid + add form (landing page)
  pages/SdFiles.jsx         # NEW — SD listing for the selected printer
  pages/Dashboard.jsx       # now renders the *selected* printer
  components/printers/      # NEW — PrinterCard, AddPrinterForm
  components/sd/            # NEW — FileTable
  components/ui/Field.jsx   # NEW primitive — label + input + error
  hooks/usePrinters.js      # replaces usePrinter — returns { printers, wsUp }
  app/pageRegistry.jsx      # Overview, Dashboard, SD Files
  App.jsx                   # holds selectedSerial
  api/printer.js            # + addPrinter, removePrinter, fetchFiles
  styles.css                # + printer grid, file table, form field classes
```

`bambu_link.py`, `capture.py`, `check_registration.py`, `probe_gcode.py`,
`server/runs.py` are **not modified**.

`store.py` and `registry.py` are split deliberately: the store is dumb file I/O
testable against `tmp_path`; the registry is thread and connection lifecycle.
Merged, the interesting logic could not be tested without touching disk.

`PrinterService` barely changes — it is already per-printer. It stops being a
singleton, gains `serial` / `name` / `capture`, and grows one method:
`list_files(path)`, delegating to `sdcard.py`. `MockPrinter` implements the same
method over a synthetic tree, so **the SD page works end-to-end under `--mock`**.

### Data flow

Per printer, unchanged from v1: printer → MQTT (8883) → `BambuLink` (deep-merges
partial reports) → `PrinterService` holds the merged state + report timestamp.

The registry fans that out: each WebSocket tick calls `registry.summaries()`,
which is N cheap dict reads. This must stay non-blocking — `summary()` runs on the
event loop, and a stall there freezes every connected client
(`server/main.py:64`).

FTPS is deliberately **off** that path: request-scoped, on FastAPI's threadpool,
triggered only by opening the SD page or pressing Refresh. Never polled — no
hammering a printer's FTP server mid-print for a page opened occasionally. A dead
FTP server yields a broken SD page and a perfectly healthy dashboard.

### Payload

`WS /ws` and `GET /api/printers` share one envelope:

```json
{
  "printers": [
    {
      "serial": "0300CA633005010",
      "name": "A1-bench",
      "printer": "192.168.137.2",
      "capture": true,
      "connection": "ok",
      "report_age_s": 1.2,
      "last_error": null,
      "gcode_state": "RUNNING",
      "layer_num": 42, "total_layer_num": 137,
      "mc_percent": 31, "mc_remaining_time": 58,
      "nozzle_temper": 219.8, "nozzle_target_temper": 220.0,
      "bed_temper": 60.1, "bed_target_temper": 60.0,
      "spd_lvl": 2, "spd_mag": 100,
      "print_error": 0, "fail_reason": null,
      "subtask_name": "Benchy", "gcode_file": "benchy.gcode",
      "hms": ["0300_0100_0001_0007"]
    }
  ]
}
```

All v1 summary fields are retained. New: `serial`, `name`, `capture`,
`last_error`. `printer` (the host) already existed in v1. Fields the printer has
not reported yet stay `null` — it sends partial updates.

`name` defaults to `host` when not supplied. The list is always ordered by
registration order, never by status — a grid that reshuffles itself as printers
change state is unusable.

### Endpoints

| Route | Behavior |
|---|---|
| `GET /api/printers` | `{"printers": [...]}` — the envelope above |
| `POST /api/printers` | Body `{host, serial, access_code, name?, capture?}`. Starts the connection, persists, returns 201 with the new printer's summary. 409 if the serial is already registered. 400 if any of host/serial/access_code is empty. |
| `DELETE /api/printers/{serial}` | Stop the connection, then drop from `printers.json`. 204. 404 if unknown. |
| `GET /api/printers/{serial}/files?path=/` | Sync route → threadpool → FTPS. `{"path": "/", "entries": [{name, is_dir, size, mtime}]}`. 404 unknown serial, 400 bad path, 502 FTPS failure with a readable message. `size` is bytes (`null` for directories); `mtime` is an ISO-8601 string, or `null` when the server does not report one. |
| `GET /api/frame/latest` | **Unchanged from v1** — global; one webcam films the one capture printer. |
| `WS /ws` | Pushes the envelope. Same throttle as v1: sampled at 4 Hz, pushed on change, plus a 5 s heartbeat. |
| `GET /*` | **Unchanged from v1** — serves `frontend/dist`, else a build hint. |

`GET /api/status` (v1) is removed; `GET /api/printers` replaces it.

Setting `capture: true` on a printer clears it on whichever printer previously
held it — it is a single-occupancy flag, since there is one webcam.

### Persistence and secrets

`printers.json` at the repo root (override with `--printers-file`):

```json
[{"serial": "0300CA633005010", "host": "192.168.137.2",
  "access_code": "test-access-code", "name": "A1-bench", "capture": true}]
```

Written atomically (temp file + replace) so a crash mid-write cannot leave a
truncated file that bricks the next startup. A corrupt or unreadable file logs a
warning and starts empty rather than crashing.

**The access code is never echoed back.** It enters via `POST`, lands in
`printers.json`, and is used for MQTT and FTPS. No `GET`, no WebSocket payload,
and no error message ever contains it. `printers.json` is added to `.gitignore`
in the same change that introduces it, so a plaintext password cannot be
committed by accident. This is asserted by a test, not left to reviewer vigilance.

### Server CLI

```
python -m server [--printers-file printers.json] [--runs-dir runs/] [--port 8000]
python -m server --mock [--port 8000]
```

`--host`, `--serial`, and `--access-code` are **removed** — printers are added in
the browser and restored from `printers.json`. With no printers registered the
server starts fine and the UI shows the add form; the printer list is no longer a
startup requirement. `--runs-dir` and `--port` keep their v1 meanings, including
`--runs-dir` defaulting to `runs-mock/` under `--mock`.

`README (1).md` and `CONNECTION.md` both document the retired flags and must be
updated in the same change; `CONNECTION.md`'s connection values become an example
of what to type into the add form.

### Error handling

**Wrong access code** is the most common failure in the field
(`CONNECTION.md` troubleshooting). `PrinterService._connect_loop`
(`server/printer.py:105-123`) already distinguishes the two failure modes and
discards the distinction into a log line. It gets captured into `last_error`:

| Condition | `last_error` shown in the UI |
|---|---|
| `link.connect()` raises | "Unreachable — check the IP, and that LAN-only Mode is on" |
| `connect()` returns `False` (TLS fine, no CONNACK in 5 s) | "No response — the access code may be wrong (it rotates on firmware updates), or Developer Mode is off" |
| Connected, reports flowing | `null` |

This requires **no change to `bambu_link.py`**.

Other cases:

- **Duplicate serial** → 409, form shows "that serial is already registered".
- **FTPS failure** → 502; the SD page shows the message and a Retry; the
  dashboard is unaffected.
- **Path traversal** — `?path=` is user input handed to an FTP server. Normalize
  and reject any path escaping `/`. Read-only is not a reason to let a path
  escape.
- **Deleting a printer** stops its thread before writing the store, so a
  half-removed printer cannot keep reconnecting.
- **Selected printer removed** → selection falls back to the first remaining
  printer, or none.
- **WebSocket drop** → v1 behavior retained (exponential backoff to 10 s; last
  data stays, dimmed).

## Pages

Shell is v1's: `260px 1fr` grid, near-black sidebar, light main panel, topbar.
Nav group "Monitor" → Overview · Dashboard · SD Files. Topbar shows the count
("3 printers · 2 online").

**Overview (landing page).** One `PrinterCard` per printer in a responsive grid:
name, host, a `StatusPill` (RUNNING / Stale / Offline), layer + progress, and
`last_error` when set. Clicking a card selects that printer and goes to its
Dashboard. Each card has a Remove action with a confirm. Below the grid, the
`AddPrinterForm`: IP · Serial · Access code · optional Name · "this printer has
the camera" checkbox. With no printers registered, the page is just the form.

**Dashboard.** v1's page, for the selected printer. `CameraCard` renders only
when that printer has `capture: true`; otherwise a muted "No camera on this
printer". Empty state when nothing is selected.

**SD Files.** Breadcrumb path, Refresh button, `FileTable` (name, size, modified;
folders first, folders clickable). Errors render in-page with a Retry. Loading
state matters here — an FTPS handshake is not instant.

**When exactly one printer is registered it is auto-selected**, so the common
case never involves picking anything.

Validation is deliberately lenient: host/serial/access-code must be non-empty and
whitespace-stripped, nothing more. The access code is "usually 8 characters" as a
UI hint, not a server-enforced rule — validation that blocks a legitimate
connection is worse than a failed connect that reports itself via `last_error`.

Design rules from `FRONTEND-STACK-GUIDE.md` continue to apply: status colors only
inside `StatusPill`, `--primary` as the only accent, all styling in `styles.css`
via `ui-*` classes.

## Mock mode

`--mock` seeds **three** printers into the registry — one RUNNING (and flagged as
the capture printer, writing real frames), one stale, one offline — so the
Overview grid, all three status states, and the SD page are exercisable with zero
hardware. Mock mode uses an in-memory store and never touches `printers.json`.

`POST /api/printers` still works under `--mock` and creates a *real*
`PrinterService`, which surfaces "Unreachable" against a nonexistent host — this
is how the add-printer error path gets exercised without hardware.

## Testing

`pytest` under `server/tests/`, pure logic, no network:

- `test_store.py` — round-trip; missing file → empty; corrupt JSON → empty +
  warning, no crash; atomic write leaves no partial file.
- `test_registry.py` — add / remove / duplicate-serial; `summaries()` shape;
  `capture` single-occupancy; **an explicit assertion that `access_code` appears
  in no summary payload**.
- `test_sdcard.py` — FTP listing lines → entries as a pure parse function; the
  path-traversal guard. The socket code is never unit-tested.
- `test_api.py` — reworked against a `FakeRegistry`; new routes; 409/404/502
  paths.
- `test_summary.py` — extended for identity fields and `last_error`.
- `test_runs.py` — unchanged.

Frontend verified end-to-end against `--mock`, matching v1's posture (no JS test
framework).

## New dependencies

**None.** `ftplib` and `ssl` are stdlib. FTPS needs an `ftplib.FTP_TLS` subclass
that wraps the socket at connect time rather than after `AUTH TLS` (implicit vs
explicit TLS) — a code detail, not a dependency.

## Out of scope

Downloading, uploading, or deleting SD files. Starting or controlling prints.
Multiple cameras. Auth on the dashboard itself. Dark theme. Runs browser.
Anything at fleet scale (pagination, filtering, per-printer sockets).

## Exit criterion

**Verifiable now, under `--mock`:** Overview shows 3 printers with correct
distinct states and a live count; selecting one drives the Dashboard; the SD page
lists a synthetic tree and navigates folders; adding a bogus printer surfaces
"Unreachable"; removing a printer stops it and updates the store; killing and
restarting the server restores the registered printers from `printers.json`;
`pytest server/tests` passes.

**Deferred to hardware** (the printer is offline as of writing — all ports time
out): real FTPS against a real microSD, and two real printers connected at once.
Two known FTPS risks to confirm there: implicit TLS must be established at
connect time, and some Bambu firmware requires TLS session reuse on the data
channel. Neither is verifiable without the printer, and neither blocks the rest
of the work.
