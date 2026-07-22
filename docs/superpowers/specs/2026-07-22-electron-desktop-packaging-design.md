# Electron desktop packaging — design

**Date:** 2026-07-22
**Status:** approved, pre-implementation
**Scope owner:** adrianSalazar050

## 1. Goal

Ship `bambu-monitor` as a **downloadable desktop application** that a
non-technical user installs and runs with no Python, Node, or manual setup.
The app must run on **Windows** (`.exe` installer) and **Linux Mint**
(`.AppImage`).

### In scope (the features to bundle)
- The full **dashboard**: printer registry (add/edit/remove/reconnect),
  live MQTT status/temps/layer/HMS over `/api/printers` + `/ws`, the print
  **queue** (plan / reorder / start), and the **SD-card browser + `.gcode.3mf`
  upload** — all features verified on hardware.

### Explicitly out of scope (bundle-time)
- **YOLO failure detection / auto-stop** — requires `torch` + `ultralytics`
  (~1 GB) and, per project memory, the model is currently "effectively blind"
  on the A1 camera. Excluded from the bundle. The launcher passes
  `detection=None`, so `detect.py` is never spawned.
- **Live camera view** — that view is produced by `detect.py`; excluded with
  detection.
- **Auto-slicing** — shells out to a separate `bambu-studio.exe` that cannot be
  legally bundled. Left **auto-detected**: enabled only if the user already has
  Bambu Studio installed, disabled (gracefully) otherwise. Nothing extra is
  shipped for it.
- **Code signing / notarization** — no certificates. See §7 caveats.

## 2. Architecture

Electron is a thin desktop shell. On launch it spawns the existing FastAPI
backend — frozen into a standalone binary — and opens a Chromium window at
`http://127.0.0.1:8000`, which the backend already serves the built React app
from. The frontend calls the API with **relative paths** (`/api/...`) and
derives the WebSocket URL from `window.location.host`, so everything resolves
against the Electron origin with no hardcoded ports.

```
Bambu Monitor.exe / Bambu Monitor.AppImage
 └─ Electron main process (desktop/main.js)
      ├─ spawn → bambu-backend (PyInstaller onedir)   FastAPI+uvicorn on 127.0.0.1:8000
      │            serves /api, /ws, and frontend/dist/ (React build)
      ├─ poll 127.0.0.1:8000 until it answers (timeout → error dialog)
      ├─ BrowserWindow.loadURL('http://127.0.0.1:8000')
      └─ before-quit / window-all-closed → terminate backend child
```

Three artifacts are produced from source:

1. **React frontend** → `frontend/dist` via `npm ci && npm run build`.
2. **Python backend** → standalone binary via **PyInstaller** (onedir).
3. **Electron shell** → wrapped by **electron-builder** into `.exe` (NSIS,
   Windows) and `.AppImage` (Linux).

## 3. Component 1 — Backend freeze

