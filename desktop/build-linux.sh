#!/usr/bin/env bash
# Build the Linux AppImage for Bambu Monitor using Docker.
#
# Run from ANYWHERE inside the repo on a Linux Mint host with Docker installed:
#     bash desktop/build-linux.sh
#
# Produces: desktop/release/Bambu Monitor <version>.AppImage
#
# Why Docker: the AppImage must be built on Linux against an older glibc
# (Ubuntu 22.04) for portability. The container carries the exact toolchain, so
# nothing needs installing on the Mint host except Docker itself.
set -euo pipefail

# Resolve the repo root as this script's parent's parent, so it works from any CWD.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"
echo "== Repo root: $REPO_ROOT"

if ! command -v docker >/dev/null 2>&1; then
    echo "ERROR: docker is not installed. Install Docker on this machine first:" >&2
    echo "  sudo apt install docker.io && sudo usermod -aG docker \$USER  (then re-login)" >&2
    exit 1
fi

IMAGE="bambu-monitor-build"

echo "== Building toolchain image ($IMAGE)"
docker build -t "$IMAGE" -f desktop/Dockerfile.build desktop

echo "== Running the build in a container"
# Mount the repo read-write so build outputs (frontend/dist, dist/, desktop/release)
# land back on the host. node_modules/venv are created inside and are disposable.
docker run --rm \
    -v "$REPO_ROOT":/src \
    -w /src \
    "$IMAGE" \
    bash desktop/build-linux-inside.sh

echo "== Done. Artifact(s):"
ls -1 "$REPO_ROOT"/desktop/release/*.AppImage 2>/dev/null \
    || echo "  (no AppImage found -- check the log above)"
