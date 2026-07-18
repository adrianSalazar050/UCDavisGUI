"""Per-printer print queue: order SD-card jobs, cache their parsed metrics,
and persist across restarts (queues.json).

A job is a plain dict: {id, sd_path, name, seconds, grams, source}. No
secrets ever live here -- only filenames and cached metrics parsed from a
.gcode.3mf (server/threemf.py) or entered manually. This module is a planner
only: it never talks to a printer or the network. The API layer
(server/main.py) does the FTPS fetch + 3MF parse and hands PrintQueue a
finished job dict via add().

QueueStore mirrors PrinterStore's (server/store.py) file-safety rules: atomic
write via temp-file + os.replace (+ flush/fsync, so a crash mid-write can
never leave a truncated or zero-length queues.json), and a tolerant
utf-8-sig read that degrades to "no queues" rather than ever raising -- a
corrupt/hand-edited queues.json must not stop the server from booting.

Note on the module name: `server/queue.py` shadows nothing. Python 3's
imports are absolute by default, so `import queue` anywhere else in this
codebase still resolves to the stdlib module; only `from .queue import ...`
/ `from server.queue import ...` reach this file.
"""
from __future__ import annotations

import json
import logging
import os
import pathlib
import tempfile
import threading
import time

log = logging.getLogger("server.queue")


class QueueStore:
    """queues.json on disk: {serial: [job, ...]}. Never raises on read --
    same reasoning as PrinterStore: a corrupt file must not stop the server
    from booting."""

    def __init__(self, path: pathlib.Path):
        self.path = pathlib.Path(path)

    def load(self) -> dict:
        try:
            # utf-8-sig, not utf-8: see store.py -- a BOM'd file (Notepad and
            # friends) is otherwise-good JSON that would silently read as
            # "no queues" under plain utf-8.
            raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            return {}
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
            log.warning("%s is unreadable (%s); starting with no queues",
                        self.path, e)
            return {}
        if not isinstance(raw, dict):
            log.warning("%s is not a JSON object; starting with no queues",
                        self.path)
            return {}
        out: dict[str, list[dict]] = {}
        for serial, jobs in raw.items():
            if not isinstance(serial, str) or not isinstance(jobs, list):
                log.warning("skipping malformed entry for %r in %s",
                            serial, self.path)
                continue
            out[serial] = [j for j in jobs if isinstance(j, dict)]
        return out

    def save(self, data: dict) -> None:
        """Atomic and durable: os.replace() makes the swap atomic, and the
        flush()+fsync() below make sure the data is actually on disk before
        that swap lands. Mirrors PrinterStore.save() exactly -- see
        server/store.py for the full argument."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(data, indent=2)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent),
                                   prefix=self.path.name, suffix=".tmp")
        try:
            try:
                f = os.fdopen(fd, "w", encoding="utf-8")
            except BaseException:
                os.close(fd)  # fdopen failed -- the fd is still ours to
                raise         # close, or it leaks and later masks this error
            with f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            pathlib.Path(tmp).unlink(missing_ok=True)
            raise


class PrintQueue:
    """{serial: [job, ...]} guarded by a lock, persisting after each
    mutation. Pure of network/registry -- the API layer does the fetch+parse
    and hands finished job dicts to add().

    Locking mirrors registry.py's two-lock design: `self._lock` guards the
    in-memory `_jobs` dict and is held only for quick dict work, never across
    disk I/O. `self._persist_lock` serializes the snapshot-then-write
    sequence in `_persist()` so two concurrent mutations for different
    printers (FastAPI routes run on a threadpool, so this is a real race)
    can't race their store.save() calls and silently drop one of them from
    queues.json -- see registry.py's `_persist()` docstring for the full
    argument, which applies here unchanged.
    """

    def __init__(self, store: QueueStore):
        self._store = store
        self._jobs: dict[str, list[dict]] = store.load()
        self._lock = threading.Lock()
        self._persist_lock = threading.Lock()

    # ---------------- mutation ----------------

    def add(self, serial: str, job: dict) -> None:
        with self._lock:
            self._jobs.setdefault(serial, []).append(dict(job))
        self._persist()

    def remove(self, serial: str, job_id) -> bool:
        with self._lock:
            jobs = self._jobs.get(serial)
            if not jobs:
                return False
            kept = [j for j in jobs if j.get("id") != job_id]
            removed = len(kept) != len(jobs)
            if removed:
                self._jobs[serial] = kept
        if removed:
            self._persist()
        return removed

    def reorder(self, serial: str, ids: list) -> None:
        """Keep only known ids, in the given order; an id in `ids` that
        isn't a current job (a stale client, a job removed meanwhile) is
        silently dropped rather than raising."""
        with self._lock:
            jobs = self._jobs.get(serial)
            if not jobs:
                return
            by_id = {j.get("id"): j for j in jobs}
            self._jobs[serial] = [by_id[i] for i in ids if i in by_id]
        self._persist()

    # ---------------- reads ----------------

    def get(self, serial: str) -> list:
        with self._lock:
            return list(self._jobs.get(serial, []))

    def totals(self, serial: str, now: float | None = None) -> dict:
        """Sums non-None `seconds`/`grams` across the queue. `finish_epoch`
        is a planner hint (labeled "~" in the UI), not a guarantee -- it is
        None whenever there is nothing to time (seconds is 0 or every job's
        seconds is None, e.g. an all-manual queue)."""
        if now is None:
            now = time.time()
        jobs = self.get(serial)
        seconds = sum(j.get("seconds") for j in jobs
                      if isinstance(j.get("seconds"), (int, float)))
        grams = sum(j.get("grams") for j in jobs
                    if isinstance(j.get("grams"), (int, float)))
        return {"seconds": seconds, "grams": round(grams, 2),
                "finish_epoch": (now + seconds) if seconds else None}

    # ---------------- internals ----------------

    def _persist(self) -> None:
        """Serialized through `_persist_lock`, separate from `self._lock` --
        see the class docstring / registry.py's `_persist()` for why."""
        with self._persist_lock:
            with self._lock:
                snapshot = {serial: list(jobs)
                           for serial, jobs in self._jobs.items()}
            self._store.save(snapshot)