### 3.1 Launcher (`desktop/launcher.py`)
A purpose-built entry point, used **instead of** `server/__main__.py` (whose
`frontend/dist` and `weights` paths assume a source checkout and whose wiring
builds a `DetectorSupervisor` we don't want).

Responsibilities:
- `resource_path(rel)` — resolves bundled data relative to `sys._MEIPASS` when
  frozen, and to the repo root when run from source (so the launcher is
  testable without freezing).
- Locate `frontend/dist` inside the bundle via `resource_path`.
- Resolve a **writable per-user data directory** for `printers.json`,
  `queues.json`, and `runs/`. Order: `--data-dir` arg / `BAMBU_DATA_DIR` env
  (set by Electron to `app.getPath('userData')`), else a per-OS default
  (`%APPDATA%\BambuMonitor` on Windows, `${XDG_CONFIG_HOME:-~/.config}/BambuMonitor`
  on Linux). Created with `parents=True, exist_ok=True`. This is essential: an
  AppImage is a **read-only** mount, so state must never be written beside the
  executable.
- Build and run the app, reusing the tested wiring pieces:
  ```
  registry = PrinterRegistry(PrinterStore(data_dir/'printers.json'), real_factory)
  registry.load()
  queue    = PrintQueue(QueueStore(data_dir/'queues.json'))
  slicer   = <auto-detected: find_slicer() → SliceCoordinator, or None>
  app      = create_app(registry, runs_dir=data_dir/'runs', frontend_dist=dist,
                        detection=None, queue=queue, slicer=slicer)
  uvicorn.run(app, host='127.0.0.1', port=8000)
  registry.stop_all()  # in finally
  ```
- `detection=None` deliberately disables the entire detector path (routes 404,
  no subprocess, no torch import) — matching scope and guaranteeing the frozen
  build never tries to import a dependency it doesn't ship.

### 3.2 PyInstaller build
- **onedir** mode (faster startup, simpler to debug than onefile).
- `--add-data` bundles `frontend/dist`, and the printer TLS/cert assets and any
  `certifi`/`ftplib` data the runtime needs.
- **Hidden imports:** uvicorn's dynamically-loaded workers/protocols
  (`uvicorn.protocols.*`, `uvicorn.lifespan.*`, `uvicorn.loops.*`),
  `anyio`, `h11`, `websockets`/`wsproto`, `multipart`. Captured in a
  `bambu-backend.spec` checked into `desktop/`.
- **Excludes:** `torch`, `torchvision`, `ultralytics`, `matplotlib`, `pandas`,
  `scipy`, test frameworks — to keep the bundle small and prevent accidental
  torch inclusion.
- **Dependency trim:** the frozen build uses **`opencv-python-headless`** in
  place of `opencv-python`. Only `MockPrinter` draws frames with `cv2`; the real
  path never opens a camera. Headless avoids the `libGL.so.1` runtime dependency
  that breaks a plain `opencv-python` inside a minimal Linux/AppImage
  environment. Managed via a separate `requirements-desktop.txt`.
- Target bundle size: ~150–250 MB (FastAPI + uvicorn + opencv-headless + numpy).

## 4. Component 2 — Electron shell (`desktop/main.js`)

- On `app.whenReady()`: compute `userData = app.getPath('userData')`, spawn the
  backend binary as a child process with `BAMBU_DATA_DIR=userData`.
- **Readiness poll:** GET `http://127.0.0.1:8000/api/printers` on an interval
  until it returns, up to a timeout (~30 s). On timeout, show a native error
  dialog (`dialog.showErrorBox`) with the backend's captured stderr tail, then
  quit.
- Create the `BrowserWindow` and `loadURL('http://127.0.0.1:8000')`. Standard
  chrome (menu trimmed to essentials), product name **"Bambu Monitor"**, app
  icon.
- **Port:** fixed **8000**. If the bind fails (port in use) the backend exits;
  the readiness poll times out and the user sees the error dialog. An
  auto-pick-free-port fallback (backend prints the chosen port on stdout,
  Electron reads it) is a documented future enhancement, not in this pass.
- **Shutdown:** on `window-all-closed` and `before-quit`, terminate the backend
  child (`SIGTERM`, then kill after a grace period; `taskkill /T` on Windows to
  get the whole tree).

## 5. Component 3 — Packaging & build scripts

`desktop/package.json` holds the electron-builder config:
- `win`: target `nsis` (one-click installer, per-user, desktop + start-menu
  shortcut).
- `linux`: target `AppImage`.
- `extraResources`: the PyInstaller onedir output, staged so `main.js` can find
  the backend binary by a stable relative path (`process.resourcesPath`).

Because PyInstaller output is **platform-specific**, there are two build paths:

### 5.1 Windows — `desktop/build-windows.ps1` (run on this machine)
1. `cd frontend; npm ci; npm run build`
2. `pip install -r requirements-desktop.txt pyinstaller`
3. `pyinstaller desktop/bambu-backend.spec` → `dist/bambu-backend/`
4. `cd desktop; npm ci; npx electron-builder --win` → `Bambu Monitor Setup.exe`

### 5.2 Linux Mint — Docker-based (`desktop/build-linux.sh` + `desktop/Dockerfile.build`)
Built in a container so no toolchain is installed on the Mint host.
- `Dockerfile.build`: base on a Debian/Ubuntu image matching Mint's glibc
  (e.g. `ubuntu:22.04`), install Python 3.11, Node, and the electron-builder
  Linux deps. **PyInstaller must run inside this Linux container** (not on
  Windows) to produce a Linux binary.
- `build-linux.sh`: `docker build` the image, then `docker run` a script that
  performs the same 4 steps as §5.1 but with `electron-builder --linux`,
  writing `Bambu Monitor.AppImage` to a bind-mounted `./dist-linux/`.
- The resulting AppImage is `chmod +x`'d and runnable on Mint directly.

## 6. Data & configuration summary

| State | Location (Windows) | Location (Linux) |
|---|---|---|
| `printers.json` (LAN access codes, plaintext) | `%APPDATA%\BambuMonitor\` | `~/.config/BambuMonitor/` |
| `queues.json` | same | same |
| `runs/` (capture output; unused w/o detection) | same | same |

The data dir is chosen by Electron and passed via `BAMBU_DATA_DIR`; the launcher
falls back to the per-OS default if the env var is absent (so the frozen backend
is also runnable standalone for smoke testing).

## 7. Non-goals & honest caveats

- **Unsigned binaries.** Windows SmartScreen shows an "unknown publisher"
  warning the user dismisses; the AppImage just needs the executable bit. Real
  signing requires purchased certificates and is out of scope.
- **No detection / camera view** in the bundle (the excluded torch path).
- **Slicing** works only if the user separately installs Bambu Studio.
- **The Linux AppImage is built on Linux** (in Docker); it cannot be produced
  from the Windows dev machine.
- **Access codes are stored in plaintext** in the per-user data dir — unchanged
  from the current trust model (`bambu_link.py` already disables TLS verification
  on the LAN). The data dir is per-user, not world-readable by default.

## 8. Verification

1. **Frozen backend standalone:** run `dist/bambu-backend/bambu-backend` with a
   temp `BAMBU_DATA_DIR`; confirm `127.0.0.1:8000` serves the dashboard and
   `/api/printers` returns `[]`.
2. **Installed app (each OS):** install → app window loads the dashboard →
   add a printer → browse its SD card → upload a `.gcode.3mf` → confirm it
   appears in the listing. (Requires a reachable printer on the LAN for the last
   steps; the window-loads step is verifiable without one.)
3. **Clean shutdown:** close the window; confirm no orphaned backend process.
4. **Read-only-safe:** confirm the AppImage writes state to `~/.config/BambuMonitor`
   and not into the mount.

## 9. New files (no changes to `server/` or `frontend/` source)

```
desktop/
  launcher.py               # frozen backend entry point
  bambu-backend.spec        # PyInstaller spec
  main.js                   # Electron main process
  preload.js                # (minimal; contextIsolation-safe)
  package.json              # electron + electron-builder config
  build-windows.ps1
  build-linux.sh
  Dockerfile.build
  icons/                    # app icons (.ico, .png)
requirements-desktop.txt    # runtime deps w/ opencv-python-headless, no torch
docs/superpowers/specs/2026-07-22-electron-desktop-packaging-design.md
```

The existing `server/` and `frontend/` code is used **unmodified**; the launcher
composes the already-tested pieces (`PrinterRegistry`, `PrintQueue`,
`create_app`) rather than editing them.
