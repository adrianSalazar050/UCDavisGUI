#!/usr/bin/env bash
# Runs INSIDE the Docker container (see Dockerfile.build). Assumes the repo is
# mounted at /src and CWD is /src. Not meant to be run directly on the host --
# use build-linux.sh, which sets up the container and mounts the repo.
set -euo pipefail
cd /src
echo "== Repo root: $(pwd)"

# 1. Frontend -> frontend/dist
echo "== [1/4] Building frontend"
( cd frontend && npm ci && npm run build )

# 2. Python deps + PyInstaller into an isolated venv (don't touch the host env)
echo "== [2/4] Installing backend build dependencies"
python3.11 -m venv /tmp/venv
# shellcheck disable=SC1091
source /tmp/venv/bin/activate
pip install --upgrade pip
pip install -r requirements-desktop.txt pyinstaller

# 3. Freeze the backend -> dist/bambu-backend/ (Linux ELF)
echo "== [3/4] Freezing backend with PyInstaller"
rm -rf build/bambu-backend dist/bambu-backend
pyinstaller --noconfirm --clean desktop/bambu-backend.spec
test -x dist/bambu-backend/bambu-backend \
    || { echo "PyInstaller did not produce dist/bambu-backend/bambu-backend"; exit 1; }
deactivate

# 4. Electron AppImage -> desktop/release/*.AppImage
echo "== [4/4] Building Electron AppImage"
( cd desktop && npm ci && npx electron-builder --linux )

echo "== Done. Artifact(s):"
ls -1 desktop/release/*.AppImage 2>/dev/null || echo "  (no AppImage found -- check the log above)"
