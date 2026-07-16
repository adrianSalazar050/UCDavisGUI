# Bambu Monitor Dashboard (v1) — Design

**Date:** 2026-07-16
**Status:** Approved by user (conversation, 2026-07-16)

## Purpose

A live, browser-based dashboard for the Bambu A1 mini data-collection rig in
this repo. It shows what `bambu_link.py` sees — printer state, temperatures,
progress/layer, HMS errors — plus the newest captured frame from `runs/`.
It is the first page of a larger GUI (runs browser, registration viewer,
print control will come later), so it ships inside a VERA-style app shell
per `FRONTEND-STACK-GUIDE.md`.

Out of scope for v1: starting/controlling prints, browsing past runs,
registration visualization, any classifier/detector output, dark theme,
auth.

## Decisions already made

| Question | Decision |
|---|---|
| Scope | Live printer dashboard only |
| Data source | Standalone Python backend owning its own `BambuLink` connection; works whether or not `capture.py` is running; has `--mock` mode |
| Camera | Serve the newest frame written by `capture.py` under `runs/`; never open the webcam (avoids device contention on Windows) |
| Theme | Light only ("Slate Daylight" token block from the guide §3.1); dark theme later via token swap |
| Shell | Full VERA-style shell (sidebar + topbar + page registry + ui kit) with a single registered page |
| Stack | Vite 6 + React 19, plain JSX, one `styles.css` with design tokens, hand-rolled ui kit — per `FRONTEND-STACK-GUIDE.md`. No Tailwind, no component frameworks, no Supabase |

## Architecture

Two pieces in this repo:

```
server/                  # Python backend (FastAPI + uvicorn)
  main.py                # app, routes, WebSocket, static serving, CLI args
  printer.py             # PrinterService (wraps BambuLink) + MockPrinter (fake feed)
  runs.py                # active-run discovery, newest-frame lookup
frontend/                # Vite + React
  index.html, vite.config.js, package.json
  src/
    main.jsx             # entry, imports styles.css
    App.jsx              # shell: Layout + active page from registry
    styles.css           # ALL styling: guide §3.1 tokens + ui-* classes
    app/pageRegistry.jsx # { dashboard: { title: "Dashboard", group: "Monitor" } }
    api/printer.js       # fetch wrappers for /api/*
    hooks/usePrinter.js  # WebSocket client + reconnect; returns live state
    components/ui/       # Button, Card, Section, PageFrame, Stack, Columns,
                         # StatTile, StatusPill, NavGroup
    components/dashboard/# CameraCard, TempsCard, HmsCard, PrintInfoCard
    pages/Dashboard.jsx
```

`bambu_link.py`, `capture.py`, `check_registration.py`, `probe_gcode.py` are
not modified.

### Data flow

printer → MQTT (8883) → `BambuLink` (unchanged) → `PrinterService` holds the
merged state → on every report, pushes a curated summary JSON over
WebSocket `/ws` → `usePrinter` hook → React components.

Camera images: `capture.py` writes `runs/<ts>_<name>/frames/layer_NNNN.jpg`;
the server finds the newest frame (see Endpoints) and the frontend polls it.
The server never opens a camera device.

### Curated state payload (pushed on `/ws`, also at `GET /api/status`)

`BambuLink.summary()` (bambu_link.py:191) extended with:

```json
{
  "gcode_state": "RUNNING",
  "layer_num": 42, "total_layer_num": 137,
  "mc_percent": 31, "mc_remaining_time": 58,
  "nozzle_temper": 219.8, "nozzle_target_temper": 220.0,
  "bed_temper": 60.1, "bed_target_temper": 60.0,
  "spd_lvl": 2, "spd_mag": 100,
  "print_error": 0, "fail_reason": null,
  "subtask_name": "Benchy", "gcode_file": "benchy.gcode",
  "hms": ["0300_0100_0001_0007"],
  "connection": "ok",          // "ok" | "stale" | "disconnected"
  "report_age_s": 1.2          // seconds since last MQTT report; null if none yet
}
```

Missing fields are `null` (the printer sends partial updates; before the
first full report some fields are simply unknown).

### Endpoints

| Route | Behavior |
|---|---|
| `WS /ws` | On connect: send current summary immediately. Then push the summary on every MQTT report, throttled to at most ~4 messages/s. Also push every 5 s regardless (keeps `report_age_s` fresh when the printer is quiet). |
| `GET /api/status` | Current summary (same JSON). For debugging/curl; the UI uses the WebSocket. |
| `GET /api/frame/latest` | JPEG bytes of the newest frame of the active run, headers `X-Frame-Layer` and `X-Frame-Run`. 404 with JSON body if no active run or no frames yet. |
| `GET /*` | Serves `frontend/dist` if it exists (SPA fallback to `index.html`); otherwise a plain-text hint to run `npm run build` or the Vite dev server. |

