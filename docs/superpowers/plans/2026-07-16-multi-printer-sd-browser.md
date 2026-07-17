# Multi-Printer Connection Manager + SD Card Browser Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the user add Bambu printers at runtime by typing IP + serial + LAN access code in the browser, see every printer's live status on an Overview grid, and browse each printer's microSD filenames — per the approved spec in `docs/superpowers/specs/2026-07-16-multi-printer-sd-browser-design.md`.

**Architecture:** A `PrinterRegistry` replaces the single `PrinterService` that `create_app` currently closes over. It owns one service per printer keyed by serial, persists configs to a gitignored `printers.json`, and is fanned out over the existing WebSocket as `{"printers": [...]}`. A new `sdcard.py` lists the microSD over FTPS (port 990, implicit TLS) — MQTT exposes no file listing — on demand, on a threadpool, never on the event loop. The frontend gains an Overview grid (landing page), an SD Files page, and a `Field` primitive.

**Tech Stack:** Python 3.11+, FastAPI, uvicorn, paho-mqtt, `ftplib`/`ssl` (stdlib — no new deps), pytest; React 19, Vite 6, plain JSX, one `styles.css`.

**Read this before trusting a code block below.** The code in this plan is a
starting point, not a verified reference implementation. Task 1's two-stage
review found **six** real defects in ~120 lines that this plan had specified —
including a `.gitignore` rule that failed to cover the temp file `save()`
writes the plaintext access code into, and a coercion bug that silently
collapsed several printers onto one registry key at boot. Task 1's blocks have
since been corrected; **the later tasks' blocks have not had that scrutiny.**
Treat every block as a proposal to review, and every test list as a floor.
Where a code block and a stated requirement disagree, the requirement wins —
say so rather than implementing the block.

**Environment notes for the engineer:**
- Windows machine, PowerShell. Do NOT chain commands with `&&` (PowerShell 5.1 rejects it); run commands one at a time.
- Repo root is `c:\Users\adria\OneDrive\Escritorio\GUI_UCDavis`. Run all `pytest` commands from the repo root.
- `bambu_link.py`, `capture.py`, `check_registration.py`, `probe_gcode.py`, and `server/runs.py` must NOT be modified.
- The printer is **offline** as of writing (all ports time out). Everything in this plan is verifiable against `--mock`. Real FTPS and real multi-printer MQTT are deferred to hardware — see the final task.
- The printer sends PARTIAL MQTT updates; `BambuLink` already deep-merges them. You only consume it.
- **The LAN access code is a password.** It must never appear in a response body, a log line, or git. Task 1 adds `printers.json` to `.gitignore` before any code can write it.

---

## File structure

```
.gitignore                          # Task 1 — printers.json
server/
  store.py                          # Task 1 — PrinterConfig, PrinterStore, MemoryStore
  sdcard.py                         # Task 2 — FTPS listing, path guard, parsers
  printer.py                        # Task 3 (build_summary) + Task 4 (services)
  registry.py                       # Task 5 — PrinterRegistry
  main.py                           # Task 6 — create_app(registry, ...)
  __main__.py                       # Task 7 — CLI, mock seeding
  runs.py                           # UNCHANGED
  tests/
    test_store.py                   # Task 1
    test_sdcard.py                  # Task 2
    test_summary.py                 # Task 3 (modify)
    test_services.py                # Task 4 (modify)
    test_registry.py                # Task 5
    test_api.py                     # Task 6 (rewrite)
    test_runs.py                    # UNCHANGED
frontend/src/
  styles.css                        # Task 8 — all new classes
  components/ui/Field.jsx           # Task 8
  api/printer.js                    # Task 9 (modify)
  hooks/usePrinters.js              # Task 9 (replaces usePrinter.js)
  app/pageRegistry.jsx              # Task 9 (modify)
  App.jsx                           # Task 9 (modify)
  components/printers/PrinterCard.jsx      # Task 10
  components/printers/AddPrinterForm.jsx   # Task 10
  pages/Overview.jsx                # Task 10
  components/sd/FileTable.jsx       # Task 11
  pages/SdFiles.jsx                 # Task 11
  pages/Dashboard.jsx               # Task 12 (modify)
README (1).md                       # Task 13
CONNECTION.md                       # Task 13
```

**Deleted:** `frontend/src/hooks/usePrinter.js` (Task 9), `GET /api/status` (Task 6).

---

### Task 1: `server/store.py` — printers.json persistence

Pure file I/O, split from the registry so it is testable against `tmp_path` with no threads or sockets.

**Files:**
- Modify: `.gitignore`
- Create: `server/store.py`
- Test: `server/tests/test_store.py`

- [ ] **Step 1: Add `printers.json` to `.gitignore` BEFORE writing any code that creates it**

Add these two lines to `.gitignore` immediately after the `runs-mock/` line:

```gitignore
# holds LAN access codes in plaintext — must never be committed.
# The wildcard is load-bearing: save() writes a temp file alongside the real
# one, containing the same plaintext, and an interrupted save orphans it.
printers.json*
```

The `*` is not cosmetic. `save()` builds its temp file with
`prefix=self.path.name`, so it is named `printers.json<random>.tmp` and this
one pattern covers both. gitignore globs anchor at the start of the filename,
so a temp file named `tmp<random>.tmp` (mkstemp's default prefix) would NOT be
matched — the prefix, not the suffix, is what makes this work.

- [ ] **Step 2: Write the failing tests**

Create `server/tests/test_store.py`:

```python
import json

from server.store import MemoryStore, PrinterConfig, PrinterStore


def cfg(serial="0300CA633005010", host="192.168.137.2", code="31661007",
        name="", capture=False):
    return PrinterConfig(serial=serial, host=host, access_code=code,
                         name=name, capture=capture)


def test_name_defaults_to_host():
    assert cfg().name == "192.168.137.2"


def test_name_kept_when_given():
    assert cfg(name="A1-bench").name == "A1-bench"


def test_fields_are_stripped():
    c = PrinterConfig(serial=" S1 ", host=" 10.0.0.1 ", access_code=" abc ")
    assert (c.serial, c.host, c.access_code) == ("S1", "10.0.0.1", "abc")


def test_missing_file_loads_empty(tmp_path):
    assert PrinterStore(tmp_path / "nope.json").load() == []


def test_round_trip(tmp_path):
    p = tmp_path / "printers.json"
    store = PrinterStore(p)
    store.save([cfg(name="A1-bench", capture=True)])
    got = store.load()
    assert len(got) == 1
    assert got[0].serial == "0300CA633005010"
    assert got[0].access_code == "31661007"
    assert got[0].name == "A1-bench"
    assert got[0].capture is True


def test_corrupt_json_loads_empty_and_does_not_raise(tmp_path):
    p = tmp_path / "printers.json"
    p.write_text("{not json at all", encoding="utf-8")
    assert PrinterStore(p).load() == []


def test_non_list_json_loads_empty(tmp_path):
    p = tmp_path / "printers.json"
    p.write_text('{"serial": "x"}', encoding="utf-8")
    assert PrinterStore(p).load() == []


def test_entry_missing_required_field_is_skipped(tmp_path):
    p = tmp_path / "printers.json"
    p.write_text(json.dumps([
        {"serial": "good", "host": "1.2.3.4", "access_code": "code"},
        {"serial": "bad-no-host", "access_code": "code"},
    ]), encoding="utf-8")
    got = PrinterStore(p).load()
    assert [c.serial for c in got] == ["good"]


def test_entry_with_wrong_typed_field_is_skipped(tmp_path):
    # Structurally valid, all keys present, but serial is a number ->
    # __post_init__'s .strip() would raise AttributeError. printers.json is
    # hand-editable and this is the boot path: raising here kills the server
    # and leaves no UI to fix the file with.
    p = tmp_path / "printers.json"
    p.write_text(json.dumps([
        {"serial": 12345, "host": "1.2.3.4", "access_code": "abc"},
        {"serial": "good", "host": "1.2.3.4", "access_code": "code"},
    ]), encoding="utf-8")
    got = PrinterStore(p).load()
    assert [c.serial for c in got] == ["good"]


def test_save_leaves_no_temp_files(tmp_path):
    store = PrinterStore(tmp_path / "printers.json")
    store.save([cfg()])
    assert sorted(f.name for f in tmp_path.iterdir()) == ["printers.json"]


def test_save_creates_parent_dir(tmp_path):
    store = PrinterStore(tmp_path / "sub" / "printers.json")
    store.save([cfg()])
    assert len(store.load()) == 1


def test_memory_store_round_trips_without_disk():
    store = MemoryStore()
    assert store.load() == []
    store.save([cfg()])
    assert len(store.load()) == 1


def test_memory_store_load_returns_a_copy():
    store = MemoryStore()
    store.save([cfg()])
    store.load().clear()
    assert len(store.load()) == 1


def test_invalid_utf8_bytes_load_empty(tmp_path):
    # PowerShell's Out-File/Set-Content default to UTF-16 LE with a BOM, so a
    # hand-edit from a PS prompt lands 0xFF 0xFE here. UnicodeDecodeError
    # subclasses ValueError, not OSError -- it must be caught explicitly or it
    # escapes load() and kills the boot path.
    p = tmp_path / "printers.json"
    p.write_bytes(b"\xff\xfe[{\"serial\": \"x\"}]")
    assert PrinterStore(p).load() == []


def test_utf8_bom_file_still_loads(tmp_path):
    # Notepad and friends prepend a UTF-8 BOM on save. The JSON is fine; only
    # the three leading bytes aren't. Reading with utf-8 would make every
    # configured printer silently vanish.
    p = tmp_path / "printers.json"
    p.write_bytes(b"\xef\xbb\xbf" + json.dumps(
        [{"serial": "S1", "host": "1.2.3.4", "access_code": "c"}]
    ).encode("utf-8"))
    got = PrinterStore(p).load()
    assert [c.serial for c in got] == ["S1"]
```

**Note on `load()`'s "never raises" contract:** it is load-bearing, and the
obvious tests do not cover it. `printers.json` is hand-editable by design and
`load()` runs on the boot path, so anything that escapes here kills the server
and leaves no UI to fix the file with. Task 1's review found three separate
defects against this contract. When you touch this function, re-run a hostile
sweep (UTF-16 and UTF-8 BOMs, raw binary, empty file, bare JSON scalars, nested
lists, wrong-typed and falsy field values, a directory in place of the file) and
confirm zero raises.

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest server/tests/test_store.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'server.store'`

- [ ] **Step 4: Implement `server/store.py`**

```python
"""Persistence for the registered-printer list (printers.json).

Holds the LAN access code in PLAINTEXT. That is deliberate -- the same trust
model bambu_link.py already takes by disabling TLS verification on a LAN -- but
it is exactly why printers.json is in .gitignore. Never echo access_code back
out of the API.

Kept free of threads and sockets so it is testable against a tmp_path; the
lifecycle side lives in registry.py.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import os
import pathlib
import tempfile

log = logging.getLogger("server.store")


@dataclasses.dataclass
class PrinterConfig:
    """One registered printer. `name` falls back to the host."""

    serial: str
    host: str
    access_code: str
    name: str = ""
    capture: bool = False

    def __post_init__(self) -> None:
        self.serial = (self.serial or "").strip()
        self.host = (self.host or "").strip()
        self.access_code = (self.access_code or "").strip()
        self.name = (self.name or "").strip() or self.host

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "PrinterConfig":
        # Validate types BEFORE __post_init__ strips. The (x or "") idiom above
        # tolerates None/0/False, so a falsy wrong-typed serial would coerce to
        # "" and be accepted -- and the registry keys on serial, so several such
        # entries collapse onto one printer and silently overwrite each other.
        # Reject, don't coerce.
        for k in ("serial", "host", "access_code"):
            # KeyError if absent; TypeError if `d` isn't a dict at all.
            if not isinstance(d[k], str):
                raise TypeError(
                    f"{k} must be a string, got {type(d[k]).__name__}")
        # The two optional fields stay tolerant -- they're cosmetic, and a bad
        # one shouldn't cost the user a whole printer.
        name = d.get("name")
        capture = d.get("capture", False)
        if not isinstance(capture, bool):
            # NOT bool(capture): that turns the string "false" into True, and
            # capture is single-occupancy, so it would steal the webcam from
            # another printer. Default to the safe direction.
            log.warning("capture must be true/false, got %r; treating as false",
                        capture)
            capture = False
        return cls(
            serial=d["serial"], host=d["host"], access_code=d["access_code"],
            name=name if isinstance(name, str) else "", capture=capture)


class PrinterStore:
    """printers.json on disk. Never raises on read -- a corrupt file must not
    stop the server from booting; you would have no UI left to fix it with."""

    def __init__(self, path: pathlib.Path):
        self.path = pathlib.Path(path)

    def load(self) -> list[PrinterConfig]:
        try:
            # utf-8-sig, not utf-8: Windows editors (Notepad, some VS Code
            # configs) prepend a BOM on save. A BOM'd file is perfectly good
            # JSON, but utf-8 would fail it -- and this function's failure mode
            # is "no printers", so the user's whole list would silently vanish
            # after they opened the file to check an IP. utf-8-sig is identical
            # to utf-8 when no BOM is present, so this is free.
            raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            return []
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
            # UnicodeDecodeError subclasses ValueError, NOT OSError, so it must
            # be named explicitly or it escapes and kills the boot path.
            # PowerShell's Out-File/Set-Content default to UTF-16 LE, so a
            # hand-edit from a PS prompt lands exactly here.
            log.warning("%s is unreadable (%s); starting with no printers",
                        self.path, e)
            return []
        if not isinstance(raw, list):
            log.warning("%s is not a JSON list; starting with no printers",
                        self.path)
            return []
        out = []
        for entry in raw:
            try:
                out.append(PrinterConfig.from_dict(entry))
            except (KeyError, TypeError) as e:
                # KeyError: a required key is absent. TypeError: `entry` isn't a
                # dict, or a required field isn't a string (from_dict checks).
                # Do NOT add AttributeError here: with explicit validation it is
                # unreachable, and catching it would swallow a genuine bug
                # introduced later inside from_dict/__post_init__ -- silently
                # skipping EVERY printer in the file with only a warning.
                log.warning("skipping malformed entry in %s: %s", self.path, e)
        return out

    def save(self, configs: list[PrinterConfig]) -> None:
        """Atomic and durable: an interrupted write must not leave a truncated
        file, and must not lose the printers that were already saved.

        The temp file holds the SAME plaintext access codes as the real one, so
        it is named with prefix=self.path.name to fall under .gitignore's
        `printers.json*`. Do not "tidy" that prefix away: mkstemp's default
        prefix is "tmp", gitignore globs anchor at the start of the filename,
        and an orphaned tmp*.tmp at the repo root is a committable password.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps([c.to_dict() for c in configs], indent=2)
        # dir=self.path.parent also guarantees same-filesystem, so os.replace
        # is a true atomic rename rather than a copy.
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent),
                                   prefix=self.path.name, suffix=".tmp")
        try:
            try:
                f = os.fdopen(fd, "w", encoding="utf-8")
            except BaseException:
                # fdopen failed -> nothing owns the fd. Closing it here stops
                # the cleanup unlink below from hitting a Windows sharing
                # violation and masking the real exception.
                os.close(fd)
                raise
            with f:
                f.write(data)
                # flush+fsync BEFORE the rename: without them a system crash can
                # order the rename ahead of the data and leave a zero-length
                # file. load() tolerates that, so the symptom isn't a crash --
                # it's every printer silently vanishing and the user re-typing
                # every access code.
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            # BaseException, not Exception: cleanup must also run on Ctrl-C.
            pathlib.Path(tmp).unlink(missing_ok=True)
            raise


