#!/usr/bin/env bash
# Build the Linux AppImage for Bambu Monitor NATIVELY, directly on a Linux host
# (e.g. Linux Mint). No Docker required -- this is the counterpart to
# build-windows.ps1 and is the simplest path when you are already ON the Linux
# machine you want to run the app on.
#
#     bash desktop/build-linux-native.sh
#
# Produces: desktop/release/Bambu Monitor <version>.AppImage
#
# Docker vs native: build-linux.sh builds inside an Ubuntu 22.04 container so the
# binary links an OLD glibc and runs on many distros. Building natively links
# THIS machine's glibc, so the AppImage is guaranteed to run here but may refuse
# to start on an OLDER distro than this one. For "build it on my Mint and run it
# on my Mint", native is perfect. To ship to older machines, prefer build-linux.sh.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"
echo "== Repo root: $REPO_ROOT"

# ---- prerequisites -------------------------------------------------------
# Fail early with an apt hint rather than deep inside a build step.
missing=""
need() { command -v "$1" >/dev/null 2>&1 || missing="$missing $2"; }

# Prefer python3.11 (the dev interpreter) but accept any python3 >= 3.10.
PY=""
for c in python3.11 python3; do
    if command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi
done
[ -n "$PY" ] || missing="$missing python3.11(python3-venv)"

need node nodejs
need npm npm

if [ -n "$missing" ]; then
    echo "ERROR: missing build tools:$missing" >&2
    echo "On Linux Mint / Ubuntu install them with:" >&2
    echo "  sudo apt update" >&2
    echo "  sudo apt install -y python3 python3-venv python3-pip nodejs npm libarchive-tools fakeroot" >&2
    echo "(If 'nodejs' is too old for Vite, install Node 20 from https://deb.nodesource.com/)" >&2
    exit 1
fi

if [ -n "$PY" ]; then
    PYV="$("$PY" -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
    echo "== Python: $PY ($PYV)   Node: $(node --version)   npm: $(npm --version)"
    case "$PYV" in
        3.10|3.11|3.12|3.13) : ;;
        *) echo "   WARNING: python $PYV is untested; 3.11 is the reference." >&2 ;;
    esac
fi

# ---- 1. frontend ---------------------------------------------------------
echo "== [1/4] Building frontend -> frontend/dist"
( cd frontend && npm ci && npm run build )

# ---- 2. backend build deps in an isolated venv ---------------------------
echo "== [2/4] Installing backend build dependencies (isolated venv)"
VENV="$(mktemp -d)/venv"
"$PY" -m venv "$VENV"
# shellcheck disable=SC1091
source "$VENV/bin/activate"
pip install --upgrade pip
pip install -r requirements-desktop.txt pyinstaller

# ---- 3. freeze the backend (Linux ELF) -----------------------------------
echo "== [3/4] Freezing backend with PyInstaller -> dist/bambu-backend/"
rm -rf build/bambu-backend dist/bambu-backend
pyinstaller --noconfirm --clean desktop/bambu-backend.spec
test -x dist/bambu-backend/bambu-backend \
    || { echo "ERROR: PyInstaller did not produce dist/bambu-backend/bambu-backend" >&2; exit 1; }
deactivate

# ---- 4. electron AppImage ------------------------------------------------
echo "== [4/4] Building Electron AppImage -> desktop/release/"
( cd desktop && npm ci && npx electron-builder --linux )

echo ""
echo "== Done. Artifact(s):"
if ls -1 "$REPO_ROOT"/desktop/release/*.AppImage >/dev/null 2>&1; then
    ls -1 "$REPO_ROOT"/desktop/release/*.AppImage
    echo ""
    echo "To RUN the AppImage you may need FUSE:"
    echo "  Mint 21 / Ubuntu 22.04:  sudo apt install -y libfuse2"
    echo "  Mint 22 / Ubuntu 24.04:  sudo apt install -y libfuse2t64"
    echo "Then: chmod +x 'desktop/release/Bambu Monitor'*.AppImage && ./'desktop/release/Bambu Monitor'*.AppImage"
    echo ""
    echo "Slicing: if Bambu Studio is installed, it's auto-detected. If not found,"
    echo "point at it explicitly, e.g.:"
    echo "  export BAMBU_STUDIO_EXE=\"\$HOME/Applications/Bambu_Studio.AppImage\""
    echo "  export BAMBU_STUDIO_PROFILES=\"\$HOME/.config/BambuStudio/system/BBL\"  # if auto-detect misses"
else
    echo "  (no AppImage found -- check the log above)"
fi