**Active run** = the directory under `--runs-dir` (default `runs/`) whose
`frames/` contains the most recently modified `layer_*.jpg`, provided that
mtime is < 30 min old. Newest frame = highest layer number in that
directory. If nothing qualifies → 404.

### Server CLI

```
python -m server --host <ip> --serial <sn> --access-code <code> \
                 [--runs-dir runs/] [--port 8000]
python -m server --mock [--port 8000]
```

`--mock` runs `MockPrinter` instead of `BambuLink`: an endless loop of fake
prints (RUNNING → layer every ~2 s with wandering temps → FINISH → 10 s IDLE
→ repeat), injecting one HMS code for a few layers mid-print. It also writes
mock frames (reusing the drawing logic pattern of `capture.py`'s
`MockCamera`) into a real run directory, so the frame-serving path is
exercised too. In mock mode `--runs-dir` defaults to `runs-mock/` instead
of `runs/`, so fake data never pollutes real captures.

### Reconnection / failure behavior

- **MQTT drops:** `PrinterService` keeps the last-known state, marks
  `connection: "disconnected"`, and retries `connect()` every 10 s in a
  background thread. Initial connect failure at startup does not kill the
  server — it starts in `disconnected` and keeps retrying.
- **Stale:** connected but no report for > 15 s → `connection: "stale"`.
- **WebSocket drops (frontend):** `usePrinter` reconnects with exponential
  backoff capped at 10 s. While down, the connection pill shows danger and
  the last data stays on screen, visually dimmed.
- **No frame available:** CameraCard shows a placeholder ("No active capture
  run — start capture.py"), never a broken image. Frontend polls
  `/api/frame/latest?t=<now>` every 2 s and swaps only on HTTP 200.

## Dashboard page

VERA shell: CSS grid `260px 1fr`; near-black sidebar (`#111821`) with the
app name and one `NavGroup` ("Monitor" → Dashboard); light main panel with a
topbar.

- **Topbar:** page title + connection `StatusPill`
  (ok = receiving reports · warn = stale · danger = disconnected) + printer
  host (or "MOCK").
- **StatTile row (6 tiles):** State · Layer `n / total` · Progress % ·
  Time remaining (min) · Nozzle °C `cur/target` · Bed °C `cur/target`.
- **Two columns below** (`Columns`, camera side wider):
  - **CameraCard:** newest frame, caption "Layer N — <run name>";
    placeholder when 404.
  - **PrintInfoCard:** gcode file, subtask name, speed level/magnitude,
    and — only when set — `print_error` / `fail_reason` rows.
  - **HmsCard:** each decoded HMS code as a danger `StatusPill` with the
    code text, linking to the Bambu HMS lookup page referenced in
    `bambu_link.py` (`wiki.bambulab.com/en/x1/troubleshooting/how-to-enter-hms-code`);
    or a muted "No errors" line.

Design rules from the guide apply: status colors only inside `StatusPill`,
`--primary` is the only accent, controls/radii/spacing from the token block,
Inter with system fallback, all styling in `styles.css` via `ui-*` classes.

## Dev & prod workflow

- **Dev:** `python -m server --mock` (port 8000) + `npm run dev` in
  `frontend/` (port 5173; `vite.config.js` proxies `/api` and `/ws` → 8000).
- **Prod/normal use:** `npm run build` once; then a single
  `python -m server ...` serves everything at `http://localhost:8000`.

## Testing

- `pytest` under `server/tests/` for pure logic, no network:
  - summary curation (partial state → payload shape, nulls, connection flag)
  - active-run discovery + newest-frame lookup (tmp dirs with fake frames;
    staleness cutoff; empty/missing dirs)
  - mock feed produces the documented payload shape and state transitions
- Frontend: verified end-to-end against `--mock` (no JS test framework in
  v1, matching the guide's minimal dependency footprint).

## New dependencies

- Python: `fastapi`, `uvicorn[standard]` (adds websocket support)
- npm: `react`, `react-dom`, `vite`, `@vitejs/plugin-react`

## Exit criterion for v1

With `--mock`: dashboard shows a full fake print lifecycle (IDLE → RUNNING
with ticking layers/temps/frames → HMS appearing and clearing → FINISH)
and survives killing/restarting the server (reconnects, pills correct).
With real hardware: same, during one actual print alongside `capture.py`.