class MemoryStore:
    """Same interface, no disk. Used by --mock so fake printers never land in
    the user's real printers.json."""

    def __init__(self) -> None:
        self._configs: list[PrinterConfig] = []

    def load(self) -> list[PrinterConfig]:
        return list(self._configs)

    def save(self, configs: list[PrinterConfig]) -> None:
        self._configs = list(configs)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest server/tests/test_store.py -v`
Expected: 22 passed. The test list above is a FLOOR, not a ceiling — Task 1's
review found six defects that none of the originally-specified tests could
catch. Beyond them, also pin: the save() temp filename matches `printers.json*`
(spy on `tempfile.mkstemp`, compare with `fnmatch`); a save failure leaves no
leftover temp file (patch `os.replace` to raise); saving OVER an existing file;
falsy wrong-typed fields are skipped rather than coerced; `"capture": "false"`
does not enable capture; and a corrupt file actually logs a warning (`caplog`) —
the difference between a silent `[]` and a warned `[]` is whether the user can
diagnose their vanished printers.

- [ ] **Step 6: Commit**

```bash
git add .gitignore server/store.py server/tests/test_store.py
git commit -m "feat(server): printers.json persistence with atomic writes"
```

---

### Task 2: `server/sdcard.py` — FTPS microSD listing

MQTT cannot list files. The only way in is FTPS on port 990 with **implicit** TLS. The socket code cannot be unit-tested without hardware, so all the logic that *can* be tested (path guard, parsers, sorting) is pulled out into pure functions and tested exhaustively; the socket wrapper stays thin.

**Files:**
- Create: `server/sdcard.py`
- Test: `server/tests/test_sdcard.py`

- [ ] **Step 1: Write the failing tests**

Create `server/tests/test_sdcard.py`:

```python
import pytest

from server.sdcard import (SdError, normalize_path, parse_list_lines,
                           parse_mlsd, sort_entries)


# ---------- normalize_path ----------

def test_empty_path_is_root():
    assert normalize_path("") == "/"
    assert normalize_path(None) == "/"


def test_relative_path_becomes_absolute():
    assert normalize_path("timelapse") == "/timelapse"


def test_trailing_slash_removed():
    assert normalize_path("/timelapse/") == "/timelapse"


def test_backslashes_normalised():
    assert normalize_path("\\timelapse\\sub") == "/timelapse/sub"


def test_dotdot_is_rejected():
    for bad in ("/../etc", "/a/../../b", "..", "/timelapse/.."):
        with pytest.raises(SdError):
            normalize_path(bad)


def test_double_slash_collapsed():
    assert normalize_path("//a//b") == "/a/b"


# ---------- parse_mlsd ----------

def test_parse_mlsd_file_and_dir():
    pairs = [
        ("timelapse", {"type": "dir", "modify": "20260716120000"}),
        ("Benchy.3mf", {"type": "file", "size": "1234",
                        "modify": "20260716130500"}),
    ]
    got = parse_mlsd(pairs)
    assert got[0] == {"name": "timelapse", "is_dir": True, "size": None,
                      "mtime": "2026-07-16T12:00:00"}
    assert got[1] == {"name": "Benchy.3mf", "is_dir": False, "size": 1234,
                      "mtime": "2026-07-16T13:05:00"}


def test_parse_mlsd_skips_cdir_and_pdir():
    pairs = [(".", {"type": "cdir"}), ("..", {"type": "pdir"}),
             ("real", {"type": "file", "size": "1"})]
    assert [e["name"] for e in parse_mlsd(pairs)] == ["real"]


def test_parse_mlsd_tolerates_missing_facts():
    got = parse_mlsd([("x.gcode", {})])
    assert got == [{"name": "x.gcode", "is_dir": False, "size": None,
                    "mtime": None}]


def test_parse_mlsd_bad_size_is_none():
    got = parse_mlsd([("x", {"type": "file", "size": "not-a-number"})])
    assert got[0]["size"] is None


def test_parse_mlsd_bad_modify_is_none():
    got = parse_mlsd([("x", {"type": "file", "modify": "garbage"})])
    assert got[0]["mtime"] is None


# ---------- parse_list_lines ----------

def test_parse_list_unix_lines():
    lines = [
        "drwxr-xr-x    2 root     root         4096 Jul 16 12:00 timelapse",
        "-rw-r--r--    1 root     root      1048576 Jul 16 13:05 Benchy.3mf",
    ]
    got = parse_list_lines(lines)
    assert got[0] == {"name": "timelapse", "is_dir": True, "size": None,
                      "mtime": None}
    assert got[1] == {"name": "Benchy.3mf", "is_dir": False,
                      "size": 1048576, "mtime": None}


def test_parse_list_keeps_spaces_in_names():
    lines = ["-rw-r--r--    1 root root  12 Jul 16 13:05 my print v2.3mf"]
    assert parse_list_lines(lines)[0]["name"] == "my print v2.3mf"


def test_parse_list_skips_dot_entries_and_junk():
    lines = [
        "total 24",
        "drwxr-xr-x    2 root root 4096 Jul 16 12:00 .",
        "drwxr-xr-x    2 root root 4096 Jul 16 12:00 ..",
        "-rw-r--r--    1 root root   12 Jul 16 13:05 real.3mf",
        "this is not a listing line",
    ]
    assert [e["name"] for e in parse_list_lines(lines)] == ["real.3mf"]


def test_parse_list_year_form_date():
    lines = ["-rw-r--r--    1 root root   12 Jul 16  2025 old.3mf"]
    assert parse_list_lines(lines)[0]["name"] == "old.3mf"


# ---------- sort_entries ----------

