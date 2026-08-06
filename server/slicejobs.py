"""Slice jobs: submit a model, slice it, upload it, queue it.

ONE WORKER, GLOBALLY -- not one per printer. Slicing pegs a core, and this
server also supervises a YOLO detector process (master.md 2). A per-printer
worker would let a three-printer fleet start three slices at once and starve
detection, which is the one thing on this box that has to stay responsive.

Jobs are RUNTIME-ONLY and never persisted, the same reasoning as "arm is
runtime-only" (master.md 4.5): they are transient work, and a half-finished
slice pointing at a deleted temp directory must not survive a restart. The
RESULT is durable -- it lands on the microSD and in queues.json.
"""
from __future__ import annotations

import logging
import pathlib
import posixpath
import shutil
import threading
import time
import uuid

from . import slicepresets, threemf
from .slicer import SliceError, bed_dimensions, flatten_profile, run_slice

log = logging.getLogger("server.slicejobs")

TICK_S = 0.25

# Extensions the slicer can load. Anything else is refused at the boundary.
MODEL_EXTS = (".stl", ".3mf", ".step", ".stp", ".obj")

# Jobs are runtime-only (module docstring) and nothing here ever clears one
# except cancel()'s "clear a finished job" path -- so a server nobody touches
# the job list on accumulates one record per slice ever submitted, forever.
# Bounded here: once a job reaches a TERMINAL state, the oldest terminal
# records beyond this cap are evicted. A queued/slicing/uploading job is
# never touched by this regardless of the cap -- see _evict_excess_finished.
MAX_FINISHED_JOBS = 50

# States a job can be evicted from once the cap is exceeded. Anything else
# (queued/slicing/uploading) is live work, not history.
_TERMINAL_STATES = ("done", "failed", "cancelled")


def output_name(filename: str) -> str:
    """An uploaded model filename -> the .gcode.3mf name to write to the card.

    Takes the basename and discards any directory component, for the same two
    reasons the SD upload route does (master.md 3.2): file:///sdcard/<name>
    has no path component, so a file in a subdirectory could be listed but
    never started -- and stripping the directory also makes a traversal
    attempt land harmlessly at the root.
    """
    base = posixpath.basename((filename or "").replace("\\", "/")).strip()
    stem = base
    for ext in MODEL_EXTS:
        if stem.lower().endswith(ext):
            stem = stem[: -len(ext)]
            break
    return f"{stem or 'model'}.gcode.3mf"


