# Building the Linux Mint AppImage

Step-by-step for producing `Bambu Monitor <version>.AppImage` on a Linux Mint
machine. Everything except Docker lives inside the build container, so this is
the only thing you install.

Expect the first build to take **10–25 minutes** (it downloads an Ubuntu image,
Python packages, and the ~115 MB Electron binary). Later builds are much faster
thanks to Docker's cache.

---

## Step 0 — Get the code onto the Mint machine

**Option A — clone from GitHub** (needs the `dashboard` branch pushed first):

```bash
git clone -b dashboard https://github.com/adrianSalazar050/UCDavisGUI.git
cd UCDavisGUI
```

The tracked repo is tiny (~42 KiB) — datasets and model weights are gitignored,
and the desktop build doesn't need them.

**Option B — copy it manually** (USB stick / network share). Copy the project
folder, but you can safely skip these heavy, unnecessary directories:
`3d-printing-failure-detection.v1i.yolov8/`, `datasets/`, `runs/`,
`frontend/node_modules/`, `desktop/node_modules/`, `desktop/release/`, `dist/`.

---

## Step 1 — Install Docker

```bash
sudo apt update
sudo apt install -y docker.io
sudo systemctl enable --now docker
sudo usermod -aG docker $USER
```

**Log out and back in** (or reboot) so the group change takes effect. Verify:

```bash
docker run --rm hello-world
```

If that prints a hello message without `sudo`, you're set.

---

## Step 2 — Build the AppImage

From the repo root:

```bash
bash desktop/build-linux.sh
```

This builds an Ubuntu 22.04 toolchain image, then inside it: builds the React
frontend, freezes the backend with PyInstaller into a Linux binary, and packages
the Electron AppImage.

Result:

```
desktop/release/Bambu Monitor 0.1.0.AppImage
```

---

## Step 3 — Run it

AppImages on Mint 21+ (Ubuntu 22.04 base) need the old FUSE 2 library, which is
**not** installed by default:

```bash
sudo apt install -y libfuse2
```

Then:

```bash
chmod +x "desktop/release/Bambu Monitor 0.1.0.AppImage"
"./desktop/release/Bambu Monitor 0.1.0.AppImage"
```

The window should open on the dashboard. To install it like a normal app, just
move the AppImage anywhere you like (e.g. `~/Applications/`) and double-click it.

---

## What the app does and doesn't do

- **Included:** printer registry, live status/temps/layer/HMS, print queue,
  microSD browser, and `.gcode.3mf` upload.
- **Not included:** YOLO failure detection and the live camera view (the torch
  dependency is deliberately excluded — see the design spec).
- **Slicing:** only appears if Bambu Studio is separately installed on the same
  machine; otherwise it stays disabled.
- **Your data** (`printers.json`, `queues.json`, `runs/`) lives in
  `~/.config/BambuMonitor/`. The AppImage itself is read-only.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `permission denied ... /var/run/docker.sock` | You skipped the log-out after `usermod -aG docker`. Log out/in, or prefix with `sudo`. |
| `dlopen(): error loading libfuse.so.2` | `sudo apt install libfuse2` (Step 3). |
| AppImage does nothing on double-click | Run it from a terminal to see the error; make sure it's `chmod +x`. |
| Build fails on `npm ci` | Delete `frontend/node_modules` and `desktop/node_modules` on the host and re-run — stale Windows-built modules can leak in if you copied the folder rather than cloning. |
| `bash: desktop/build-linux.sh: /usr/bin/env^M` | The file has Windows line endings. `.gitattributes` prevents this for clones; if you copied manually, run `sed -i 's/\r$//' desktop/*.sh`. |
| Very slow build | Normal on first run. Docker caches the toolchain image, so re-runs are much quicker. |
| Window opens but says it can't reach the backend | Run the AppImage from a terminal and share the `[backend]` log lines — the backend is a child process and logs there. |

---

## Rebuilding after code changes

Just re-run `bash desktop/build-linux.sh`. To bump the version shown in the
filename, edit `version` in `desktop/package.json`.