def test_sort_puts_dirs_first_then_case_insensitive_name():
    entries = [
        {"name": "beta.3mf", "is_dir": False, "size": 1, "mtime": None},
        {"name": "Zulu", "is_dir": True, "size": None, "mtime": None},
        {"name": "alpha.3mf", "is_dir": False, "size": 1, "mtime": None},
        {"name": "acme", "is_dir": True, "size": None, "mtime": None},
    ]
    assert [e["name"] for e in sort_entries(entries)] == [
        "acme", "Zulu", "alpha.3mf", "beta.3mf"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest server/tests/test_sdcard.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'server.sdcard'`

- [ ] **Step 3: Implement `server/sdcard.py`**

```python
"""Read-only microSD listing over the printer's FTPS server.

The MQTT protocol exposes NO file listing -- the only way to read the card is
FTPS on port 990 with IMPLICIT TLS, using the same bblp + access-code
credentials as MQTT. See:
https://github.com/Doridian/OpenBambuAPI/blob/main/ftp.md

Two things here cost people days:

  * IMPLICIT vs EXPLICIT TLS. ftplib.FTP_TLS speaks explicit TLS: it connects
    in plaintext and issues AUTH TLS. Bambu's server expects TLS from the very
    first byte. Hence ImplicitFTP_TLS below, which wraps the socket the moment
    it is assigned rather than after a command.
  * The cert is self-signed and its common name is the SERIAL, not the IP, so
    verification is off -- same trust model as bambu_link.py.

Everything testable (path guard, parsers, sorting) is a pure function. The
socket path is thin on purpose: it cannot be unit-tested without hardware.
"""
from __future__ import annotations

import datetime as dt
import ftplib
import logging
import posixpath
import re
import ssl

log = logging.getLogger("server.sdcard")

FTPS_PORT = 990
FTP_USER = "bblp"
TIMEOUT_S = 10.0

# "-rw-r--r--  1 root root  1048576 Jul 16 13:05 Benchy.3mf"
#  type       links owner group size  month day time-or-year  name
_LIST_RE = re.compile(
    r"^([-dl])\S*\s+\d+\s+\S+\s+\S+\s+(\d+)\s+"
    r"\w{3}\s+\d{1,2}\s+(?:\d{4}|\d{1,2}:\d{2})\s+(.+?)\s*$")


class SdError(Exception):
    """Anything that stopped us listing the card. Message is user-facing, so
    it must never contain the access code."""


def normalize_path(path: str | None) -> str:
    """Absolute, slash-normalised, no traversal.

    `..` is rejected outright rather than collapsed. posixpath.normpath would
    silently clamp '/a/../../b' to '/b', which is safe but hides a caller bug;
    a read-only endpoint is still no excuse for accepting an escaping path.
    """
    raw = (path or "/").strip().replace("\\", "/")
    # Collapse repeats BEFORE normpath: POSIX says a leading "//" is
    # implementation-defined and posixpath.normpath deliberately preserves it,
    # so normpath("//a//b") is "//a/b", not "/a/b".
    raw = re.sub(r"/+", "/", raw)
    if not raw.startswith("/"):
        raw = "/" + raw
    if ".." in raw.split("/"):
        raise SdError("path may not contain '..'")
    return posixpath.normpath(raw)


def _int_or_none(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _mlsd_time(v) -> str | None:
    """MLSD 'modify' fact is YYYYMMDDHHMMSS -> ISO-8601."""
    try:
        return dt.datetime.strptime(str(v), "%Y%m%d%H%M%S").isoformat()
    except (TypeError, ValueError):
        return None


def sort_entries(entries: list[dict]) -> list[dict]:
    """Folders first, then case-insensitive by name."""
    return sorted(entries, key=lambda e: (not e["is_dir"], e["name"].lower()))


def parse_mlsd(pairs) -> list[dict]:
    """(name, facts) pairs from ftplib's mlsd() -> entry dicts."""
    out = []
    for name, facts in pairs:
        typ = (facts.get("type") or "").lower()
        if typ in ("cdir", "pdir") or name in (".", ".."):
            continue
        is_dir = typ == "dir"
        out.append({
            "name": name,
            "is_dir": is_dir,
            "size": None if is_dir else _int_or_none(facts.get("size")),
            "mtime": _mlsd_time(facts.get("modify")),
        })
    return sort_entries(out)


def parse_list_lines(lines) -> list[dict]:
    """Unix-style LIST output -> entry dicts.

    Fallback for servers without MLSD. mtime is None here on purpose: LIST
    omits the year for recent files ("Jul 16 13:05"), and guessing it would
    produce confidently wrong dates. Names and sizes are what was asked for.
    """
    out = []
    for line in lines:
        m = _LIST_RE.match(line.strip())
        if not m:
            continue  # "total 24" banners and anything else non-conforming
        kind, size, name = m.group(1), m.group(2), m.group(3)
        if name in (".", ".."):
            continue
        is_dir = kind == "d"
        out.append({"name": name, "is_dir": is_dir,
                    "size": None if is_dir else _int_or_none(size),
                    "mtime": None})
    return sort_entries(out)


def _ssl_context() -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False          # cert CN is the serial, not the IP
    ctx.verify_mode = ssl.CERT_NONE     # self-signed; same as bambu_link.py
    return ctx


class ImplicitFTP_TLS(ftplib.FTP_TLS):
    """FTP_TLS that negotiates TLS at connect time instead of after AUTH TLS,
    and reuses the control session on the data channel.

    ftplib has no implicit-TLS mode. The documented trick is to intercept the
    socket assignment and wrap it, which is what the `sock` property does.
    FTP_TLS.login() then skips its AUTH TLS step on its own, because it checks
    whether the socket is already an SSLSocket.
    """

    def __init__(self, *args, **kwargs):
        # BEFORE super().__init__: the base class assigns self.sock, which
        # lands in the setter below, which reads self._sock.
        self._sock = None
        super().__init__(*args, **kwargs)

    @property
    def sock(self):
        return self._sock

    @sock.setter
    def sock(self, value):
        if value is not None and not isinstance(value, ssl.SSLSocket):
            value = self.context.wrap_socket(value)
        self._sock = value

    def ntransfercmd(self, cmd, rest=None):
        """Wrap the data connection reusing the control connection's TLS
        session.

        Verified against this repo's Python 3.11.9: the stdlib's
        FTP_TLS.ntransfercmd calls wrap_socket WITHOUT `session=`, so every
        data transfer starts a brand-new TLS session. Servers configured to
        require session reuse (vsftpd's `require_ssl_reuse`, which several
        Bambu firmwares behave like) then accept the login and fail or hang the
        moment you LIST. Skipping straight to FTP.ntransfercmd and passing the
        session ourselves is the fix.
        """
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            conn = self.context.wrap_socket(conn, server_hostname=self.host,
                                            session=self.sock.session)
        return conn, size


def list_dir(host: str, access_code: str, path: str = "/") -> list[dict]:
    """List one directory on the card. Raises SdError on any failure.

    Opens a fresh connection per call: this is an on-demand endpoint, not a
    poller, and holding an FTP session open against a printer that may power
    off mid-print buys nothing.
    """
    target = normalize_path(path)
    ftp = ImplicitFTP_TLS(context=_ssl_context(), timeout=TIMEOUT_S)
    try:
        ftp.connect(host, FTPS_PORT)
        ftp.login(FTP_USER, access_code)
        ftp.prot_p()
        ftp.set_pasv(True)
        try:
            return parse_mlsd(list(ftp.mlsd(target)))
        except ftplib.error_perm:
            # Server has no MLSD (500/502). Fall back to LIST.
            lines: list[str] = []
            ftp.dir(target, lines.append)
            return parse_list_lines(lines)
    except ftplib.all_errors as e:
        # Never interpolate access_code into this message.
        raise SdError(f"Could not list {target} on {host}: {e}") from e
    finally:
        try:
            ftp.close()
        except Exception:  # noqa: BLE001 - close() must never mask the real error
            pass
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest server/tests/test_sdcard.py -v`
Expected: 16 passed

- [ ] **Step 5: Commit**

```bash
git add server/sdcard.py server/tests/test_sdcard.py
git commit -m "feat(server): read-only microSD listing over FTPS (implicit TLS)"
```

---

### Task 3: `build_summary()` gains printer identity + last_error

**Files:**
- Modify: `server/printer.py:39-68` (the `build_summary` function)
- Test: `server/tests/test_summary.py` (append)

- [ ] **Step 1: Add the failing tests**

Append to `server/tests/test_summary.py`:

```python
def test_identity_fields_default_empty():
    s = build_summary({}, None, False, "192.0.2.1")
    assert s["serial"] == ""
    assert s["name"] == ""
    assert s["capture"] is False
    assert s["last_error"] is None


def test_identity_fields_passed_through():
    s = build_summary({}, 1.0, True, "192.168.137.2",
                      serial="0300CA633005010", name="A1-bench",
                      capture=True, last_error="Unreachable")
    assert s["serial"] == "0300CA633005010"
    assert s["name"] == "A1-bench"
    assert s["capture"] is True
    assert s["last_error"] == "Unreachable"


def test_summary_never_contains_access_code():
    s = build_summary({"gcode_state": "RUNNING"}, 1.0, True, "192.168.137.2",
                      serial="S1", name="n", capture=False)
    assert "access_code" not in s
    assert "31661007" not in repr(s)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest server/tests/test_summary.py -v`
Expected: FAIL — `KeyError: 'serial'` on `test_identity_fields_default_empty`, and `TypeError: build_summary() got an unexpected keyword argument 'serial'` on the other two.

- [ ] **Step 3: Update `build_summary` in `server/printer.py`**

Replace the whole `build_summary` function (currently `server/printer.py:39-68`) with:

```python
def build_summary(state: dict, report_age: float | None,
                  connected: bool, printer: str, *,
                  serial: str = "", name: str = "", capture: bool = False,
                  last_error: str | None = None) -> dict:
    """Curate the merged printer state into the payload the UI consumes.

    Fields the printer hasn't reported yet are null — it sends partial
    updates, so early in a session most fields are unknown.

    Identity is keyword-only so the v1 positional call signature still reads
    the same. `access_code` is deliberately NOT a parameter: nothing about the
    password should be able to reach a payload by accident.
    """
    out = {k: state.get(k) for k in SUMMARY_FIELDS}
    hms_codes = []
    for h in state.get("hms") or []:
        # hms comes from printer-controlled MQTT JSON; one malformed entry
        # must not take down every future summary() call.
        if not isinstance(h, dict):
            continue
        try:
            hms_codes.append(decode_hms(int(h.get("attr", 0)),
                                        int(h.get("code", 0))))
        except (TypeError, ValueError):
            continue
    out["hms"] = hms_codes
    if not connected:
        conn = "disconnected"
    elif report_age is None or report_age > STALE_S:
        conn = "stale"
    else:
        conn = "ok"
    out["connection"] = conn
    out["report_age_s"] = None if report_age is None else round(report_age, 1)
    out["printer"] = printer
    out["serial"] = serial
    out["name"] = name
    out["capture"] = capture
    out["last_error"] = last_error
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest server/tests/test_summary.py -v`
Expected: 7 passed (4 pre-existing + 3 new)

- [ ] **Step 5: Commit**

```bash
git add server/printer.py server/tests/test_summary.py
git commit -m "feat(server): summary payload carries printer identity and last_error"
```

---

### Task 4: `PrinterService` + `MockPrinter` gain identity, `last_error`, `list_files()`

`_connect_loop` already distinguishes "unreachable" from "reached but no CONNACK" and throws the distinction away in a log line. Capture it — per `CONNECTION.md`, a wrong access code is the single most common failure, and it is indistinguishable from "printer off" unless we record which branch fired.

`MockPrinter` gains a `mode` so `--mock` can seed one running, one stale, and one offline printer.

**Files:**
- Modify: `server/printer.py` (both classes)
- Test: `server/tests/test_services.py` (rewrite)

- [ ] **Step 1: Rewrite `server/tests/test_services.py`**

```python
import pytest

from server.printer import STALE_S, MockPrinter, PrinterService
from server.sdcard import SdError


# ---------- PrinterService ----------

def svc(**kw):
    # 192.0.2.1 is TEST-NET; constructing does NOT open a socket.
    kw.setdefault("host", "192.0.2.1")
    kw.setdefault("serial", "0309TESTSERIAL")
    kw.setdefault("access_code", "12345678")
    return PrinterService(**kw)


def test_service_summary_before_connect():
    s = svc().summary()
    assert s["connection"] == "disconnected"
    assert s["printer"] == "192.0.2.1"
    assert s["serial"] == "0309TESTSERIAL"
    assert s["report_age_s"] is None


def test_service_name_defaults_to_host():
    assert svc().summary()["name"] == "192.0.2.1"


def test_service_name_used_when_given():
    assert svc(name="A1-bench").summary()["name"] == "A1-bench"


def test_service_capture_flag_in_summary():
    assert svc(capture=True).summary()["capture"] is True


def test_service_summary_never_leaks_access_code():
    s = svc(access_code="31661007").summary()
    assert "31661007" not in repr(s)
    assert "access_code" not in s


def test_service_last_error_none_before_any_attempt():
    assert svc().summary()["last_error"] is None


def test_unreachable_error_message_when_connect_raises(monkeypatch):
    s = svc()

    def boom(timeout=5):
        raise OSError("boom")

    monkeypatch.setattr(s.link, "connect", boom)
    # The retry backoff must actually SET the stop event, not merely return
    # True: _connect_loop's `while not self._stop.is_set()` ignores wait()'s
    # return value, so a lambda returning True would spin forever.
    monkeypatch.setattr(s._stop, "wait", lambda t: s._stop.set())
    s._connect_loop()
    assert "Unreachable" in s.summary()["last_error"]


def test_no_connack_error_message_when_connect_returns_false(monkeypatch):
    s = svc()
    monkeypatch.setattr(s.link, "connect", lambda timeout=5: False)
    s._connect_loop()
    err = s.summary()["last_error"]
    assert "access code" in err
    assert "Developer Mode" in err


def test_last_error_cleared_on_successful_connect(monkeypatch):
    s = svc()
    s._last_error = "stale error from an earlier attempt"
    monkeypatch.setattr(s.link, "connect", lambda timeout=5: True)
    s._connect_loop()
    assert s._last_error is None


# ---------- MockPrinter ----------

def test_mock_frame_shape(tmp_path):
    assert MockPrinter(tmp_path)._frame(5).shape == (480, 640, 3)


def test_mock_touch_updates_summary(tmp_path):
    mp = MockPrinter(tmp_path)
    assert mp.summary()["connection"] == "stale"  # no report yet
    mp._touch({"gcode_state": "RUNNING", "layer_num": 2})
    s = mp.summary()
    assert s["layer_num"] == 2
    assert s["gcode_state"] == "RUNNING"
    assert s["connection"] == "ok"
    assert s["printer"] == "MOCK"


def test_mock_offline_mode_is_disconnected(tmp_path):
    mp = MockPrinter(tmp_path, mode="offline")
    mp.start()
    s = mp.summary()
    assert s["connection"] == "disconnected"
    assert "Unreachable" in s["last_error"]


def test_mock_stale_mode_reports_stale(tmp_path):
    mp = MockPrinter(tmp_path, mode="stale")
    mp.start()
    s = mp.summary()
    assert s["connection"] == "stale"
    assert s["report_age_s"] > STALE_S


def test_mock_identity_in_summary(tmp_path):
    mp = MockPrinter(tmp_path, serial="MOCK1", host="mock-bench",
                     name="bench", capture=True)
    s = mp.summary()
    assert s["serial"] == "MOCK1"
    assert s["name"] == "bench"
    assert s["capture"] is True


def test_mock_lists_root(tmp_path):
    names = [e["name"] for e in MockPrinter(tmp_path).list_files("/")]
    assert "timelapse" in names
    assert "Benchy.3mf" in names


def test_mock_lists_subdir(tmp_path):
    entries = MockPrinter(tmp_path).list_files("/timelapse")
    assert all(e["is_dir"] is False for e in entries)
    assert len(entries) >= 1


def test_mock_unknown_dir_raises_sderror(tmp_path):
    with pytest.raises(SdError):
        MockPrinter(tmp_path).list_files("/nope")


def test_mock_list_rejects_traversal(tmp_path):
    with pytest.raises(SdError):
        MockPrinter(tmp_path).list_files("/../etc")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest server/tests/test_services.py -v`
Expected: FAIL — `TypeError: PrinterService.__init__() got an unexpected keyword argument 'name'` and `AttributeError: 'MockPrinter' object has no attribute 'list_files'`.

- [ ] **Step 3: Add the import and constants to `server/printer.py`**

Change the import block near the top of `server/printer.py` — after the existing `from bambu_link import BambuLink, decode_hms  # noqa: E402` line, add:

```python
from . import sdcard  # noqa: E402
from .sdcard import SdError  # noqa: E402
```

Then add these constants directly below the existing `SUMMARY_FIELDS` tuple:

```python
ERR_UNREACHABLE = ("Unreachable — check the IP, and that LAN-only Mode is on")
ERR_NO_CONNACK = ("No response — the access code may be wrong (it rotates on "
                  "firmware updates), or Developer Mode is off")

# What --mock pretends is on the card. Mirrors the real layout: model files at
# the root, timelapse/ and cache/ subdirectories.
MOCK_TREE: dict[str, list[dict]] = {
    "/": [
        {"name": "timelapse", "is_dir": True, "size": None, "mtime": None},
        {"name": "cache", "is_dir": True, "size": None, "mtime": None},
        {"name": "Benchy.3mf", "is_dir": False, "size": 1048576,
         "mtime": "2026-07-16T13:05:00"},
        {"name": "calibration_cube.gcode.3mf", "is_dir": False, "size": 204800,
         "mtime": "2026-07-15T09:12:00"},
    ],
    "/timelapse": [
        {"name": "video_2026-07-16.mp4", "is_dir": False, "size": 8388608,
         "mtime": "2026-07-16T14:00:00"},
    ],
    "/cache": [
        {"name": "Benchy.gcode.3mf", "is_dir": False, "size": 1048576,
         "mtime": "2026-07-16T13:04:00"},
    ],
}
```

- [ ] **Step 4: Replace `PrinterService` in `server/printer.py`**

Replace the entire `PrinterService` class with:

```python
class PrinterService:
    """Real printer: owns a BambuLink, retries MQTT in the background.

    Startup must not die if the printer is off — we start disconnected and
    keep retrying every RETRY_S. Once paho has connected ONCE, its network
    loop auto-reconnects on drops, so we only drive the initial connect.
    """

    def __init__(self, host: str, serial: str, access_code: str,
                 name: str = "", capture: bool = False):
        self.host = host
        self.serial = serial
        self.access_code = access_code
        self.name = name or host
        self.capture = capture
        self.link = BambuLink(host, serial, access_code,
                              on_state=self._on_state)
        self._last_report: float | None = None
        self._last_error: str | None = None
        self._snapshot: dict = {}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._connect_loop,
                                        daemon=True)

    def _on_state(self, state: dict, patch: dict) -> None:
        # `state` is the deep-copied snapshot BambuLink built under its lock;
        # keeping it (instead of re-reading link.state later) means summary()
        # never races the MQTT thread's deep_merge writes.
        self._snapshot = state
        self._last_report = time.time()

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.link.disconnect()
        if self._thread.is_alive():
            self._thread.join(timeout=2)

    def _connect_loop(self) -> None:
        # BambuLink.connect() raising means paho's network loop never
        # started -> retrying is ours to do. connect() returning False means
        # loop_start() ran and paho now retries forever on its own;
        # re-driving connect() from this thread would race paho's thread on
        # the same socket, so we log and hand off.
        #
        # Which of those two happened is the ONLY signal distinguishing a bad
        # access code from an unplugged printer, so it is recorded rather than
        # only logged.
        while not self._stop.is_set():
            try:
                if self.link.connect(timeout=5):
                    self._last_error = None
                    return
                self._last_error = ERR_NO_CONNACK
                log.warning(
                    "MQTT reached %s but no CONNACK within 5s (wrong access "
                    "code, or Developer Mode off?). paho keeps retrying in "
                    "the background.", self.host)
                return
            except Exception as e:
                self._last_error = ERR_UNREACHABLE
                log.warning("MQTT connect to %s failed: %s (retry in %ss)",
                            self.host, e, RETRY_S)
            self._stop.wait(RETRY_S)

    def list_files(self, path: str = "/") -> list[dict]:
        """Blocking FTPS call. MUST NOT be called from the event loop — the
        route that uses it is a sync def, so FastAPI runs it on a threadpool."""
        return sdcard.list_dir(self.host, self.access_code, path)

    def summary(self) -> dict:
        age = (None if self._last_report is None
               else time.time() - self._last_report)
        connected = self.link.connected.is_set()
        return build_summary(
            self._snapshot, age, connected, self.host,
            serial=self.serial, name=self.name, capture=self.capture,
            last_error=None if connected else self._last_error)
```

- [ ] **Step 5: Update `MockPrinter` in `server/printer.py`**

Replace `MockPrinter.__init__`, `start`, `summary` and add `list_files` (keep `stop`, `_touch`, `_frame`, `_loop` exactly as they are). The class docstring gains the mode explanation:

```python
class MockPrinter:
    """Endless fake print for developing the GUI with no hardware.

    mode="running": RUNNING (one layer every LAYER_PERIOD_S, temps wander, an
    HMS code appears during HMS_LAYERS) -> FINISH -> IDLE_S -> new run. Frames
    are written as real JPEGs into a real run directory so /api/frame/latest is
    exercised too.
    mode="stale":   reports once, long ago, then never again -> "stale".
    mode="offline": never connects -> "disconnected" + last_error.

    The three modes exist so --mock can seed an Overview grid that actually
    shows all three states.
    """

    LAYERS = 30
    LAYER_PERIOD_S = 2.0
    IDLE_S = 10.0
    HMS_LAYERS = range(12, 17)

    def __init__(self, runs_dir: pathlib.Path, serial: str = "MOCK0000000000",
                 host: str = "MOCK", name: str = "", capture: bool = False,
                 mode: str = "running"):
        self.runs_dir = runs_dir
        self.serial = serial
        self.host = host
        self.name = name or host
        self.capture = capture
        self.mode = mode
        self.state: dict = {"gcode_state": "IDLE"}
        self._last_report: float | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self) -> None:
        if self.mode == "running":
            self._thread.start()
        elif self.mode == "stale":
            self._touch({"gcode_state": "RUNNING", "subtask_name": "stalled",
                         "layer_num": 7, "total_layer_num": 30,
                         "mc_percent": 23})
            # Backdate the report so it reads as stale immediately.
            self._last_report = time.time() - (STALE_S + 5)
        # "offline": never connects, nothing to start.

    def summary(self) -> dict:
        age = (None if self._last_report is None
               else time.time() - self._last_report)
        connected = self.mode != "offline"
        return build_summary(
            self.state, age, connected, self.host,
            serial=self.serial, name=self.name, capture=self.capture,
            last_error=None if connected else ERR_UNREACHABLE)

    def list_files(self, path: str = "/") -> list[dict]:
        target = sdcard.normalize_path(path)  # raises SdError on traversal
        try:
            return sdcard.sort_entries(list(MOCK_TREE[target]))
        except KeyError:
            raise SdError(f"Could not list {target} on {self.host}: "
                          "no such directory") from None
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest server/tests/test_services.py -v`
Expected: 18 passed

- [ ] **Step 7: Run the whole suite so far**

Run: `pytest server/tests -v`
Expected: all pass except `test_api.py`, which still calls `create_app(FakeService(...), ...)` — it is rewritten in Task 6. If `test_api.py` fails here, that is expected; do not fix it yet.

- [ ] **Step 8: Commit**

```bash
git add server/printer.py server/tests/test_services.py
git commit -m "feat(server): printer identity, last_error diagnosis, SD listing per service"
```

---

### Task 5: `server/registry.py` — the printer registry

**Files:**
- Create: `server/registry.py`
- Test: `server/tests/test_registry.py`

- [ ] **Step 1: Write the failing tests**

Create `server/tests/test_registry.py`:

```python
import pytest

from server.registry import DuplicateSerial, PrinterRegistry
from server.store import MemoryStore, PrinterConfig


class FakeService:
    """Stands in for PrinterService/MockPrinter: no sockets, no threads."""

    def __init__(self, cfg: PrinterConfig):
        self.cfg = cfg
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def summary(self):
        return {"serial": self.cfg.serial, "name": self.cfg.name,
                "printer": self.cfg.host, "capture": self.cfg.capture,
                "connection": "ok"}


def reg(store=None):
    return PrinterRegistry(store or MemoryStore(), FakeService)


def test_starts_empty():
    assert reg().summaries() == []


def test_add_starts_and_returns_summary():
    r = reg()
    s = r.add(host="1.2.3.4", serial="S1", access_code="code")
    assert s["serial"] == "S1"
    assert r.get("S1").started is True


def test_add_persists_to_store():
    store = MemoryStore()
    reg(store).add(host="1.2.3.4", serial="S1", access_code="code")
    saved = store.load()
    assert [c.serial for c in saved] == ["S1"]
    assert saved[0].access_code == "code"


def test_duplicate_serial_rejected():
    r = reg()
    r.add(host="1.2.3.4", serial="S1", access_code="code")
    with pytest.raises(DuplicateSerial):
        r.add(host="5.6.7.8", serial="S1", access_code="other")


def test_missing_fields_rejected():
    r = reg()
    for kw in ({"host": "", "serial": "S", "access_code": "c"},
               {"host": "h", "serial": "", "access_code": "c"},
               {"host": "h", "serial": "S", "access_code": ""}):
        with pytest.raises(ValueError):
            r.add(**kw)


def test_remove_stops_service_and_persists():
    store = MemoryStore()
    r = reg(store)
    r.add(host="1.2.3.4", serial="S1", access_code="code")
    svc = r.get("S1")
    assert r.remove("S1") is True
    assert svc.stopped is True
    assert store.load() == []
    assert r.get("S1") is None


def test_remove_unknown_is_false():
    assert reg().remove("nope") is False


def test_summaries_keep_registration_order():
    r = reg()
    for i in (1, 2, 3):
        r.add(host=f"10.0.0.{i}", serial=f"S{i}", access_code="c")
    assert [s["serial"] for s in r.summaries()] == ["S1", "S2", "S3"]


def test_capture_is_single_occupancy():
    r = reg()
    r.add(host="1.1.1.1", serial="S1", access_code="c", capture=True)
    r.add(host="2.2.2.2", serial="S2", access_code="c", capture=True)
    caps = {s["serial"]: s["capture"] for s in r.summaries()}
    assert caps == {"S1": False, "S2": True}


def test_capture_cleared_in_store_too():
    store = MemoryStore()
    r = reg(store)
    r.add(host="1.1.1.1", serial="S1", access_code="c", capture=True)
    r.add(host="2.2.2.2", serial="S2", access_code="c", capture=True)
    assert {c.serial: c.capture for c in store.load()} == {"S1": False,
                                                           "S2": True}


def test_load_restores_and_starts_from_store():
    store = MemoryStore()
    store.save([PrinterConfig(serial="S1", host="1.2.3.4", access_code="c",
                              name="bench", capture=True)])
    r = reg(store)
    r.load()
    assert [s["serial"] for s in r.summaries()] == ["S1"]
    assert r.get("S1").started is True


def test_summaries_never_contain_access_code():
    r = reg()
    r.add(host="1.2.3.4", serial="S1", access_code="31661007")
    assert "31661007" not in repr(r.summaries())


def test_stop_all_stops_every_service():
    r = reg()
    r.add(host="1.1.1.1", serial="S1", access_code="c")
    r.add(host="2.2.2.2", serial="S2", access_code="c")
    services = [r.get("S1"), r.get("S2")]
    r.stop_all()
    assert all(s.stopped for s in services)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest server/tests/test_registry.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'server.registry'`

- [ ] **Step 3: Implement `server/registry.py`**

```python
"""The live set of printers, keyed by serial.

Replaces v1's single PrinterService. Holds the config alongside the service
rather than reading fields back off the service, so persistence never depends
on what a given service class happens to expose -- MockPrinter has no
access_code, and asking it for one would be a bug waiting to happen.

Ordering is registration order and nothing else: a grid that reshuffles itself
as printers change state is unusable.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Callable

from .store import PrinterConfig

log = logging.getLogger("server.registry")


class DuplicateSerial(Exception):
    """That serial is already registered."""


class PrinterRegistry:
    """Owns one service per printer.

    `service_factory(cfg) -> service` builds the thing that talks to a printer.
    Injecting it is what lets --mock seed fake printers and lets the tests run
    with no sockets at all.

    A service must provide: start(), stop(), summary() -> dict,
    list_files(path) -> list[dict].
    """

    def __init__(self, store, service_factory: Callable[[PrinterConfig], Any]):
        self._store = store
        self._factory = service_factory
        # dicts preserve insertion order == registration order
        self._configs: dict[str, PrinterConfig] = {}
        self._services: dict[str, Any] = {}
        self._lock = threading.Lock()

    # ---------------- lifecycle ----------------

    def load(self) -> None:
        """Restore and start everything in the store. Called once at startup."""
        for cfg in self._store.load():
            with self._lock:
                if cfg.serial in self._services:
                    continue
                self._insert(cfg)
        log.info("restored %d printer(s)", len(self._services))

    def stop_all(self) -> None:
        for svc in list(self._services.values()):
            try:
                svc.stop()
            except Exception as e:  # noqa: BLE001 - one bad stop must not
                log.warning("error stopping %s: %s", svc, e)  # skip the rest

    # ---------------- mutation ----------------

    def add(self, host: str, serial: str, access_code: str,
            name: str = "", capture: bool = False) -> dict:
        cfg = PrinterConfig(serial=serial, host=host, access_code=access_code,
                            name=name, capture=capture)
        if not (cfg.serial and cfg.host and cfg.access_code):
            raise ValueError("host, serial and access_code are all required")
        with self._lock:
            if cfg.serial in self._services:
                raise DuplicateSerial(cfg.serial)
            if cfg.capture:
                self._clear_capture()
            svc = self._insert(cfg)
        self._persist()
        return svc.summary()

    def remove(self, serial: str) -> bool:
        with self._lock:
            cfg = self._configs.pop(serial, None)
            svc = self._services.pop(serial, None)
        if svc is None and cfg is None:
            return False
        if svc is not None:
            # Stop before persisting: a half-removed printer that keeps
            # reconnecting is worse than a slow DELETE.
            svc.stop()
        self._persist()
        return True

    # ---------------- reads ----------------

    def get(self, serial: str):
        return self._services.get(serial)

    def summaries(self) -> list[dict]:
        """Must stay non-blocking: this runs on the event loop for every
        WebSocket tick (see server/main.py)."""
        return [svc.summary() for svc in list(self._services.values())]

    # ---------------- internals (call with the lock held) ----------------

    def _insert(self, cfg: PrinterConfig):
        svc = self._factory(cfg)
        self._configs[cfg.serial] = cfg
        self._services[cfg.serial] = svc
        svc.start()
        return svc

    def _clear_capture(self) -> None:
        """One webcam -> at most one capture printer."""
        for serial, cfg in self._configs.items():
            if cfg.capture:
                cfg.capture = False
                svc = self._services.get(serial)
                if svc is not None:
                    svc.capture = False

    def _persist(self) -> None:
        self._store.save(list(self._configs.values()))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest server/tests/test_registry.py -v`
Expected: 13 passed

- [ ] **Step 5: Commit**

```bash
git add server/registry.py server/tests/test_registry.py
git commit -m "feat(server): printer registry with persistence and single-capture rule"
```

---

### Task 6: `server/main.py` — registry-backed API

**Files:**
- Modify: `server/main.py` (rewrite `create_app`; `/api/frame/latest` and the static mount are unchanged)
- Test: `server/tests/test_api.py` (rewrite)

- [ ] **Step 1: Rewrite `server/tests/test_api.py`**

```python
from fastapi.testclient import TestClient

from server.main import create_app
from server.registry import DuplicateSerial
from server.sdcard import SdError


class FakeService:
    def __init__(self, serial="S1", entries=None, error=None):
        self.serial = serial
        self.capture = False
        self._entries = entries if entries is not None else [
            {"name": "timelapse", "is_dir": True, "size": None, "mtime": None},
            {"name": "Benchy.3mf", "is_dir": False, "size": 12,
             "mtime": "2026-07-16T13:05:00"},
        ]
        self._error = error

    def summary(self):
        return {"serial": self.serial, "gcode_state": "IDLE",
                "connection": "ok", "report_age_s": 1.0}

    def list_files(self, path="/"):
        if self._error:
            raise SdError(self._error)
        return self._entries


class FakeRegistry:
    def __init__(self, services=None, duplicate=False):
        self._services = {s.serial: s for s in (services or [])}
        self.duplicate = duplicate
        self.added = []
        self.removed = []

    def summaries(self):
        return [s.summary() for s in self._services.values()]

    def get(self, serial):
        return self._services.get(serial)

    def add(self, host, serial, access_code, name="", capture=False):
        if self.duplicate:
            raise DuplicateSerial(serial)
        if not (host and serial and access_code):
            raise ValueError("host, serial and access_code are all required")
        svc = FakeService(serial)
        self._services[serial] = svc
        self.added.append((host, serial, access_code, name, capture))
        return svc.summary()

    def remove(self, serial):
        if serial not in self._services:
            return False
        del self._services[serial]
        self.removed.append(serial)
        return True


def client(tmp_path, registry=None):
    return TestClient(create_app(registry or FakeRegistry([FakeService()]),
                                 tmp_path))


def make_frame(runs_dir, run="20260716T000000_x", layer=7):
    frames = runs_dir / run / "frames"
    frames.mkdir(parents=True, exist_ok=True)
    (frames / f"layer_{layer:04d}.jpg").write_bytes(b"\xff\xd8\xff\xe0fake")


# ---------- GET /api/printers ----------

def test_list_printers_envelope(tmp_path):
    r = client(tmp_path).get("/api/printers")
    assert r.status_code == 200
    assert r.json() == {"printers": [{"serial": "S1", "gcode_state": "IDLE",
                                      "connection": "ok",
                                      "report_age_s": 1.0}]}


def test_list_printers_empty(tmp_path):
    r = client(tmp_path, FakeRegistry([])).get("/api/printers")
    assert r.json() == {"printers": []}


def test_status_route_is_gone(tmp_path):
    assert client(tmp_path).get("/api/status").status_code == 404


# ---------- POST /api/printers ----------

def test_add_printer_201(tmp_path):
    reg = FakeRegistry([])
    r = client(tmp_path, reg).post("/api/printers", json={
        "host": "192.168.137.2", "serial": "S9",
        "access_code": "31661007", "name": "bench", "capture": True})
    assert r.status_code == 201
    assert r.json()["serial"] == "S9"
    assert reg.added == [("192.168.137.2", "S9", "31661007", "bench", True)]


def test_add_printer_duplicate_409(tmp_path):
    reg = FakeRegistry([], duplicate=True)
    r = client(tmp_path, reg).post("/api/printers", json={
        "host": "1.2.3.4", "serial": "S1", "access_code": "c"})
    assert r.status_code == 409
    assert "already registered" in r.json()["detail"]


def test_add_printer_empty_field_400(tmp_path):
    r = client(tmp_path, FakeRegistry([])).post("/api/printers", json={
        "host": "", "serial": "S1", "access_code": "c"})
    assert r.status_code == 400


def test_add_printer_missing_field_422(tmp_path):
    r = client(tmp_path, FakeRegistry([])).post("/api/printers",
                                                json={"host": "1.2.3.4"})
    assert r.status_code == 422  # pydantic rejects it before the route runs


# ---------- DELETE /api/printers/{serial} ----------

def test_remove_printer_204(tmp_path):
    reg = FakeRegistry([FakeService("S1")])
    r = client(tmp_path, reg).delete("/api/printers/S1")
    assert r.status_code == 204
    assert reg.removed == ["S1"]


def test_remove_unknown_printer_404(tmp_path):
    r = client(tmp_path, FakeRegistry([])).delete("/api/printers/nope")
    assert r.status_code == 404


# ---------- GET /api/printers/{serial}/files ----------

def test_list_files_default_root(tmp_path):
    r = client(tmp_path).get("/api/printers/S1/files")
    assert r.status_code == 200
    body = r.json()
    assert body["path"] == "/"
    assert [e["name"] for e in body["entries"]] == ["timelapse", "Benchy.3mf"]


def test_list_files_normalises_path(tmp_path):
    r = client(tmp_path).get("/api/printers/S1/files", params={
        "path": "/timelapse/"})
    assert r.json()["path"] == "/timelapse"


def test_list_files_unknown_printer_404(tmp_path):
    r = client(tmp_path).get("/api/printers/nope/files")
    assert r.status_code == 404


def test_list_files_traversal_400(tmp_path):
    r = client(tmp_path).get("/api/printers/S1/files", params={
        "path": "/../etc"})
    assert r.status_code == 400
    assert ".." in r.json()["detail"]


def test_list_files_ftps_failure_502(tmp_path):
    reg = FakeRegistry([FakeService("S1", error="Could not list / on host")])
    r = client(tmp_path, reg).get("/api/printers/S1/files")
    assert r.status_code == 502
    assert "Could not list" in r.json()["detail"]


# ---------- unchanged v1 routes ----------

def test_frame_404_when_no_run(tmp_path):
    r = client(tmp_path).get("/api/frame/latest")
    assert r.status_code == 404
    assert r.json() == {"error": "no active run"}


def test_frame_served_with_headers(tmp_path):
    make_frame(tmp_path, layer=7)
    r = client(tmp_path).get("/api/frame/latest")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"
    assert r.headers["x-frame-layer"] == "7"
    assert r.headers["x-frame-run"] == "20260716T000000_x"
    assert r.headers["cache-control"] == "no-store"


def test_ws_sends_envelope_immediately(tmp_path):
    with client(tmp_path).websocket_connect("/ws") as ws:
        msg = ws.receive_json()
        assert msg["printers"][0]["gcode_state"] == "IDLE"


def test_root_hint_when_no_dist(tmp_path):
    r = client(tmp_path).get("/")
    assert r.status_code == 200
    assert "npm run build" in r.text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest server/tests/test_api.py -v`
Expected: FAIL — `create_app` still expects a service with `.summary()` only; `/api/printers` is 404.

- [ ] **Step 3: Rewrite `server/main.py`**

```python
"""FastAPI app: /api/printers, /api/frame/latest, /ws, static frontend."""
from __future__ import annotations

import asyncio
import json
import logging
import pathlib
import time

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import runs, sdcard
from .registry import DuplicateSerial
from .sdcard import SdError

log = logging.getLogger("server.main")

WS_POLL_S = 0.25      # summary sampled at 4 Hz -> at most ~4 pushes/s
WS_HEARTBEAT_S = 5.0  # push even when unchanged, keeps report_age_s fresh


class AddPrinter(BaseModel):
    """Pydantic rejects non-strings at the body-parse layer -> 422.

    That matters: PrinterConfig's type validation lives in from_dict(), NOT in
    its constructor, so `PrinterConfig(serial=None, ...)` still coerces to "".
    The registry keys on serial, so a None reaching it would collapse printers
    onto one entry. This model is what keeps a request body off that path --
    do not bypass it by building a PrinterConfig straight from raw request data.
    """

    host: str
    serial: str
    access_code: str
    name: str = ""
    capture: bool = False


def _comparable(printers: list[dict]) -> list[dict]:
    """report_age_s ticks every sample; ignore it when deciding whether the
    state meaningfully changed."""
    return [{k: v for k, v in p.items() if k != "report_age_s"}
            for p in printers]


def create_app(registry, runs_dir: pathlib.Path,
               frontend_dist: pathlib.Path | None = None) -> FastAPI:
    """`registry` is anything with summaries() -> list[dict], get(serial),
    add(...), remove(serial) (PrinterRegistry, or a test fake)."""
    app = FastAPI(title="bambu-monitor")

    @app.get("/api/printers")
    def list_printers():
        return {"printers": registry.summaries()}

    @app.post("/api/printers", status_code=201)
    def add_printer(body: AddPrinter):
        try:
            return registry.add(host=body.host, serial=body.serial,
                                access_code=body.access_code,
                                name=body.name, capture=body.capture)
        except DuplicateSerial:
            raise HTTPException(409, "that serial is already registered")
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.delete("/api/printers/{serial}", status_code=204)
    def remove_printer(serial: str):
        if not registry.remove(serial):
            raise HTTPException(404, "unknown printer")
        return Response(status_code=204)

    @app.get("/api/printers/{serial}/files")
    def list_files(serial: str, path: str = "/"):
        # Deliberately a SYNC def: FastAPI runs these on a threadpool, so the
        # blocking FTPS handshake cannot stall the event loop and freeze every
        # connected WebSocket.
        svc = registry.get(serial)
        if svc is None:
            raise HTTPException(404, "unknown printer")
        try:
            target = sdcard.normalize_path(path)
        except SdError as e:
            raise HTTPException(400, str(e))  # bad input, not a printer fault
        try:
            return {"path": target, "entries": svc.list_files(target)}
        except SdError as e:
            raise HTTPException(502, str(e))  # the printer/FTPS failed us

    @app.get("/api/frame/latest")
    def frame_latest():
        info = runs.newest_frame(runs_dir)
        if info is None:
            return JSONResponse({"error": "no active run"}, status_code=404)
        try:
            # Read in-handler (FastAPI runs sync routes in a threadpool) so a
            # frame vanishing between discovery and send stays a clean 404
            # instead of FileResponse's late FileNotFoundError -> 500.
            # capture.py writes non-atomically, so a rare truncated JPEG is
            # possible and accepted; the frontend re-polls within 2 s.
            data = info["path"].read_bytes()
        except OSError:
            return JSONResponse({"error": "no active run"}, status_code=404)
        return Response(
            content=data, media_type="image/jpeg",
            headers={"X-Frame-Layer": str(info["layer"]),
                     "X-Frame-Run": info["run"],
                     "Cache-Control": "no-store"})

    @app.websocket("/ws")
    async def ws(sock: WebSocket):
        try:
            await sock.accept()
            printers = registry.summaries()
            await sock.send_text(json.dumps({"printers": printers}))
            last_sent, last_time = printers, time.monotonic()
            while True:
                await asyncio.sleep(WS_POLL_S)
                now = time.monotonic()
                # summaries() must stay non-blocking: it runs on the event loop
                # and a stall here would freeze every connected client.
                printers = registry.summaries()
                changed = _comparable(printers) != _comparable(last_sent)
                if changed or now - last_time >= WS_HEARTBEAT_S:
                    await sock.send_text(json.dumps({"printers": printers}))
                    last_sent, last_time = printers, now
        except WebSocketDisconnect:
            pass

    if frontend_dist is not None and (frontend_dist / "index.html").exists():
        app.mount("/", StaticFiles(directory=frontend_dist, html=True),
                  name="static")
    else:
        @app.get("/")
        def hint():
            return PlainTextResponse(
                "bambu-monitor server is running.\n"
                "Frontend not built yet: run `npm run build` in frontend/,\n"
                "or use the Vite dev server (`npm run dev`) on port 5173.\n")

    return app
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest server/tests/test_api.py -v`
Expected: 18 passed

- [ ] **Step 5: Run the whole suite**

Run: `pytest server/tests -v`
Expected: ~100 passed — 6 runs + 22 store + 16 sdcard + 7 summary + 18 services + 13 registry + 18 api. Zero failures is the bar; if the total differs because you added a case, that is fine, but a *failure* is not.

- [ ] **Step 6: Commit**

```bash
git add server/main.py server/tests/test_api.py
git commit -m "feat(server): registry-backed API with printer CRUD and SD listing"
```

---

### Task 7: `server/__main__.py` — CLI, mock seeding

**Files:**
- Modify: `server/__main__.py` (rewrite)

- [ ] **Step 1: Rewrite `server/__main__.py`**

```python
"""CLI entry: python -m server [--mock] [--printers-file printers.json]

Printers are no longer configured on the command line -- they are added in the
browser and restored from printers.json. The server starts fine with none
registered; the UI shows the add form.
"""
from __future__ import annotations

import argparse
import logging
import pathlib
import signal

import uvicorn

from .main import create_app
from .printer import MockPrinter, PrinterService
from .registry import PrinterRegistry
from .store import MemoryStore, PrinterStore

# serial, name, mode, capture -- one of each state so the Overview grid is
# fully exercisable with no hardware.
MOCK_SEED = [
    ("MOCK0000000001", "mock-bench", "running", True),
    ("MOCK0000000002", "mock-window", "stale", False),
    ("MOCK0000000003", "mock-spare", "offline", False),
]


def real_factory(cfg):
    return PrinterService(cfg.host, cfg.serial, cfg.access_code,
                          name=cfg.name, capture=cfg.capture)


def mock_factory(runs_dir: pathlib.Path):
    """Fake printers for the seeded serials; a REAL PrinterService for anything
    added through the UI -- that is how the add-printer error path ("Unreachable")
    gets exercised without hardware."""
    modes = {serial: mode for serial, _, mode, _ in MOCK_SEED}

    def make(cfg):
        mode = modes.get(cfg.serial)
        if mode is None:
            return real_factory(cfg)
        return MockPrinter(runs_dir, serial=cfg.serial, host=cfg.host,
                           name=cfg.name, capture=cfg.capture, mode=mode)

    return make


def main() -> int:
    p = argparse.ArgumentParser(
        prog="python -m server",
        description="Dashboard backend for the bambu_monitor rig.")
    p.add_argument("--mock", action="store_true",
                   help="no printers: seed three fake ones (running/stale/"
                        "offline) and never touch printers.json")
    p.add_argument("--printers-file", type=pathlib.Path,
                   default=pathlib.Path("printers.json"),
                   help="registered-printer list (default printers.json)")
    p.add_argument("--runs-dir", type=pathlib.Path, default=None,
                   help="capture output dir (default runs/, or runs-mock/ "
                        "with --mock)")
    p.add_argument("--port", type=int, default=8000)
    a = p.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
        datefmt="%H:%M:%S")

    if a.mock:
        runs_dir = a.runs_dir or pathlib.Path("runs-mock")
        runs_dir.mkdir(parents=True, exist_ok=True)
        registry = PrinterRegistry(MemoryStore(), mock_factory(runs_dir))
        for serial, name, _mode, capture in MOCK_SEED:
            registry.add(host=name, serial=serial, access_code="00000000",
                         name=name, capture=capture)
    else:
        runs_dir = a.runs_dir or pathlib.Path("runs")
        runs_dir.mkdir(parents=True, exist_ok=True)
        registry = PrinterRegistry(PrinterStore(a.printers_file), real_factory)
        registry.load()

    dist = pathlib.Path(__file__).resolve().parent.parent / "frontend" / "dist"
    app = create_app(registry, runs_dir, dist)
    # uvicorn re-raises the signal it caught using whatever handler was
    # installed beforehand. SIGBREAK's OS default kills the process outright
    # (skipping `finally`), so map it to KeyboardInterrupt like SIGINT gets.
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, signal.default_int_handler)
    try:
        uvicorn.run(app, host="127.0.0.1", port=a.port)
    finally:
        registry.stop_all()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Smoke-test mock mode**

Start (background process): `python -m server --mock`
Expected log: uvicorn banner, `Uvicorn running on http://127.0.0.1:8000`.

Run: `curl http://127.0.0.1:8000/api/printers`
Expected: JSON with three printers — `MOCK0000000001` `"connection":"ok"` and a climbing `layer_num`, `MOCK0000000002` `"connection":"stale"`, `MOCK0000000003` `"connection":"disconnected"` with a `last_error` mentioning "Unreachable".

Run: `curl "http://127.0.0.1:8000/api/printers/MOCK0000000001/files"`
Expected: `{"path":"/","entries":[...]}` with `timelapse` and `cache` first, then `Benchy.3mf`.

Run: `curl "http://127.0.0.1:8000/api/printers/MOCK0000000001/files?path=/../etc"`
Expected: HTTP 400 with a `..` message.

Run: `curl -o nul -w "%{http_code}" http://127.0.0.1:8000/api/frame/latest`
Expected: `200` (give the mock a few seconds to write its first frame).

Verify `printers.json` was **not** created: the repo root must not contain it.

Stop the server.

- [ ] **Step 3: Commit**

```bash
git add server/__main__.py
git commit -m "feat(server): GUI-configured printers; retire per-printer CLI flags"
```

---

### Task 8: Frontend — stylesheet additions + `Field` primitive

All styling for the new UI is written here once, so later tasks only compose class names (per `FRONTEND-STACK-GUIDE.md`: "all real styling lives in styles.css").

**Files:**
- Modify: `frontend/src/styles.css` (append)
- Create: `frontend/src/components/ui/Field.jsx`

- [ ] **Step 1: Append to `frontend/src/styles.css`**

Append at the end of the file, after the existing `.kv` block:

```css
/* ================= field (form control) ================= */
.ui-field { display: flex; flex-direction: column; gap: var(--sp-1); }
.ui-field__label {
  font-size: 12px;
  font-weight: 500;
  color: var(--text-muted);
}
.ui-field__input {
  height: var(--ctl-h);
  padding: 0 var(--sp-3);
  border: 1px solid var(--line-strong);
  border-radius: var(--r-control);
  background: var(--surface);
  color: var(--text);
  font: inherit;
}
.ui-field__input:focus-visible {
  outline: 3px solid var(--focus);
  outline-offset: 1px;
  border-color: var(--primary);
}
.ui-field__input[aria-invalid="true"] { border-color: var(--danger-text); }
.ui-field__help { font-size: 11px; color: var(--text-faint); }
.ui-field__error { font-size: 11px; color: var(--danger-text); }

/* ================= printers ================= */
.printer-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: var(--sp-4);
}
.printer-card {
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: var(--r-card);
  box-shadow: var(--shadow-sm);
  padding: var(--sp-4);
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  text-align: left;
  font: inherit;
  cursor: pointer;
}
.printer-card:hover { border-color: var(--line-strong); }
.printer-card:focus-visible { outline: 3px solid var(--focus); outline-offset: 1px; }
.printer-card--selected {
  border-color: var(--primary);
  box-shadow: 0 0 0 2px var(--focus);
}
.printer-card__name {
  font-size: 14px;
  font-weight: 600;
  color: var(--text);
}
.printer-card__meta {
  font-size: 12px;
  color: var(--text-muted);
  font-variant-numeric: tabular-nums;
}
.printer-card__error { font-size: 11px; color: var(--danger-text); }
.printer-card__foot {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  margin-top: var(--sp-1);
}
.printer-card__foot .ui-btn { margin-left: auto; }

.add-form { display: flex; flex-direction: column; gap: var(--sp-4); max-width: 640px; }
.add-form__row {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: var(--sp-4);
}
@media (max-width: 700px) { .add-form__row { grid-template-columns: 1fr; } }
.add-form__check {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  font-size: 13px;
  color: var(--text-body);
}
.add-form__actions { display: flex; align-items: center; gap: var(--sp-3); }
.add-form__error { font-size: 12px; color: var(--danger-text); }

/* ================= sd files ================= */
.sd-toolbar {
  display: flex;
  align-items: center;
  gap: var(--sp-3);
  margin-bottom: var(--sp-4);
}
.crumbs {
  display: flex;
  align-items: center;
  gap: var(--sp-1);
  font-size: 13px;
  flex-wrap: wrap;
  margin-right: auto;
}
.crumbs__sep { color: var(--text-faint); }
.crumbs__btn {
  border: 0;
  background: transparent;
  color: var(--primary);
  font: inherit;
  padding: 2px var(--sp-1);
  border-radius: var(--r-control);
  cursor: pointer;
}
.crumbs__btn:hover { background: var(--primary-soft); }
.crumbs__btn:disabled { color: var(--text); cursor: default; }
.crumbs__btn:focus-visible { outline: 3px solid var(--focus); outline-offset: 1px; }

.file-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.file-table th {
  text-align: left;
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: var(--text-faint);
  font-weight: 500;
  padding: 0 var(--sp-3) var(--sp-2);
  border-bottom: 1px solid var(--line);
}
.file-table td {
  padding: var(--sp-2) var(--sp-3);
  border-bottom: 1px solid var(--line);
  color: var(--text);
}
.file-table td:nth-child(2), .file-table th:nth-child(2) {
  text-align: right;
  font-variant-numeric: tabular-nums;
}
.file-table tr:last-child td { border-bottom: 0; }
.file-table__dir {
  border: 0;
  background: transparent;
  color: var(--primary);
  font: inherit;
  padding: 0;
  cursor: pointer;
}
.file-table__dir:hover { text-decoration: underline; }
.file-table__dir:focus-visible { outline: 3px solid var(--focus); outline-offset: 1px; }
.file-table__muted { color: var(--text-faint); }

/* ================= shared empty / error states ================= */
.empty {
  padding: var(--sp-7);
  text-align: center;
  color: var(--text-faint);
  background: var(--surface-2);
  border: 1px dashed var(--line-strong);
  border-radius: var(--r-card);
}
.state-error {
  padding: var(--sp-4);
  border-radius: var(--r-control);
  background: var(--danger-bg);
  color: var(--danger-text);
  font-size: 13px;
  display: flex;
  align-items: center;
  gap: var(--sp-3);
}
.state-error .ui-btn { margin-left: auto; }
```

- [ ] **Step 2: Create `frontend/src/components/ui/Field.jsx`**

```jsx
let seq = 0;

export default function Field({ label, help, error, ...rest }) {
  // Stable per-instance id so <label for> points at the right input even when
  // two fields share a label text.
  const id = rest.id ?? `ui-field-${(seq += 1)}`;
  return (
    <div className="ui-field">
      <label className="ui-field__label" htmlFor={id}>{label}</label>
      <input className="ui-field__input" id={id}
             aria-invalid={error ? "true" : undefined} {...rest} />
      {error
        ? <div className="ui-field__error">{error}</div>
        : help ? <div className="ui-field__help">{help}</div> : null}
    </div>
  );
}
```

- [ ] **Step 3: Verify it still builds**

Run (in `frontend/`): `npm run build`
Expected: succeeds. Nothing imports `Field` yet, so this only proves the CSS parses and the scaffold is intact.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/styles.css frontend/src/components/ui/Field.jsx
git commit -m "feat(frontend): Field primitive and styles for printers/SD/forms"
```

---

### Task 9: Frontend data layer + shell

**Files:**
- Modify: `frontend/src/api/printer.js`
- Create: `frontend/src/hooks/usePrinters.js`
- Delete: `frontend/src/hooks/usePrinter.js`
- Modify: `frontend/src/app/pageRegistry.jsx`
- Modify: `frontend/src/App.jsx`
- Create: `frontend/src/pages/Overview.jsx` (stub, completed in Task 10)
- Create: `frontend/src/pages/SdFiles.jsx` (stub, completed in Task 11)

- [ ] **Step 1: Rewrite `frontend/src/api/printer.js`**

```js
// Fetch wrappers for the dashboard backend. WebSocket lives in usePrinters.

// FastAPI puts HTTPException messages in {"detail": "..."}.
async function detail(res) {
  try {
    const body = await res.json();
    return body.detail ?? `HTTP ${res.status}`;
  } catch {
    return `HTTP ${res.status}`;
  }
}

export async function fetchPrinters() {
  const res = await fetch("/api/printers");
  if (!res.ok) throw new Error(await detail(res));
  return (await res.json()).printers ?? [];
}

export async function addPrinter({ host, serial, access_code, name, capture }) {
  const res = await fetch("/api/printers", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ host, serial, access_code, name, capture }),
  });
  if (!res.ok) throw new Error(await detail(res));
  return res.json();
}