class SliceCoordinator:
    """Owns the job list and the single worker thread.

    `run` and `parse` are injectable so the whole state machine tests with no
    slicer, no printer and no camera -- the same seam design as
    DetectorSupervisor's `spawn` and the registry's `service_factory`.
    """

    def __init__(self, registry, queue, slicer_exe, index, *, work_dir,
                 run=run_slice, parse=threemf.parse_slice_info,
                 clock=time.time, max_finished_jobs=MAX_FINISHED_JOBS):
        self._registry = registry
        self._queue = queue
        self._exe = slicer_exe
        self._index = index
        self._work_dir = pathlib.Path(work_dir)
        self._run = run
        self._parse = parse
        self._clock = clock
        self._max_finished_jobs = max_finished_jobs
        self._lock = threading.Lock()
        self._jobs: dict = {}
        self._order: list = []
        self._pending: list = []
        self._thread = None
        self._stop = threading.Event()

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        # Liveness, not identity with None: stop() can deliberately leave
        # self._thread pointing at a thread that is still alive (see stop()'s
        # comment below) specifically so THIS check catches it. Comparing to
        # None instead would let a start() called after a timed-out stop()
        # spawn a SECOND worker on top of one still running a slice --
        # exactly the "starves the YOLO detector" scenario the module
        # docstring's single-global-worker design exists to prevent.
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="slice-worker")
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the worker to stop and wait up to `timeout` seconds for it
        to actually exit. `timeout` is a parameter (not a bare constant) so
        tests can drive this deterministically fast without waiting out the
        real default.
        """
        self._stop.set()
        thread = self._thread
        if thread is None:
            return
        thread.join(timeout=timeout)
        if thread.is_alive():
            # run_slice allows the CLI subprocess up to SLICE_TIMEOUT_S
            # (900s), so a stop() called mid-slice can easily outlive this
            # join -- the worker is still inside _do(), running a subprocess
            # there is no safe way to interrupt (killing it mid-write is how
            # you get a truncated file on a printer's microSD, the same
            # reasoning cancel() already applies to a job that's already
            # slicing). Do NOT clear self._thread here: doing so would make
            # start()'s liveness check see None and spawn a second worker
            # while this one is still alive -- two slices running at once,
            # defeating the entire point of the single global worker. Same
            # class of bug DetectorSupervisor._stop_proc guards against for
            # its own subprocess: never let go of a handle to something you
            # only ASKED to stop, only to something that actually did.
            log.warning(
                "slice worker did not stop within %.0fs (a slice is likely "
                "still running); leaving the reference in place so a later "
                "start() cannot spawn a second worker on top of it", timeout)
            return
        self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:            # never let one bad job kill the loop
                log.exception("slice worker tick failed")
            self._stop.wait(TICK_S)

    # -- api ---------------------------------------------------------------

    def options(self, serial: str) -> dict:
        """Presets and filaments that actually resolve for this printer, plus
        the detected filament."""
        model_id = self._registry.printer_model(serial)
        nozzle = self._registry.printer_nozzle(serial)
        svc = self._registry.get(serial)
        detected = slicepresets.detect_loaded_filament(
            getattr(svc, "state", None))
        # Bed size for the STL viewer's plate. Resolved from the machine
        # profile's printable_area; None (unknown) when the model/nozzle
        # doesn't resolve a machine profile -- the viewer then draws a default
        # plate rather than blocking.
        machine_name = slicepresets.machine_profile_name(model_id, nozzle)
        bed = None
        if machine_name and machine_name in self._index:
            try:
                bed = bed_dimensions(flatten_profile(machine_name, self._index))
            except SliceError:
                bed = None
        return {
            "model_id": model_id,
            "nozzle": nozzle,
            "presets": slicepresets.available_presets(model_id, nozzle,
                                                      self._index),
            "filaments": slicepresets.available_filaments(model_id,
                                                          self._index),
            "detected_filament": detected,
            "bed": bed,
        }

    def submit(self, serial, filename, data, tier_id, material,
               supports, *, part_id=None, recipe_id=None) -> str:
        """Queue a slice. Raises KeyError (unknown printer) or ValueError
        (unusable preset/filament) -- both are 4xx at the route, and both are
        checked HERE so a doomed job never reaches the worker.

        part_id/recipe_id are optional provenance: when a slice is submitted
        for a stored part+recipe (rather than an ad-hoc upload), they ride
        along on the job so the eventual queue entry -- and, once printed,
        the run row -- is attributed back to the part."""
        if self._registry.get(serial) is None:
            raise KeyError(serial)
        model_id = self._registry.printer_model(serial)
        nozzle = self._registry.printer_nozzle(serial)
        preset = slicepresets.resolve_preset(tier_id, model_id, nozzle,
                                             self._index)
        if preset is None:
            raise ValueError(
                f"no {tier_id!r} preset for this printer -- check its model "
                f"and nozzle ({nozzle} mm) on the Printers page")
        # filament_profile_name resolves against the index and returns "" if
        # nothing matches, so the in-index check it used to need is now inside.
        filament = slicepresets.filament_profile_name(
            material, model_id, self._index)
        if not filament:
            raise ValueError(f"no profile for {material!r} on this printer")

        job_id = uuid.uuid4().hex
        job = {
            "id": job_id, "serial": serial, "state": "queued",
            "name": output_name(filename), "source_name": filename,
            "preset": preset["id"], "preset_label": preset["label"],
            "material": material, "supports": bool(supports),
            # Filled in by _do(), same as seconds/grams -- the printer's
            # CONFIGURED plate (registry.printer_bed_type) is what actually
            # gets patched into curr_bed_type, so this is read fresh there
            # rather than captured now, exactly like model_id is re-read in
            # _do() rather than reused from submit() time.
            "bed_type": None,
            "created": self._clock(), "error": None,
            "seconds": None, "grams": None, "sd_path": None,
            "part_id": part_id, "recipe_id": recipe_id,
        }
        with self._lock:
            self._jobs[job_id] = job
            self._order.append(job_id)
            self._pending.append((job_id, preset, filament, data))
        return job_id

    def get(self, job_id) -> dict | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return dict(job) if job else None

    def list(self, serial=None) -> list:
        with self._lock:
            jobs = [dict(self._jobs[i]) for i in reversed(self._order)]
        return [j for j in jobs if serial is None or j["serial"] == serial]

    def cancel(self, job_id) -> bool:
        """Cancel a queued job or clear a finished one. A job already being
        sliced is left alone -- killing the subprocess mid-write is how you
        get a truncated file on the card."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return False
            if job["state"] == "queued":
                job["state"] = "cancelled"
                self._pending = [p for p in self._pending if p[0] != job_id]
                self._evict_excess_finished()   # cancelled is terminal too
                return True
            if job["state"] in _TERMINAL_STATES:
                self._jobs.pop(job_id, None)
                self._order.remove(job_id)
                return True
            return False

    # -- the work ----------------------------------------------------------

    def run_once(self) -> None:
        """Process at most one pending job. Public so tests drive the whole
        state machine synchronously, with no thread involved."""
        with self._lock:
            if not self._pending:
                return
            job_id, preset, filament_name, data = self._pending.pop(0)
            job = self._jobs.get(job_id)
            if job is None or job["state"] != "queued":
                return          # cancelled between submit and now
            job["state"] = "slicing"
            serial, name, supports = job["serial"], job["name"], job["supports"]
            source_name = job["source_name"]
            part_id = job.get("part_id")
            recipe_id = job.get("recipe_id")

        work = self._work_dir / job_id
        try:
            self._do(job_id, serial, name, source_name, preset, filament_name,
                     data, supports, work, part_id=part_id, recipe_id=recipe_id)
        except Exception as e:
            # log.exception (not log.warning) so the traceback always lands
            # in the log, not just a bare "%s" of the message. The catch-all
            # here has to cover both an ORDINARY, expected failure (a bad
            # model -> SliceError) and a genuine bug in this code
            # (AttributeError, KeyError) -- without the traceback the two are
            # indistinguishable and a real defect reads as just another
            # unslicable model.
            #
            # Deliberately NOT split into "known-domain" vs "unexpected"
            # exception types: the upload step's failure shape isn't a fixed
            # set to whitelist against -- svc.upload_file/FTPS can raise
            # SdError, but also a bare socket/OSError or anything else the
            # underlying ftplib throws (this module's own tests stand a bare
            # RuntimeError in for it, on purpose). A hand-maintained
            # whitelist would either miss a real category -- silently
            # downgrading a genuine bug back to "just a slice failure" and
            # losing its traceback anyway -- or need constant upkeep as the
            # printer link changes. log.exception costs only log verbosity
            # and never throws away the one clue that might explain a bug.
            log.exception("slice job %s failed", job_id)
            self._finish(job_id, "failed", error=str(e))
        finally:
            # Always: a work directory left behind holds a whole gcode file,
            # and this runs on every path including cancellation.
            shutil.rmtree(work, ignore_errors=True)

    def _do(self, job_id, serial, name, source_name, preset, filament_name,
            data, supports, work, *, part_id=None, recipe_id=None) -> None:
        work.mkdir(parents=True, exist_ok=True)
        # The plan's original line here computed this as
        # `work / posixpath.basename(...) or work / "model.stl"` -- operator
        # precedence makes `or` bind to the whole `work / ...` Path, which is
        # always truthy, so the fallback was dead code. Worse, if the source
        # name's basename is empty the intended fallback path is never taken
        # and the model would be written to `work` itself (a directory) not a
        # file. Compute the basename (with its own fallback) FIRST, then join
        # it onto `work` exactly once.
        basename = posixpath.basename(source_name.replace("\\", "/")).strip()
        model_path = work / (basename or "model.stl")
        model_path.write_bytes(data)

        machine = flatten_profile(preset["machine"], self._index)
        process = flatten_profile(preset["process"], self._index)
        filament = flatten_profile(filament_name, self._index)

        # MEASURED 2026-07-22: with curr_bed_type unset, Bambu Studio
        # defaults to Cool Plate (35 C for PLA); this lab's A1 has a Textured
        # PEI Plate (65 C) and a print with no bed adhesion stalled at 5%
        # with an HMS warning. The printer cannot report which plate is
        # fitted (see store.py's BED_TYPES), so it must be read from the
        # printer's CONFIGURATION, not assumed -- same reasoning as
        # printer_model above.
        bed_type = self._registry.printer_bed_type(serial)
        self._set(job_id, bed_type=bed_type)

        produced = self._run(self._exe, model_path, machine, process, filament,
                             work, supports=supports, bed_type=bed_type)
        sliced = pathlib.Path(produced).read_bytes()

        meta = self._parse(sliced)
        self._set(job_id, state="uploading", seconds=meta.get("seconds"),
                  grams=meta.get("grams"))

        target = "/" + name
        self._registry.upload_sd_file(serial, target, sliced)

        seconds, grams = meta.get("seconds"), meta.get("grams")
        self._queue.add(serial, {
            "id": uuid.uuid4().hex,
            "sd_path": target,
            "name": name,
            "seconds": seconds,
            "grams": grams,
            "source": "3mf" if (seconds or grams) else "manual",
            # PROVENANCE, not the file. The CLI omits printer_model_id, so a
            # sliced file would otherwise skip the model guard entirely
            # (master.md 5.3). We know which printer we sliced for.
            "model_id": self._registry.printer_model(serial) or None,
            "part_id": part_id, "recipe_id": recipe_id,
        })
        self._finish(job_id, "done", sd_path=target)

    def _set(self, job_id, **fields) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                job.update(fields)

    def _finish(self, job_id, state, **fields) -> None:
        self._set(job_id, state=state, **fields)
        with self._lock:
            self._evict_excess_finished()

    def _evict_excess_finished(self) -> None:
        """Must be called with self._lock already held (the same
        caller-holds-the-lock convention `registry.py`'s internals use).

        Keeps at most `self._max_finished_jobs` TERMINAL (done/failed/
        cancelled) records, evicting the OLDEST ones first. `self._order` is
        append order, i.e. oldest first, so a single left-to-right filter
        finds the terminal ones in eviction order already -- no sort needed.
        A queued/slicing/uploading job never appears in `finished_ids` at
        all, so it can never be evicted here no matter how far past the cap
        the total job count grows -- only history is bounded, never live
        work.
        """
        finished_ids = [jid for jid in self._order
                        if self._jobs[jid]["state"] in _TERMINAL_STATES]
        excess = len(finished_ids) - self._max_finished_jobs
        if excess <= 0:
            return
        for jid in finished_ids[:excess]:
            self._jobs.pop(jid, None)
            self._order.remove(jid)
