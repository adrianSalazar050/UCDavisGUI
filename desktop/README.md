# Bambu Monitor — desktop app (Electron)

Packages the dashboard into an installable desktop app: an Electron shell that
launches the frozen FastAPI backend and opens a window on it. **Scope: dashboard
+ SD-card `.gcode.3mf` upload.** Failure detection (torch) is intentionally
excluded; slicing lights up only if the user already has Bambu Studio installed.

See the design: [`../docs/superpowers/specs/2026-07-22-electron-desktop-packaging-design.md`](../docs/superpowers/specs/2026-07-22-electron-desktop-packaging-design.md).

## What's here

| File | Role |
|---|---|
| `launcher.py` | Frozen-backend entry point; composes `create_app(..., detection=None)` and resolves a per-user writable data dir |
| `bambu-backend.spec` | PyInstaller spec (onedir); bundles `frontend/dist`, excludes torch |
| `main.js` | Electron main process: pick a free port (`getFreePort()`) → spawn backend on it → wait for it → open window → kill backend on quit |
| `preload.js` | Minimal (empty) preload; keeps `contextIsolation` on |
| `package.json` | Electron + electron-builder config (NSIS `.exe`, AppImage) |
| `build-windows.ps1` | Full Windows build (run on Windows) |
| `build-linux-native.sh` | Linux build on the machine you'll run it on — no Docker, links against this machine's glibc ([`LINUX-BUILD.md`](LINUX-BUILD.md) Method 1) |
| `build-linux.sh` + `Dockerfile.build` + `build-linux-inside.sh` | Linux build inside Ubuntu 22.04 for portability to older distros (needs Docker; Method 2) |
| `icons/` | App icons (`icon.ico`, `icon.png`) |

**The port is chosen per launch, never a fixed 8000.** `main.js::getFreePort()`
binds `:0` and passes the number to the backend as `BAMBU_PORT`. A fixed 8000
made the readiness poll succeed against a *dev server already on 8000*, and
Electron then showed the wrong backend — observed on the dev box, which is why
the fixed port was removed. Don't reintroduce one.

## Build — Windows

Needs Python 3.11+, Node 18+. From the **repo root**:

```powershell
powershell -ExecutionPolicy Bypass -File desktop\build-windows.ps1
```

Output: `desktop\release\Bambu Monitor Setup <version>.exe`.

## Build — Linux Mint

Needs Docker only. From anywhere in the repo:

```bash
bash desktop/build-linux.sh
```

Output: `desktop/release/Bambu Monitor <version>.AppImage`. `chmod +x` it and run.

## Run without packaging (dev)

Freeze the backend once (`pyinstaller desktop/bambu-backend.spec` after
`pip install -r requirements-desktop.txt pyinstaller` and `npm --prefix frontend
run build`), then:

```bash
cd desktop && npm install && npm start
```

## Where user data lives

`printers.json`, `queues.json`, `ledger.db` (+ its `parts/` model files), and
`runs/` are written to a per-user dir, never beside the app (an AppImage is
read-only):

- Windows: `%APPDATA%\BambuMonitor\`
- Linux: `~/.config/BambuMonitor/`

`launcher.py::data_dir()` resolves it; Electron passes it as `BAMBU_DATA_DIR`.

## Caveats

- Installers are **unsigned**: Windows SmartScreen shows an "unknown publisher"
  prompt; the AppImage just needs the executable bit.
- No live camera view / failure detection in this bundle.
- Slicing requires a separate Bambu Studio install.