export async function removePrinter(serial) {
  const res = await fetch(`/api/printers/${encodeURIComponent(serial)}`,
                          { method: "DELETE" });
  if (!res.ok) throw new Error(await detail(res));
}

// { path, entries: [{ name, is_dir, size, mtime }] }
export async function fetchFiles(serial, path = "/") {
  const res = await fetch(
    `/api/printers/${encodeURIComponent(serial)}/files` +
    `?path=${encodeURIComponent(path)}`);
  if (!res.ok) throw new Error(await detail(res));
  return res.json();
}

// Returns { url, layer, run } (url is an object URL the caller must revoke)
// or null when there is no active run (HTTP 404) or on network error.
export async function fetchLatestFrame() {
  try {
    const res = await fetch(`/api/frame/latest?t=${Date.now()}`, { cache: "no-store" });
    if (!res.ok) return null;
    const blob = await res.blob();
    return {
      url: URL.createObjectURL(blob),
      layer: res.headers.get("X-Frame-Layer"),
      run: res.headers.get("X-Frame-Run"),
    };
  } catch {
    return null; // network error or body stream failure — same as "no frame"
  }
}
```

- [ ] **Step 2: Create `frontend/src/hooks/usePrinters.js`**

```js
import { useEffect, useState } from "react";

const MAX_BACKOFF_MS = 10000;

