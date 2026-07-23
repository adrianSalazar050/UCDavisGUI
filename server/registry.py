"""The live set of printers, keyed by serial.

Replaces v1's single PrinterService. Holds the config alongside the service
rather than reading fields back off the service, so persistence never depends
on what a given service class happens to expose -- MockPrinter has no
access_code, and asking it for one would be a bug waiting to happen.

Ordering is registration order and nothing else: a grid that reshuffles itself
as printers change state is unusable.

Locking model (two locks, deliberately):

- `self._lock` guards the in-memory `_configs`/`_services` dicts. It is only
  ever held for quick, in-memory work -- dict reads/writes and building a
  service via the factory (PrinterService/BambuLink's __init__ only builds an
  mqtt.Client object; it does no socket I/O until connect(), which runs on
  its own background thread after start()). It is NEVER held across
  svc.start() or svc.stop(), both of which can block on real I/O or a thread
  join, and NEVER held across disk I/O.

- `self._persist_lock` guards `_persist()`'s read-snapshot-then-write-to-disk
  sequence, serializing it against itself so concurrent add()/remove() calls
  (FastAPI routes run on a threadpool, so this is a real race) can't race
  their store.save() calls and silently lose a printer from the persisted
  file. See _persist() for the full argument.

summaries() runs on the asyncio event loop for every WebSocket tick, so it
must never block. It takes `self._lock` only long enough to copy the dict
values into a list -- never across svc.summary() itself, and never across
anything that touches _persist_lock or disk. That snapshot lock is defense
in depth rather than a fix for an observed crash: `list(a_dict.values())`
racing a concurrent `dict[key] = value` is, empirically, safe under
CPython's GIL today (verified with a 4-writer/4-reader stress test -- no
`RuntimeError: dictionary changed size during iteration` in millions of
iterations), because materializing a dict view via the `list()` builtin is
a tight C loop that never calls back into Python and so never yields the
GIL mid-loop, and a single dict `__setitem__`/`pop` is itself one atomic
C-level step. That safety is a GIL implementation detail, not a language
guarantee -- it would not hold under a free-threaded (no-GIL) CPython
build -- and the lock costs one uncontended acquire/release to make the
guarantee explicit instead of borrowed, so it stays in.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Callable

from .sdcard import SdError
from .store import (BED_TYPES, CAMERA_SOURCES, DEFAULT_BED_TYPE,
                    DEFAULT_NOZZLE, DETECTION_CLASSES, PrinterConfig,
                    guess_model_id, normalize_roi)

log = logging.getLogger("server.registry")

# "argument not supplied", distinct from None. Needed for roi, where None is a
# real value meaning "clear it" rather than "leave it alone".
_UNSET = object()


class DuplicateSerial(Exception):
    """That serial is already registered."""


class PrinterRegistry:
    """Owns one service per printer.

    `service_factory(cfg) -> service` builds the thing that talks to a printer.
    Injecting it is what lets --mock seed fake printers and lets the tests run
    with no sockets at all.

    A service must provide: start(), stop(), summary() -> dict,
    list_files(path) -> list[dict], and attributes serial, host, name,
    capture.
    """

    def __init__(self, store, service_factory: Callable[[PrinterConfig], Any]):
        self._store = store
        self._factory = service_factory
        # dicts preserve insertion order == registration order
        self._configs: dict[str, PrinterConfig] = {}
        self._services: dict[str, Any] = {}
        self._lock = threading.Lock()
        self._persist_lock = threading.Lock()

    # ---------------- lifecycle ----------------

    def load(self) -> None:
        """Restore and start everything in the store. Called once at
        startup, before the app serves requests.

        Also enforces the single-capture invariant across the restored set:
        store.py's load() is deliberately tolerant of a hand-edited
        printers.json, so a file with *two* `capture: true` entries is
        possible on disk. That is not store.py's invariant to police -- it
        is registry's (design decision #4) -- so this walks entries in file
        order and clears any earlier capture flag when a later one also
        claims it, same "last one wins" rule add() uses.
        """
        to_start = []
        for cfg in self._store.load():
            with self._lock:
                if cfg.serial in self._services:
                    continue
                if cfg.capture:
                    self._clear_capture()
                svc = self._factory(cfg)
                self._configs[cfg.serial] = cfg
                self._services[cfg.serial] = svc
            to_start.append(svc)
        for svc in to_start:
            svc.start()
        log.info("restored %d printer(s)", len(to_start))

    def stop_all(self) -> None:
        with self._lock:
            services = list(self._services.values())
        for svc in services:
            try:
                svc.stop()
            except Exception as e:  # noqa: BLE001 - one bad stop must not
                log.warning("error stopping %s: %s", svc, e)  # skip the rest

    # ---------------- mutation ----------------

    def add(self, host: str, serial: str, access_code: str,
            name: str = "", capture: bool = False,
            model_id: str | None = None) -> dict:
        # Required fields: reject non-string types outright, before they
        # ever reach PrinterConfig. __post_init__ does `(x or "").strip()`,
        # which happily coerces a *falsy* non-string (None, 0) to "" --
        # caught by the emptiness check below -- but a *truthy* non-string
        # (host=123, say) sails through `x or ""` unchanged and then blows
        # up in `.strip()` with a raw AttributeError instead of a clean,
        # catchable ValueError. store.py's from_dict guards the on-disk
        # boundary the same way; add() is the in-memory equivalent.
        for field_name, value in (("host", host), ("serial", serial),
                                  ("access_code", access_code)):
            if not isinstance(value, str):
                raise ValueError(f"{field_name} must be a string, got "
                                f"{type(value).__name__}")
        if not isinstance(name, str):
            name = ""  # cosmetic field: same tolerance as from_dict
        if not isinstance(capture, bool):
            capture = False  # gates the webcam; wrong type defaults safe

        # None means "work it out from the serial"; "" is a deliberate
        # "unknown", which disables the model check for this printer.
        if model_id is None:
            model_id = guess_model_id(serial)
        elif not isinstance(model_id, str):
            model_id = ""

        cfg = PrinterConfig(serial=serial, host=host, access_code=access_code,
                           name=name, capture=capture, model_id=model_id)
        # __post_init__ already strips whitespace (so host="   " -> ""),
        # which is exactly what makes this emptiness check catch
        # whitespace-only fields too, not just outright-missing ones.
        if not (cfg.serial and cfg.host and cfg.access_code):
            raise ValueError("host, serial and access_code are all required")

        with self._lock:
            if cfg.serial in self._services:
                raise DuplicateSerial(cfg.serial)
            if cfg.capture:
                self._clear_capture()
            # Factory is called under the lock -- it only builds an in-memory
            # object (see module docstring) -- but start() is not: start()
            # can spawn a thread or otherwise block, and summaries() must
            # never be made to wait behind that.
            svc = self._factory(cfg)
            self._configs[cfg.serial] = cfg
            self._services[cfg.serial] = svc
        svc.start()
        self._persist()
        return svc.summary()

    def remove(self, serial: str) -> bool:
        with self._lock:
            svc = self._services.pop(serial, None)
            self._configs.pop(serial, None)
        if svc is None:
            return False
        # Stop outside the lock: stop() may join a thread for up to 2s, and
        # summaries() must never wait behind that. This does leave a narrow
        # window where the serial is already gone from the registry (so a
        # concurrent add() of the same serial can succeed, and summaries()
        # already stops reporting it) while the old service is still
        # finishing its stop() in the background. Accepted: closing that
        # window would mean either holding the lock across stop() (the
        # thing we're avoiding) or a per-serial lock, and nothing in this
        # task's scope needs that.
        svc.stop()
        self._persist()
        return True

    def update(self, serial, *, host=None, access_code=None, name=None,
               capture=None, model_id=None, bed_type=None) -> dict | None:
        """Edit a registered printer's connection info. Returns the new summary
        dict, or None if the serial is unknown. The serial is NOT changeable.
        A blank/None access_code KEEPS the current one. Changing host or
        access_code rebuilds the service (so it reconnects with the new
        params); name/capture-only edits update the live service in place.
        Raises ValueError on an empty host when host is provided.

        bed_type: None = not submitted, keep the stored plate (same
        "omission keeps the current value" convention as model_id/name/
        capture) -- an old client, or a save that only touches host/name,
        must never silently reset a deliberately-chosen plate back to the
        default. Unlike model_id there is no "Unknown" sentinel here: every
        value must be one of BED_TYPES, so a submitted-but-invalid one
        degrades to DEFAULT_BED_TYPE rather than being stored verbatim --
        same posture as store.py's from_dict, because a wrong plate slices
        for the wrong temperature (see PrinterConfig.bed_type)."""
        # Same validation posture as add(): reject wrong types outright
        # (before they can reach `.strip()` and raise a raw AttributeError,
        # or reach PrinterConfig and get silently coerced); cosmetic fields
        # default instead of raising.
        if host is not None:
            if not isinstance(host, str):
                raise ValueError(f"host must be a string, got "
                                f"{type(host).__name__}")
            if not host.strip():
                raise ValueError("host must not be empty")
        if access_code is not None and not isinstance(access_code, str):
            raise ValueError(f"access_code must be a string, got "
                            f"{type(access_code).__name__}")
        if name is not None and not isinstance(name, str):
            name = ""
        if capture is not None and not isinstance(capture, bool):
            capture = False

        with self._lock:
            cfg = self._configs.get(serial)
            if cfg is None:
                return None
            # access_code is a secret the client never receives back, so a
            # blank/omitted value means "keep the current one", not "wipe it".
            new_code = (access_code or "").strip()
            reconnect = ((host is not None and host.strip() != cfg.host) or
                        bool(new_code and new_code != cfg.access_code))
            if host is not None:
                cfg.host = host.strip()
            if new_code:
                cfg.access_code = new_code
            if name is not None:
                cfg.name = name.strip() or cfg.host
            # None = "not submitted, keep what's there"; "" = the user chose
            # Unknown, which disables the check. Distinct on purpose.
            if model_id is not None:
                cfg.model_id = model_id.strip() if isinstance(model_id, str) else ""
            if bed_type is not None:
                cfg.bed_type = bed_type if bed_type in BED_TYPES else DEFAULT_BED_TYPE
            if capture is not None:
                if capture:
                    self._clear_capture()
                cfg.capture = bool(capture)
            old_svc = None
            if reconnect:
                # Factory is called under the lock -- same reasoning as
                # add(): it only builds an in-memory object, never touches a
                # socket. start()/stop() are not, and run outside the lock
                # below.
                old_svc = self._services.get(serial)
                svc = self._factory(cfg)
                self._services[serial] = svc
            else:
                # PrinterService/MockPrinter each keep their own
                # name/capture/model_id copy independent of cfg (see
                # _clear_capture()'s docstring), so the live service must be
                # told explicitly. Miss one and the config is right while
                # every summary keeps reporting the old value.
                svc = self._services[serial]
                svc.name = cfg.name
                svc.capture = cfg.capture
                svc.model_id = cfg.model_id

        if reconnect:
            if old_svc is not None:
                old_svc.stop()
            svc.start()
        self._persist()
        return svc.summary()

    # ---------------- reads ----------------

    def get(self, serial: str):
        # A single dict.get() is one atomic operation under the GIL --
        # no iteration involved, so no lock needed here the way summaries()
        # needs one below.
        return self._services.get(serial)

    def summaries(self) -> list[dict]:
        """Must stay non-blocking: this runs on the asyncio event loop for
        every WebSocket tick (see server/main.py).

        The lock is held only long enough to copy the dict values into a
        plain list -- see the module docstring for why that's defense in
        depth rather than a fix for a reproduced crash. svc.summary() itself
        always runs unlocked, after the lock is released, so a slow
        summary() can never block add()/remove() on another printer, and
        add()/remove() never hold this same lock across anything slower
        than a dict write, so this can never block on a slow start()/
        stop()/disk write either.
        """
        with self._lock:
            services = list(self._services.values())
        return [svc.summary() for svc in services]

    def fetch_sd_file(self, serial: str, path: str) -> bytes:
        """Download one file off `serial`'s SD card, hiding the access code
        behind the service exactly like the /files route's svc.list_files()
        call does -- this method's own signature never takes or returns an
        access_code, so there is no channel for the secret to reach a queue
        route or its response.

        get() is a single atomic dict lookup (see get()'s docstring), so,
        like list_files, this never holds self._lock across the blocking
        FTPS call that follows -- svc.fetch_file() always runs unlocked.

        Always raises SdError on failure, unknown serial included, so
        callers get one exception type to handle regardless of which kind of
        failure this is (same contract as sdcard.list_dir/fetch_file).
        """
        svc = self.get(serial)
        if svc is None:
            raise SdError(f"unknown printer {serial}")
        return svc.fetch_file(path)

    def printer_model(self, serial: str) -> str:
        """`serial`'s configured Bambu model id, or "" when the printer is
        unknown or its model was never set. "" means unknown, and unknown
        never blocks a print -- see store.model_mismatch."""
        with self._lock:
            cfg = self._configs.get(serial)
            return cfg.model_id if cfg is not None else ""

    def printer_nozzle(self, serial: str) -> str:
        """`serial`'s configured nozzle diameter, or the default when the
        printer is unknown. Never "" -- callers substitute it straight into a
        profile name, and an empty one would silently build a name that
        matches nothing."""
        with self._lock:
            cfg = self._configs.get(serial)
            return cfg.nozzle if cfg is not None else DEFAULT_NOZZLE

    def printer_bed_type(self, serial: str) -> str:
        """`serial`'s configured build plate, or the default when the printer
        is unknown. Never "" -- run_slice patches this straight into
        curr_bed_type, and an empty string there is indistinguishable from
        never setting it at all, which is the exact defect this field exists
        to fix (see PrinterConfig.bed_type for the measured 35 C-vs-65 C
        consequence)."""
        with self._lock:
            cfg = self._configs.get(serial)
            return cfg.bed_type if cfg is not None else DEFAULT_BED_TYPE

    def reconnect(self, serial: str) -> dict | None:
        """Rebuild `serial`'s connection using the config already stored.
        Returns the new summary, or None if the serial is unknown.

        NOT an edit: nothing about the printer changes, so this never
        persists. It exists because the automatic paths cannot do two things
        -- retry *now* rather than waiting out RETRY_S, and tear down a client
        that has wedged. It does not help when the IP has changed; that needs
        update(), which already rebuilds on a host change.

        Locking follows update()'s shape exactly: the factory runs under the
        lock (it only builds an in-memory object), while stop()/start() run
        OUTSIDE it, because summaries() is called from the event loop on every
        WS tick and must never block behind a connect.

        Sends no command to the printer -- MQTT here is telemetry only, so
        this cannot disturb a running print.
        """
        with self._lock:
            cfg = self._configs.get(serial)
            if cfg is None:
                return None
            old_svc = self._services.get(serial)
            svc = self._factory(cfg)
            self._services[serial] = svc

        if old_svc is not None:
            old_svc.stop()
        svc.start()
        return svc.summary()

    def upload_sd_file(self, serial: str, path: str, data: bytes) -> None:
        """Upload one file to `serial`'s SD card. Write counterpart of
        fetch_sd_file and identical in posture: the access code stays behind
        the service, the lock is never held across the blocking FTPS call,
        and every failure (unknown serial included) is an SdError.
        """
        svc = self.get(serial)
        if svc is None:
            raise SdError(f"unknown printer {serial}")
        svc.upload_file(path, data)

    # ---------------- detection accessors ----------------

    def capture_serial(self):
        with self._lock:
            for serial, cfg in self._configs.items():
                if cfg.capture:
                    return serial
        return None

    def detection_config(self, serial):
        with self._lock:
            cfg = self._configs.get(serial)
            if cfg is None:
                return None
            return {"camera_source": cfg.camera_source,
                    "camera_index": cfg.camera_index, "conf": cfg.conf,
                    "armed_classes": list(cfg.armed_classes),
                    "detect_enabled": cfg.detect_enabled,
                    "roi": list(cfg.roi) if cfg.roi else None}

    def detection_target(self):
        with self._lock:
            for serial, cfg in self._configs.items():
                if cfg.capture and cfg.detect_enabled:
                    return {"serial": serial, "camera_source": cfg.camera_source,
                            "camera_index": cfg.camera_index, "conf": cfg.conf,
                            "host": cfg.host, "access_code": cfg.access_code,
                            "roi": list(cfg.roi) if cfg.roi else None}
        return None

    def update_detection(self, serial, *, camera_source=None, camera_index=None,
                         conf=None, armed_classes=None, detect_enabled=None,
                         roi=_UNSET) -> bool:
        with self._lock:
            cfg = self._configs.get(serial)
            if cfg is None:
                return False
            if camera_source in CAMERA_SOURCES:
                cfg.camera_source = camera_source
            if camera_index is not None:
                cfg.camera_index = max(0, int(camera_index))
            if conf is not None:
                cfg.conf = min(1.0, max(0.0, float(conf)))
            if armed_classes is not None:
                cfg.armed_classes = [c for c in armed_classes
                                     if c in DETECTION_CLASSES] or ["spaghetti"]
            if detect_enabled is not None:
                cfg.detect_enabled = bool(detect_enabled)
            if roi is not _UNSET:
                # Sentinel, not None: None is a MEANINGFUL value here ("clear
                # the ROI, use the whole frame"), so it cannot double as
                # "leave unchanged" the way the other fields do.
                cfg.roi = normalize_roi(roi)
        self._persist()
        return True

    # ---------------- internals ----------------

    def _clear_capture(self) -> None:
        """One webcam -> at most one capture printer. Must be called with
        self._lock already held.

        Mutates the config (the persisted source of truth) AND the live
        service's own `capture` attribute. That second part is load-bearing:
        PrinterService/MockPrinter each keep their own `capture` copy and
        read *that* in summary(), not the config object (unlike this
        module's tests' FakeService, which happens to read straight off
        cfg). Clearing only cfg.capture here would silently leave a real
        service's summary() still reporting capture=True for the old
        printer.
        """
        for serial, cfg in self._configs.items():
            if cfg.capture:
                cfg.capture = False
                svc = self._services.get(serial)
                if svc is not None:
                    svc.capture = False

    def _persist(self) -> None:
        """Serialized through `_persist_lock`, a lock separate from
        `self._lock`.

        Two concurrent add()/remove() calls each mutate the dicts under
        self._lock and then call _persist() afterwards, unlocked. Without a
        lock *here*, their store.save() calls can race: thread A takes its
        snapshot, then stalls on disk I/O; meanwhile thread B mutates, takes
        a newer snapshot, and finishes writing first; A's now-stale write
        then lands last and silently drops B's printer from printers.json,
        even though B's printer is alive and running in memory. That is a
        real, silent data-loss bug, not a cosmetic one -- restart the server
        at the wrong moment and the "lost" printer never comes back.

        Serializing snapshot+write through _persist_lock fixes it: whichever
        write finishes last is guaranteed to have taken its snapshot no
        earlier than every write that finished before it, so the file
        always converges to the latest state.

        self._lock itself is only held for the instant it takes to copy
        _configs.values() into a list -- never across the actual save() --
        so a slow disk write can never block summaries() on the event loop.
        """
        with self._persist_lock:
            with self._lock:
                configs = list(self._configs.values())
            self._store.save(configs)
