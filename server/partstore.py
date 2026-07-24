"""Model-file storage for parts: bytes on disk, sha256 for integrity.

Kept out of server/ledger.py deliberately -- the ledger holds the metadata
row (filename, sha256, size); the bytes live here on disk under the same data
dir as ledger.db so the desktop build's BAMBU_DATA_DIR covers both. Model
files can be large (an STL is easily tens of MB); putting them in SQLite would
bloat the file the recorder writes to on every tick.
"""
from __future__ import annotations

import hashlib
import ntpath
import os
import pathlib
import re
import shutil

MODEL_EXTS = (".stl", ".3mf", ".step", ".stp", ".obj")

# Anything outside this set is replaced with "_". This strips not just path
# separators but also quotes and control characters (CR/LF), so the stored
# name is safe to interpolate into an HTTP Content-Disposition header on
# download without header-splitting or a broken filename="..." quote.
_UNSAFE_CHARS = re.compile(r"[^\w.\- ]")


def _safe_name(filename: str) -> str:
    """A leaf filename safe on disk AND in an HTTP header. Strips any directory
    component (Windows- or POSIX-style: ntpath handles backslashes even on
    Linux, os.path.basename catches forward slashes on every OS), then replaces
    every character outside [word, dot, dash, space] -- quotes, CR, LF -- so a
    hostile filename can neither escape the part dir nor inject a header."""
    leaf = os.path.basename(ntpath.basename(filename))
    return _UNSAFE_CHARS.sub("_", leaf).strip()


class PartStore:
    def __init__(self, root):
        self.root = pathlib.Path(root)

    def _dir(self, part_id: str) -> pathlib.Path:
        # part_id is a server-generated uuid hex -- no separators -- so it
        # cannot traverse; the filename is basenamed separately in save().
        return self.root / "parts" / part_id

    def save(self, part_id: str, filename: str, data: bytes) -> dict:
        """-> {filename, sha256, bytes}. Clears any prior model for this part
        first, so a re-upload under a different name never leaves two."""
        name = _safe_name(filename)
        if not name or not name.lower().endswith(MODEL_EXTS):
            raise ValueError(f"not a model file: {filename!r}")
        d = self._dir(part_id)
        d.mkdir(parents=True, exist_ok=True)
        for old in d.iterdir():
            old.unlink()
        (d / name).write_bytes(data)
        return {"filename": name,
                "sha256": hashlib.sha256(data).hexdigest(),
                "bytes": len(data)}

    def open_bytes(self, part_id: str, filename: str) -> bytes:
        return (self._dir(part_id) / _safe_name(filename)).read_bytes()

    def delete(self, part_id: str) -> None:
        shutil.rmtree(self._dir(part_id), ignore_errors=True)