// Live list of every registered printer over /ws with auto-reconnect.
// Returns { printers, wsUp }: printers is the last received list (empty until
// the first message), wsUp is whether the socket is currently open.
export function usePrinters() {
  const [printers, setPrinters] = useState([]);
  const [wsUp, setWsUp] = useState(false);

  useEffect(() => {
    let ws = null;
    let timer = null;
    let alive = true;
    let delay = 1000;

    const connect = () => {
      const proto = window.location.protocol === "https:" ? "wss" : "ws";
      ws = new WebSocket(`${proto}://${window.location.host}/ws`);
      ws.onopen = () => {
        setWsUp(true);
        delay = 1000;
      };
      ws.onmessage = (e) => setPrinters(JSON.parse(e.data).printers ?? []);
      ws.onclose = () => {
        // A torn-down effect's socket (StrictMode double-mount) must not
        // touch state owned by the effect that replaced it. Dev consoles
        // log "closed before the connection is established" here — expected.
        if (!alive) return;
        setWsUp(false);
        timer = setTimeout(connect, delay);
        delay = Math.min(delay * 2, MAX_BACKOFF_MS);
      };
    };

    connect();
    return () => {
      alive = false;
      clearTimeout(timer);
      if (ws) ws.close();
    };
  }, []);

  return { printers, wsUp };
}
```

- [ ] **Step 3: Delete the old hook**

```bash
git rm frontend/src/hooks/usePrinter.js
```

- [ ] **Step 4: Create `frontend/src/pages/Overview.jsx` as a stub** (real page in Task 10)

```jsx
import PageFrame from "../components/ui/PageFrame.jsx";

