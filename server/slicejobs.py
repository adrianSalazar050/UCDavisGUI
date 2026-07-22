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
from .slicer import SliceError, flatten_profile, run_slice

log = logging.getLogger("server.slicejobs")

TICK_S = 0.25

# Extensions the slicer can load. Anything else is refused at the boundary.
MODEL_EXTS = (".stl", ".3mf", ".step", ".stp", ".obj")


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
                 clock=time.time):
        self._registry = registry
        self._queue = queue
        self._exe = slicer_exe
        self._index = index
        self._work_dir = pathlib.Path(work_dir)
        self._run = run
        self._parse = parse
        self._clock = clock
        self._lock = threading.Lock()
        self._jobs: dict = {}
        self._order: list = []
        self._pending: list = []
        self._thread = None
        self._stop = threading.Event()

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="slice-worker")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=5.0)

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
        return {
            "model_id": model_id,
            "nozzle": nozzle,
            "presets": slicepresets.available_presets(model_id, nozzle,
                                                      self._index),
            "filaments": slicepresets.available_filaments(model_id,
                                                          self._index),
            "detected_filament": detected,
        }

    def submit(self, serial, filename, data, tier_id, material,
               supports) -> str:
        """Queue a slice. Raises KeyError (unknown printer) or ValueError
        (unusable preset/filament) -- both are 4xx at the route, and both are
        checked HERE so a doomed job never reaches the worker."""
        if self._registry.get(serial) is None:
            raise KeyError(serial)
        model_id = self._registry.printer_model(serial)
        nozzle = self._registry.printer_nozzle(serial)
        preset = slicepresets.resolve_preset(tier_id, model_id, nozzle,
                                             self._index)
        if preset is None:
            raise ValueError(
                f"no {tier_id!r} preset for this printer -- check its model "
                f"and nozzle ({nozzle} mm) on the Overview page")
        filament = slicepresets.filament_profile_name(material, model_id)
        if not filament or filament not in self._index:
            raise ValueError(f"no profile for {material!r} on this printer")

        job_id = uuid.uuid4().hex
        job = {
            "id": job_id, "serial": serial, "state": "queued",
            "name": output_name(filename), "source_name": filename,
            "preset": preset["id"], "preset_label": preset["label"],
            "material": material, "supports": bool(supports),
            "created": self._clock(), "error": None,
            "seconds": None, "grams": None, "sd_path": None,
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
                return True
            if job["state"] in ("done", "failed", "cancelled"):
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

        work = self._work_dir / job_id
        try:
            self._do(job_id, serial, name, source_name, preset, filament_name,
                     data, supports, work)
        except Exception as e:
            log.warning("slice job %s failed: %s", job_id, e)
            self._finish(job_id, "failed", error=str(e))
        finally:
            # Always: a work directory left behind holds a whole gcode file,
            # and this runs on every path including cancellation.
            shutil.rmtree(work, ignore_errors=True)

    def _do(self, job_id, serial, name, source_name, preset, filament_name,
            data, supports, work) -> None:
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

        produced = self._run(self._exe, model_path, machine, process, filament,
                             work, supports=supports)
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
        })
        self._finish(job_id, "done", sd_path=target)

    def _set(self, job_id, **fields) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                job.update(fields)

    def _finish(self, job_id, state, **fields) -> None:
        self._set(job_id, state=state, **fields)
