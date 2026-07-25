# Building the Linux Mint AppImage

Produces `Bambu Monitor <version>.AppImage` on a Linux Mint machine. There are
two build methods — pick one:

| Method | When to use | Needs |
|---|---|---|
| **Native** (`build-linux-native.sh`) | You are ON the Mint machine you'll run it on. Simplest. | Python 3, Node, npm |
| **Docker** (`build-linux.sh`) | You want one AppImage that also runs on **older** distros than this one. | Docker |

Native links against *this* machine's glibc, so the AppImage is guaranteed to
run here but may refuse to start on an older distro. Docker builds against
Ubuntu 22.04's older glibc for wide portability. **For "build on my Mint, run on
my Mint", use native — it's faster and needs no Docker.**

---

## Step 0 — Get the code onto the Mint machine

**Option A — clone from GitHub** (needs the `dashboard` branch pushed first):

```bash
git clone -b dashboard https://github.com/adrianSalazar050/UCDavisGUI.git
cd UCDavisGUI
```

The tracked repo is tiny — datasets and model weights are gitignored, and the
desktop build doesn't need them.

**Option B — copy it manually** (USB stick / network share). Copy the project
folder, but you can safely skip these heavy, unnecessary directories:
`3d-printing-failure-detection.v1i.yolov8/`, `datasets/`, `runs/`,
`runs-mock/`, `frontend/node_modules/`, `desktop/node_modules/`,
`desktop/release/`, `build/`, `dist/`.
If you copied rather than cloned, strip any Windows line endings first:
`sed -i 's/\r$//' desktop/*.sh`.

---

## Method 1 — Native build (recommended for Mint)

### Prerequisites

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip nodejs npm libarchive-tools fakeroot
```

Vite needs a reasonably recent Node. If the distro's `nodejs` is too old (Mint
21 ships Node 12), install Node 20:

```bash
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt install -y nodejs
```

### Build

```bash
bash desktop/build-linux-native.sh
```

It builds the React frontend, freezes the backend with PyInstaller into a Linux
binary (in a throwaway venv — your system Python is untouched), and packages the
Electron AppImage. The script checks its prerequisites up front and prints an
`apt install` line if anything's missing.

Result: `desktop/release/Bambu Monitor <version>.AppImage`

---

## Method 2 — Docker build (for portability)

Install Docker once:

```bash
sudo apt update
sudo apt install -y docker.io
sudo systemctl enable --now docker
sudo usermod -aG docker $USER
```

**Log out and back in** (or reboot) so the group change takes effect, then:

```bash
docker run --rm hello-world   # should print a hello message without sudo
bash desktop/build-linux.sh
```

This builds an Ubuntu 22.04 toolchain image, then runs the same four build steps
inside it. Expect the first build to take **10–25 minutes** (it downloads an
Ubuntu image, Python packages, and the Electron binary); later builds are much
faster thanks to Docker's cache.

Result: `desktop/release/Bambu Monitor <version>.AppImage`

---

## Step 3 — Run it

AppImages need the old FUSE 2 library, which is **not** installed by default:

```bash
sudo apt install -y libfuse2      # Mint 21 / Ubuntu 22.04
sudo apt install -y libfuse2t64   # Mint 22 / Ubuntu 24.04 (package was renamed)
```

Then:

```bash
chmod +x "desktop/release/Bambu Monitor <version>.AppImage"
"./desktop/release/Bambu Monitor <version>.AppImage"
```

The window should open on the dashboard. To install it like a normal app, move
the AppImage anywhere you like (e.g. `~/Applications/`) and double-click it.

---

## What the app does and doesn't do

- **Included:** printer registry, live status/temps/layer/HMS, print queue,
  microSD browser, `.gcode.3mf` upload, **automatic slicing** (STL → sliced
  `.gcode.3mf` → queued), and the traceability ledger — run history, the parts
  catalogue, and filament inventory.
- **Not included:** YOLO failure detection and the live camera view (the torch
  dependency is deliberately excluded — see the design spec).
- **Your data** (`printers.json`, `queues.json`, `ledger.db` + its `parts/`
  model files, and `runs/`) lives in `~/.config/BambuMonitor/`. The AppImage
  itself is read-only, which is the whole reason nothing is written beside it.

### Enabling slicing on Linux

Slicing shells out to **Bambu Studio**, which is not bundled. If it's installed,
the app auto-detects it in the usual places (an AppImage in `~/Applications`,
`~/Downloads`, or `~/.local/bin`; a binary on `/usr/bin`, `/usr/local/bin`, or
`/opt/...`). If your install isn't found, point at it explicitly before
launching:

```bash
export BAMBU_STUDIO_EXE="$HOME/Applications/Bambu_Studio_ubuntu.AppImage"
# Only if profile auto-detection also misses (rare):
export BAMBU_STUDIO_PROFILES="$HOME/.config/BambuStudio/system/BBL"
```

The profiles directory is the one Bambu Studio writes on its first run. If
neither is set and nothing is auto-detected, the Slice page simply stays
disabled — everything else works.

> **Not yet verified on Linux.** The slicing pipeline was verified end to end on
> Windows (a full clean print). The Linux auto-detection paths and the AppImage
> profile layout are best-effort and untested on real Linux hardware — the env
> vars above are the reliable fallback. Monitoring, queue, SD upload, and the
> rest are platform-agnostic and expected to work as on Windows.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `permission denied ... /var/run/docker.sock` | You skipped the log-out after `usermod -aG docker`. Log out/in, or prefix with `sudo`. (Docker method only.) |
| `dlopen(): error loading libfuse.so.2` | Install FUSE 2 (Step 3) — `libfuse2` on Mint 21, `libfuse2t64` on Mint 22. |
| AppImage does nothing on double-click | Run it from a terminal to see the error; make sure it's `chmod +x`. |
| `build-linux-native.sh` says a tool is missing | Run the `apt install` line it prints, then re-run. |
| Vite build fails / syntax errors | Node is too old — install Node 20 (see Method 1 prerequisites). |
| `npm ci` fails after copying (not cloning) | Delete `frontend/node_modules` and `desktop/node_modules` — stale Windows-built modules can leak in. |
| `/usr/bin/env: bash^M` | Windows line endings. `sed -i 's/\r$//' desktop/*.sh`. |
| Window opens but "can't reach the backend" | Run the AppImage from a terminal and share the `[backend]` log lines — the backend is a child process and logs there. |
| Slice tab missing / greyed | Bambu Studio wasn't found. Set `BAMBU_STUDIO_EXE` (see "Enabling slicing"). |

---

## Rebuilding after code changes

Re-run whichever build script you used. To bump the version shown in the
filename, edit `version` in `desktop/package.json`.