export default function Overview({ printers }) {
  return (
    <PageFrame>
      <pre>{JSON.stringify(printers, null, 2)}</pre>
    </PageFrame>
  );
}
```

- [ ] **Step 5: Create `frontend/src/pages/SdFiles.jsx` as a stub** (real page in Task 11)

```jsx
import PageFrame from "../components/ui/PageFrame.jsx";

export default function SdFiles({ selected }) {
  return <PageFrame><div className="empty">SD Files for {selected ?? "—"}</div></PageFrame>;
}
```

- [ ] **Step 6: Rewrite `frontend/src/app/pageRegistry.jsx`**

```jsx
import Dashboard from "../pages/Dashboard.jsx";
import Overview from "../pages/Overview.jsx";
import SdFiles from "../pages/SdFiles.jsx";

// Every page: key -> { title, group, component }. The sidebar and topbar
// are derived from this — add future pages (runs browser, print control)
// here and nowhere else.
//
// Every page receives the same props: { printers, selected, onSelect }.
export const pages = {
  overview: { title: "Overview", group: "Monitor", component: Overview },
  dashboard: { title: "Dashboard", group: "Monitor", component: Dashboard },
  sdfiles: { title: "SD Files", group: "Monitor", component: SdFiles },
};

export function navGroups() {
  const groups = {};
  for (const [key, page] of Object.entries(pages)) {
    (groups[page.group] ??= []).push({ key, title: page.title });
  }
  return groups;
}
```

- [ ] **Step 7: Rewrite `frontend/src/App.jsx`**

```jsx
import { useEffect, useState } from "react";
import { navGroups, pages } from "./app/pageRegistry.jsx";
import NavGroup from "./components/ui/NavGroup.jsx";
import StatusPill from "./components/ui/StatusPill.jsx";
import { usePrinters } from "./hooks/usePrinters.js";

const CONN = {
  ok: { status: "ok", label: "Connected" },
  stale: { status: "warn", label: "Stale" },
  disconnected: { status: "danger", label: "Printer offline" },
};
const SERVER_DOWN = { status: "danger", label: "Server offline" };
const NO_PRINTERS = { status: "warn", label: "No printers" };

export default function App() {
  const [active, setActive] = useState("overview");
  const [selected, setSelected] = useState(null);
  const { printers, wsUp } = usePrinters();

  // One printer is the common case — never make the user pick. Also repairs
  // the selection when the selected printer is removed.
  useEffect(() => {
    if (printers.length === 1) setSelected(printers[0].serial);
    else if (selected && !printers.some((p) => p.serial === selected)) {
      setSelected(printers[0]?.serial ?? null);
    }
  }, [printers, selected]);

  const select = (serial) => {
    setSelected(serial);
    setActive("dashboard");
  };

  const current = printers.find((p) => p.serial === selected) ?? null;
  const online = printers.filter((p) => p.connection === "ok").length;

  let conn;
  if (!wsUp) conn = SERVER_DOWN;
  else if (printers.length === 0) conn = NO_PRINTERS;
  else if (current) conn = CONN[current.connection] ?? CONN.stale;
  else conn = { status: "ok", label: `${online} of ${printers.length} online` };

  const Page = pages[active].component;

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="sidebar__brand">bambu monitor</div>
        {Object.entries(navGroups()).map(([label, items]) => (
          <NavGroup key={label} label={label} items={items}
                    activeKey={active} onSelect={setActive} />
        ))}
      </aside>
      <div className="main">
        <header className="topbar">
          <span className="topbar__title">{pages[active].title}</span>
          <span className="topbar__host">
            {printers.length > 0
              ? `${printers.length} printer${printers.length === 1 ? "" : "s"} · ${online} online`
              : ""}
          </span>
          <StatusPill status={conn.status}>{conn.label}</StatusPill>
        </header>
        <div className={!wsUp ? "dimmed" : ""}>
          <Page printers={printers} selected={selected} onSelect={select} />
        </div>
      </div>
    </div>
  );
}
```

- [ ] **Step 8: Replace `frontend/src/pages/Dashboard.jsx` entirely** (transitional — camera gating lands in Task 12)

The whole file, not a partial edit: the old body passes `summary={summary}` to two
child cards, and that binding no longer exists once the props change.

```jsx
import CameraCard from "../components/dashboard/CameraCard.jsx";
import HmsCard from "../components/dashboard/HmsCard.jsx";
import PrintInfoCard from "../components/dashboard/PrintInfoCard.jsx";
import Columns from "../components/ui/Columns.jsx";
import PageFrame from "../components/ui/PageFrame.jsx";
import Stack from "../components/ui/Stack.jsx";
import StatTile from "../components/ui/StatTile.jsx";

const deg = (v) => (v == null ? "—" : `${Number(v).toFixed(0)}°`);

export default function Dashboard({ printers, selected }) {
  const s = printers.find((p) => p.serial === selected) ?? {};
  return (
    <PageFrame>
      <div className="tile-row">
        <StatTile label="State" value={s.gcode_state} />
        <StatTile label="Layer"
                  value={s.layer_num != null
                    ? `${s.layer_num} / ${s.total_layer_num ?? "?"}` : null} />
        <StatTile label="Progress"
                  value={s.mc_percent != null ? `${s.mc_percent}%` : null} />
        <StatTile label="Remaining"
                  value={s.mc_remaining_time != null
                    ? `${s.mc_remaining_time} min` : null} />
        <StatTile label="Nozzle" value={deg(s.nozzle_temper)}
                  sub={`target ${deg(s.nozzle_target_temper)}`} />
        <StatTile label="Bed" value={deg(s.bed_temper)}
                  sub={`target ${deg(s.bed_target_temper)}`} />
      </div>
      <Columns template="3fr 2fr">
        <CameraCard />
        <Stack gap={5}>
          <PrintInfoCard summary={s} />
          <HmsCard summary={s} />
        </Stack>
      </Columns>
    </PageFrame>
  );
}
```

- [ ] **Step 9: Verify against the mock server**

Terminal 1 (repo root): `python -m server --mock`
Terminal 2 (in `frontend/`): `npm run dev`

Open `http://localhost:5173`. Expected:
- sidebar shows "Monitor → Overview · Dashboard · SD Files", Overview active
- topbar reads "3 printers · 1 online", with a green pill "1 of 3 online" (no
  printer is selected yet, so the pill reports the fleet, not one printer)
- Overview shows the raw JSON of three printers, `MOCK0000000001` ticking
- kill the python server → pill flips to red "Server offline" and content dims; restart → recovers within ~10 s

- [ ] **Step 10: Commit**

```bash
git add frontend/src
git commit -m "feat(frontend): multi-printer data layer, shell, and page registry"
```

---

### Task 10: Overview page — printer grid + add form

**Files:**
- Modify: `frontend/src/styles.css` (append the card-overlay rules in Step 1)
- Create: `frontend/src/components/printers/PrinterCard.jsx`
- Create: `frontend/src/components/printers/AddPrinterForm.jsx`
- Modify: `frontend/src/pages/Overview.jsx` (replace the stub entirely)

- [ ] **Step 1: Append the card-overlay rules to `frontend/src/styles.css`**

The card cannot be a `<button>`: it contains the Remove `<button>`, and nesting
one interactive element inside another is invalid HTML — it breaks Tab order and
screen readers collapse or hide the inner control. Instead the card is a plain
`<div>` and the *name* is a real button whose `::after` overlays the whole card.
That yields valid HTML, exactly two tab stops (select, Remove), and a card that
is still clickable anywhere.

```css
/* The card is a <div>, not a <button> — it contains the Remove button, and
   nested interactive elements are invalid HTML. The name button's ::after
   overlays the whole card instead: one tab stop to select, one to remove. */
.printer-card { position: relative; }
.printer-card__select {
  border: 0;
  background: transparent;
  padding: 0;
  font: inherit;
  text-align: left;
  cursor: pointer;
}
.printer-card__select::after {
  content: "";
  position: absolute;
  inset: 0;
  border-radius: var(--r-card);
}
.printer-card__select:focus-visible { outline: none; }
.printer-card__select:focus-visible::after {
  outline: 3px solid var(--focus);
  outline-offset: 1px;
}
/* Remove must sit above the overlay or the overlay swallows its clicks.
   Button.jsx does not merge a caller's className, so this is reached by
   descendant selector rather than a modifier class. */
.printer-card__foot .ui-btn { position: relative; z-index: 1; }
```

- [ ] **Step 2: Create `frontend/src/components/printers/PrinterCard.jsx`**

```jsx
import { useState } from "react";
import { removePrinter } from "../../api/printer.js";
import Button from "../ui/Button.jsx";
import StatusPill from "../ui/StatusPill.jsx";

// connection -> pill. When connected, show what the printer is actually doing.
function pill(p) {
  if (p.connection === "disconnected") return { status: "danger", label: "Offline" };
  if (p.connection === "stale") return { status: "warn", label: "Stale" };
  return { status: "ok", label: p.gcode_state ?? "Connected" };
}

export default function PrinterCard({ printer, selected, onSelect }) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const { status, label } = pill(printer);

  const remove = async () => {
    if (!window.confirm(`Remove ${printer.name}? It will stop being monitored.`))
      return;
    setBusy(true);
    setErr(null);
    try {
      await removePrinter(printer.serial);
      // No refetch: /ws pushes the new list within ~250 ms.
    } catch (e2) {
      setErr(e2.message);
      setBusy(false);
    }
  };

  const progress = printer.layer_num != null
    ? `layer ${printer.layer_num} / ${printer.total_layer_num ?? "?"}` +
      (printer.mc_percent != null ? ` · ${printer.mc_percent}%` : "")
    : "—";

  return (
    <div className={`printer-card${selected ? " printer-card--selected" : ""}`}>
      {/* The ::after on this button covers the card, so clicking anywhere
          selects — without nesting Remove inside another button. */}
      <button type="button"
              className="printer-card__name printer-card__select"
              aria-pressed={selected}
              onClick={() => onSelect(printer.serial)}>
        {printer.name}
      </button>
      <div className="printer-card__meta">{printer.printer}</div>
      <StatusPill status={status}>{label}</StatusPill>
      <div className="printer-card__meta">{progress}</div>
      {printer.last_error && (
        <div className="printer-card__error">{printer.last_error}</div>
      )}
      {err && <div className="printer-card__error">{err}</div>}
      <div className="printer-card__foot">
        {printer.capture && <span className="ui-stattile__sub">camera</span>}
        <Button variant="ghost" size="sm" busy={busy} onClick={remove}>
          Remove
        </Button>
      </div>
    </div>
  );
}
```

Note `remove` no longer needs `e.stopPropagation()`: the card is not a button,
so there is no ancestor click handler to stop. The z-index rule is what keeps
the overlay from swallowing the click.

- [ ] **Step 3: Create `frontend/src/components/printers/AddPrinterForm.jsx`**

```jsx
import { useState } from "react";
import { addPrinter } from "../../api/printer.js";
import Button from "../ui/Button.jsx";
import Field from "../ui/Field.jsx";

const BLANK = { host: "", serial: "", access_code: "", name: "", capture: false };

export default function AddPrinterForm() {
  const [form, setForm] = useState(BLANK);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);

  const set = (k) => (e) =>
    setForm((f) => ({
      ...f,
      [k]: e.target.type === "checkbox" ? e.target.checked : e.target.value,
    }));

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    setErr(null);
    try {
      await addPrinter({
        host: form.host.trim(),
        serial: form.serial.trim(),
        access_code: form.access_code.trim(),
        name: form.name.trim(),
        capture: form.capture,
      });
      setForm(BLANK); // /ws pushes the new card in on its own
    } catch (e2) {
      setErr(e2.message);
    } finally {
      setBusy(false);
    }
  };

  const ready = form.host.trim() && form.serial.trim() && form.access_code.trim();

  return (
    <form className="add-form" onSubmit={submit}>
      <div className="add-form__row">
        <Field label="IP address" value={form.host} onChange={set("host")}
               placeholder="192.168.137.2"
               help="Printer screen: Settings → WLAN" />
        <Field label="Serial" value={form.serial} onChange={set("serial")}
               placeholder="0300CA633005010"
               help="Settings → Device, or the sticker" />
      </div>
      <div className="add-form__row">
        <Field label="LAN access code" value={form.access_code}
               onChange={set("access_code")} placeholder="31661007"
               help="Usually 8 characters. Rotates on some firmware updates." />
        <Field label="Name (optional)" value={form.name} onChange={set("name")}
               placeholder="A1-bench" help="Defaults to the IP address" />
      </div>
      <label className="add-form__check">
        <input type="checkbox" checked={form.capture} onChange={set("capture")} />
        This printer is the one the webcam points at
      </label>
      {err && <div className="add-form__error">{err}</div>}
      <div className="add-form__actions">
        <Button type="submit" variant="primary" busy={busy} disabled={!ready}>
          Connect
        </Button>
        <span className="ui-field__help">
          Requires LAN-only Mode and Developer Mode on the printer.
        </span>
      </div>
    </form>
  );
}
```

- [ ] **Step 4: Replace `frontend/src/pages/Overview.jsx` entirely**

```jsx
import AddPrinterForm from "../components/printers/AddPrinterForm.jsx";
import PrinterCard from "../components/printers/PrinterCard.jsx";
import PageFrame from "../components/ui/PageFrame.jsx";
import Section from "../components/ui/Section.jsx";

export default function Overview({ printers, selected, onSelect }) {
  const online = printers.filter((p) => p.connection === "ok").length;
  const title = printers.length === 0
    ? "Printers"
    : `Printers (${printers.length} · ${online} online)`;

  return (
    <PageFrame>
      <Section title={title}>
        {printers.length === 0 ? (
          <div className="empty">
            No printers yet — add one below to start monitoring it.
          </div>
        ) : (
          <div className="printer-grid">
            {printers.map((p) => (
              <PrinterCard key={p.serial} printer={p}
                           selected={p.serial === selected}
                           onSelect={onSelect} />
            ))}
          </div>
        )}
      </Section>
      <Section title="Add printer">
        <AddPrinterForm />
      </Section>
    </PageFrame>
  );
}
```

- [ ] **Step 5: Verify against the mock**

With `python -m server --mock` and `npm run dev` running, open `http://localhost:5173`. Expected:
1. Three cards: `mock-bench` green with a climbing layer count and a "camera" marker; `mock-window` amber "Stale"; `mock-spare` red "Offline" with the "Unreachable — check the IP…" message.
2. Header reads "Printers (3 · 1 online)".
3. Clicking a card highlights it and navigates to Dashboard.
4. Add a bogus printer (IP `192.0.2.1`, serial `TEST`, code `12345678`) → a fourth card appears and settles on red "Offline" with "Unreachable — check the IP, and that LAN-only Mode is on" within ~10 s. **This is the add-printer error path working.**
5. Re-adding serial `TEST` → the form shows "that serial is already registered".
6. Remove the bogus printer → confirm dialog → the card disappears.
7. With `Connect` disabled until all three required fields have content.

- [ ] **Step 6: Commit**

```bash
git add frontend/src
git commit -m "feat(frontend): Overview grid with live printer cards and add form"
```

---

### Task 11: SD Files page

**Files:**
- Create: `frontend/src/components/sd/FileTable.jsx`
- Modify: `frontend/src/pages/SdFiles.jsx` (replace the stub entirely)

- [ ] **Step 1: Create `frontend/src/components/sd/FileTable.jsx`**

```jsx
const UNITS = ["B", "KB", "MB", "GB"];

function humanSize(bytes) {
  if (bytes == null) return "—";
  let n = bytes;
  let u = 0;
  while (n >= 1024 && u < UNITS.length - 1) {
    n /= 1024;
    u += 1;
  }
  return `${u === 0 ? n : n.toFixed(1)} ${UNITS[u]}`;
}

// The LIST fallback reports no mtime (see server/sdcard.py) — render that as
// an em dash rather than an empty cell.
function when(mtime) {
  if (!mtime) return "—";
  const d = new Date(mtime);
  return Number.isNaN(d.getTime()) ? mtime : d.toLocaleString();
}

export default function FileTable({ entries, onOpen }) {
  if (entries.length === 0) {
    return <div className="empty">This folder is empty.</div>;
  }
  return (
    <table className="file-table">
      <thead>
        <tr><th>Name</th><th>Size</th><th>Modified</th></tr>
      </thead>
      <tbody>
        {entries.map((e) => (
          <tr key={e.name}>
            <td>
              {e.is_dir ? (
                <button type="button" className="file-table__dir"
                        onClick={() => onOpen(e.name)}>
                  {e.name}/
                </button>
              ) : e.name}
            </td>
            <td className={e.size == null ? "file-table__muted" : undefined}>
              {humanSize(e.size)}
            </td>
            <td className="file-table__muted">{when(e.mtime)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
```

- [ ] **Step 2: Replace `frontend/src/pages/SdFiles.jsx` entirely**

```jsx
import { useCallback, useEffect, useState } from "react";
import { fetchFiles } from "../api/printer.js";
import FileTable from "../components/sd/FileTable.jsx";
import Button from "../components/ui/Button.jsx";
import Card from "../components/ui/Card.jsx";
import PageFrame from "../components/ui/PageFrame.jsx";

const join = (path, name) => (path === "/" ? `/${name}` : `${path}/${name}`);

function Crumbs({ path, onGo }) {
  const parts = path.split("/").filter(Boolean);
  return (
    <nav className="crumbs" aria-label="Folder path">
      <button type="button" className="crumbs__btn" onClick={() => onGo("/")}
              disabled={parts.length === 0}>
        SD card
      </button>
      {parts.map((part, i) => (
        <span key={`${part}-${i}`}>
          <span className="crumbs__sep">/</span>
          <button type="button" className="crumbs__btn"
                  disabled={i === parts.length - 1}
                  onClick={() => onGo("/" + parts.slice(0, i + 1).join("/"))}>
            {part}
          </button>
        </span>
      ))}
    </nav>
  );
}

export default function SdFiles({ printers, selected }) {
  const printer = printers.find((p) => p.serial === selected) ?? null;
  const [path, setPath] = useState("/");
  const [entries, setEntries] = useState([]);
  const [err, setErr] = useState(null);
  const [loading, setLoading] = useState(false);

  // Switching printers must not show the previous card's tree.
  useEffect(() => {
    setPath("/");
    setEntries([]);
    setErr(null);
  }, [selected]);

  const load = useCallback(async (target) => {
    if (!selected) return;
    setLoading(true);
    setErr(null);
    try {
      const data = await fetchFiles(selected, target);
      setEntries(data.entries);
    } catch (e) {
      setErr(e.message);
      setEntries([]);
    } finally {
      setLoading(false);
    }
  }, [selected]);

  // An FTPS handshake is not instant — this is not a poller, it runs on
  // navigation and on Refresh only.
  useEffect(() => { load(path); }, [load, path]);

  if (!printer) {
    return (
      <PageFrame>
        <div className="empty">
          No printer selected — pick one on the Overview page.
        </div>
      </PageFrame>
    );
  }

  return (
    <PageFrame>
      <Card title={`microSD — ${printer.name}`}>
        <div className="sd-toolbar">
          <Crumbs path={path} onGo={setPath} />
          <Button size="sm" busy={loading} onClick={() => load(path)}>
            Refresh
          </Button>
        </div>
        {err ? (
          <div className="state-error">
            <span>{err}</span>
            <Button size="sm" onClick={() => load(path)}>Retry</Button>
          </div>
        ) : loading && entries.length === 0 ? (
          <div className="empty">Reading the card…</div>
        ) : (
          <FileTable entries={entries}
                     onOpen={(name) => setPath(join(path, name))} />
        )}
      </Card>
    </PageFrame>
  );
}
```

- [ ] **Step 3: Verify against the mock**

With both servers running, select `mock-bench` on Overview, then open SD Files. Expected:
1. Table lists `cache/` and `timelapse/` first (folders), then `Benchy.3mf` (1.0 MB) and `calibration_cube.gcode.3mf` (200.0 KB), with formatted dates.
2. Clicking `timelapse/` navigates in; the breadcrumb reads "SD card / timelapse"; the last crumb is disabled; clicking "SD card" returns to the root.
3. Refresh re-reads without changing the path.
4. Select `mock-spare` (offline) on Overview, open SD Files → the mock has no tree for it beyond the standard one, so it lists normally; then pick the bogus `192.0.2.1` printer added in Task 10 → **a red error banner with a Retry button** ("Could not list / on 192.0.2.1: …"), and the Dashboard still works. That is the FTPS-failure path.
5. With no printer selected → "No printer selected" empty state.

- [ ] **Step 4: Commit**

```bash
git add frontend/src
git commit -m "feat(frontend): microSD file browser with breadcrumbs and error states"
```

---

### Task 12: Dashboard — selected printer, camera only on the capture printer

**Files:**
- Modify: `frontend/src/pages/Dashboard.jsx` (replace entirely)

- [ ] **Step 1: Replace `frontend/src/pages/Dashboard.jsx` entirely**

```jsx
import CameraCard from "../components/dashboard/CameraCard.jsx";
import HmsCard from "../components/dashboard/HmsCard.jsx";
import PrintInfoCard from "../components/dashboard/PrintInfoCard.jsx";
import Card from "../components/ui/Card.jsx";
import Columns from "../components/ui/Columns.jsx";
import PageFrame from "../components/ui/PageFrame.jsx";
import Stack from "../components/ui/Stack.jsx";
import StatTile from "../components/ui/StatTile.jsx";

const deg = (v) => (v == null ? "—" : `${Number(v).toFixed(0)}°`);

export default function Dashboard({ printers, selected }) {
  const s = printers.find((p) => p.serial === selected) ?? null;

  if (!s) {
    return (
      <PageFrame>
        <div className="empty">
          No printer selected — pick one on the Overview page.
        </div>
      </PageFrame>
    );
  }

  return (
    <PageFrame>
      <div className="tile-row">
        <StatTile label="State" value={s.gcode_state} />
        <StatTile label="Layer"
                  value={s.layer_num != null
                    ? `${s.layer_num} / ${s.total_layer_num ?? "?"}` : null} />
        <StatTile label="Progress"
                  value={s.mc_percent != null ? `${s.mc_percent}%` : null} />
        <StatTile label="Remaining"
                  value={s.mc_remaining_time != null
                    ? `${s.mc_remaining_time} min` : null} />
        <StatTile label="Nozzle" value={deg(s.nozzle_temper)}
                  sub={`target ${deg(s.nozzle_target_temper)}`} />
        <StatTile label="Bed" value={deg(s.bed_temper)}
                  sub={`target ${deg(s.bed_target_temper)}`} />
      </div>
      <Columns template="3fr 2fr">
        {/* There is one webcam. Showing its frames on a printer it isn't
            pointed at would be a lie, so only the capture printer gets it. */}
        {s.capture ? (
          <CameraCard />
        ) : (
          <Card title="Camera">
            <div className="camera-placeholder">
              No camera on this printer — mark it as the capture printer on the
              Overview page if the webcam points at it.
            </div>
          </Card>
        )}
        <Stack gap={5}>
          <PrintInfoCard summary={s} />
          <HmsCard summary={s} />
        </Stack>
      </Columns>
    </PageFrame>
  );
}
```

- [ ] **Step 2: Verify against the mock**

With both servers running:
1. Select `mock-bench` (the capture printer) → Dashboard shows the growing synthetic print in CameraCard, tiles ticking, and between layers 12–16 a red HMS pill `0300_0100_0001_0007`.
2. Select `mock-window` (stale) → tiles show its frozen state; CameraCard is replaced by "No camera on this printer".
3. Select `mock-spare` (offline) → tiles mostly "—"; no camera.
4. Remove every printer on Overview → Dashboard shows "No printer selected".

- [ ] **Step 3: Commit**

```bash
git add frontend/src/pages/Dashboard.jsx
git commit -m "feat(frontend): dashboard follows the selected printer; camera gated on capture"
```

---

### Task 13: Docs, production build, full verification

**Files:**
- Modify: `CONNECTION.md`
- Modify: `README (1).md`

- [ ] **Step 1: Update `CONNECTION.md`**

Replace the entire "## Plugging these values into GUI_UCDavis" section (its heading through the line ending `...for the full dashboard design this connection feeds into.`) with:

```markdown
## Plugging these values into GUI_UCDavis

The dashboard no longer takes the printer on the command line. Start it:

```
python -m server
```

Then open http://localhost:8000, go to **Overview → Add printer**, and type:

| Field | Value |
|---|---|
| IP address | `192.168.137.2` |
| Serial | `0300CA633005010` |
| LAN access code | `31661007` |
| Name (optional) | anything, e.g. `A1-bench` |
| Camera checkbox | tick it if the webcam points at this printer |

The printer is saved to `printers.json` (gitignored — it holds the access code
in plaintext) and reconnects automatically on every restart. Add up to a
handful of printers this way; the Overview page shows all of them at once.

If a printer sits on red **Offline**, the card tells you which failure it is:
"Unreachable" means the IP is wrong or LAN-only Mode is off; "No response"
means the TLS handshake worked but the access code is likely wrong, or
Developer Mode is off. That maps to the troubleshooting list below.

**microSD files** are read over FTPS (port 990), not MQTT — MQTT exposes no
file listing at all. Same `bblp` + access-code credentials. The SD Files page
is read-only.

Add `--port 8000` to change the port, `--runs-dir runs/` to change where
capture frames are read from, `--printers-file` to move the printer list.
Without hardware, `python -m server --mock` seeds three fake printers
(running / stale / offline) so the whole UI can be exercised.

Then, for frontend dev, run `npm run dev` inside `GUI_UCDavis/frontend`
(port 5173, proxies `/api` and `/ws` to the backend on port 8000). For a
normal/prod run, `npm run build` once and the single `python -m server`
process serves everything on `http://localhost:8000`.

See `GUI_UCDavis/docs/superpowers/specs/2026-07-16-bambu-dashboard-design.md`
for the v1 dashboard design, and
`docs/superpowers/specs/2026-07-16-multi-printer-sd-browser-design.md` for the
multi-printer + SD browser design this connection feeds into.
```

- [ ] **Step 2: Update `README (1).md`**

In the `## 4. server/ + frontend/ — the dashboard` section, replace the fenced `bash` block with:

```bash
pip install -r requirements.txt

# once, or after frontend changes:
cd frontend; npm install; npm run build; cd ..

# with printers (add them in the browser: Overview → Add printer):
python -m server

# without any hardware (three fake printers into runs-mock/):
python -m server --mock
```

Then, immediately below that block, add:

```markdown
Printers are added in the GUI by typing their IP, serial, and LAN access code —
there are no `--host/--serial/--access-code` flags any more. They persist to
`printers.json` (gitignored; it holds access codes in plaintext) and reconnect
on restart. The Overview page shows every printer's live status; the SD Files
page lists each printer's microSD read-only over FTPS.
```

- [ ] **Step 3: Build the frontend**

Run (in `frontend/`): `npm run build`
Expected: `dist/index.html` + assets produced, no errors.

- [ ] **Step 4: Verify single-process serving**

Run (repo root): `python -m server --mock`
Open `http://127.0.0.1:8000` (note: 8000, not 5173).
Expected: the full app, identical to the Vite dev behavior. This proves the StaticFiles mount and that `/ws` and `/api` work same-origin.

- [ ] **Step 5: Verify persistence across a restart (the spec's exit criterion)**

Run (repo root, NOT mock): `python -m server`
In the browser at `http://127.0.0.1:8000`, add a printer: IP `192.0.2.1`, serial `PERSIST1`, code `12345678`.
Expected: a card appears, settles on "Offline / Unreachable".

Stop the server. Run: `cat printers.json`
Expected: one entry with `"serial": "PERSIST1"`.

Run: `git status --short`
Expected: `printers.json` does **not** appear — it is ignored.

Restart `python -m server`, reload the page.
Expected: the `PERSIST1` card is back without retyping anything.

Remove it in the UI, stop the server, `cat printers.json` → `[]`. Delete the file.

- [ ] **Step 6: Run the entire test suite one last time**

Run: `pytest server/tests -v`
Expected: 0 failures.

- [ ] **Step 7: Commit**

```bash
git add "README (1).md" CONNECTION.md docs/
git commit -m "docs: GUI-configured printers, SD browser, retired CLI flags"
```

---

## Spec exit criterion (verify before calling this done)

**Verifiable now, under `--mock`** — every item below is covered by a step above:

| Criterion | Covered by |
|---|---|
| Overview shows 3 printers, distinct states, live count | Task 10 Step 4 |
| Selecting one drives the Dashboard | Task 12 Step 2 |
| SD page lists a tree and navigates folders | Task 11 Step 3 |
| Adding a bogus printer surfaces "Unreachable" | Task 10 Step 4 item 4 |
| Removing a printer stops it and updates the store | Task 13 Step 5 |
| Restart restores printers from `printers.json` | Task 13 Step 5 |
| `printers.json` never enters git | Task 1 Step 1, Task 13 Step 5 |
| Access code never in a payload | Tasks 3/4/5 tests |
| `pytest server/tests` passes | Task 13 Step 6 |

**Deferred to hardware — do NOT treat as blocking.** The printer was offline
during planning (all ports time out). When it is back:

1. Add the real printer (`192.168.137.2` / `0300CA633005010` / `31661007`) via
   the GUI and confirm the card goes green.
2. Open SD Files and confirm real filenames appear. **This is the highest-risk
   step in the whole plan.** Both known failure modes are already pre-empted in
   `server/sdcard.py` — implicit TLS at connect time via the `sock` property,
   and TLS session reuse on the data channel via the `ntransfercmd` override
   (the stdlib does NOT do this; verified against Python 3.11.9). If it still
   fails, the symptom tells you which:
   - login itself is rejected → access code, or FTPS is off on that firmware
   - login succeeds, LIST hangs or resets → still a data-channel TLS problem;
     check `_prot_p` is set (i.e. `prot_p()` ran) and that the session is being
     passed
   - login succeeds, LIST returns 500/502 → MLSD unsupported; the LIST fallback
     should have engaged automatically. If it did not, the `error_perm` catch
     is too narrow.
3. If MLSD is unsupported, the LIST fallback engages automatically and the
   Modified column shows "—". That is expected, not a bug.
4. Confirm two printers connected at once both report independently.
