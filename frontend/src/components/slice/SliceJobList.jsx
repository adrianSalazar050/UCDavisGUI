import { useCallback, useEffect, useRef, useState } from "react";
import { cancelSliceJob, fetchSliceJobs } from "../../api/printer.js";
import { formatDuration, formatGrams } from "../queue/format.js";
import Button from "../ui/Button.jsx";
import EmptyState from "../ui/EmptyState.jsx";
import StatusPill from "../ui/StatusPill.jsx";

const POLL_MS = 2000;

// state -> StatusPill status. queued/slicing/uploading are all "in progress",
// same warn color; done is ok; failed is danger; cancelled reads as a settled
// non-error the same way a removed queue job does, so it stays warn too.
const PILL_STATUS = {
  queued: "warn", slicing: "warn", uploading: "warn",
  done: "ok", failed: "danger", cancelled: "warn",
};

// Only a queued job can be cancelled, and only a terminal one (done/failed/
// cancelled) can be cleared -- a job actually slicing or uploading is left
// alone (server/slicejobs.py: killing the subprocess mid-write is how you get
// a truncated file on the card), so there is no button to offer for it.
const CANCELLABLE = new Set(["queued", "done", "failed", "cancelled"]);

// Polls the slice job list for one printer. `refreshSignal` lets the parent
// (Slice.jsx) force an immediate reload right after a submit, instead of
// waiting out the poll interval for the new `queued` row to appear.
//
// Same requestId ref guard as QueuePanel (Queue.jsx): only the most recently
// issued request may write to state, so a slow response for an old poll can
// never clobber what a newer one already wrote.
export default function SliceJobList({ serial, refreshSignal }) {
  const [jobs, setJobs] = useState([]);
  const [err, setErr] = useState(null);
  const [initialLoaded, setInitialLoaded] = useState(false);
  const [busyId, setBusyId] = useState(null);

  const requestId = useRef(0);

  const load = useCallback(async () => {
    const id = (requestId.current += 1);
    try {
      const data = await fetchSliceJobs(serial);
      if (id === requestId.current) {
        setJobs(data);
        setErr(null);
      }
    } catch (e) {
      if (id === requestId.current) setErr(e.message);
    } finally {
      if (id === requestId.current) setInitialLoaded(true);
    }
  }, [serial]);

  // refreshSignal is listed as a dependency purely to retrigger this effect
  // (and so re-run `load` once immediately) right after a submit -- its value
  // is never read.
  useEffect(() => {
    load();
    const iid = setInterval(load, POLL_MS);
    return () => clearInterval(iid);
  }, [load, refreshSignal]);

  const handleCancel = async (id) => {
    setBusyId(id);
    setErr(null);
    try {
      await cancelSliceJob(id);
      await load();
    } catch (e) {
      setErr(e.message);
    } finally {
      setBusyId(null);
    }
  };

  // Same shape as QueuePanel: an error from one bad poll sits ABOVE whatever
  // was last successfully loaded, rather than blanking a perfectly good table
  // -- a single transient poll failure must not flash the list away.
  return (
    <>
      {err && (
        <div className="state-error">
          <span>{err}</span>
          <Button size="sm" onClick={load}>Retry</Button>
        </div>
      )}
      {!initialLoaded ? (
        <EmptyState title="Loading slice jobs…" />
      ) : jobs.length === 0 ? (
        <EmptyState title="No slice jobs yet">
          Slice a model above and it appears here while it slices and uploads,
          then stays as a record of what was made.
        </EmptyState>
      ) : (
        // Still .slice-table, not .table: only .slice-table top-aligns its
        // cells, which is what keeps a row level with the first line of a
        // failed job's error block.
        <table className="slice-table">
          <thead>
            <tr>
              {/* job.name is the .gcode.3mf the slicer writes to the card
                  (server/slicejobs.py output_name), not the model that went
                  in -- so the column says so rather than "Name". */}
              <th>Output file</th><th>Preset</th><th>Material</th><th>Supports</th>
              <th>State</th><th className="table__num">Print time</th>
              <th className="table__num">Filament used</th><th></th>
            </tr>
          </thead>
          <tbody>
            {jobs.map((job) => (
              <tr key={job.id}>
                <td>
                  {job.name}
                  {job.state === "failed" && job.error && (
                    <pre className="slice-table__error">{job.error}</pre>
                  )}
                </td>
                <td>{job.preset_label}</td>
                <td>{job.material}</td>
                {/* "No" is the common answer and the one nobody reads, so it
                    steps back and lets "Yes" stand out down the column. */}
                <td>
                  {job.supports ? "Yes" : <span className="muted">No</span>}
                </td>
                <td>
                  <StatusPill status={PILL_STATUS[job.state] ?? "warn"}>
                    {job.state}
                  </StatusPill>
                </td>
                <td className="table__num">
                  {job.state === "done" ? formatDuration(job.seconds) : "—"}
                </td>
                <td className="table__num">
                  {job.state === "done" ? formatGrams(job.grams) : "—"}
                </td>
                <td>
                  {CANCELLABLE.has(job.state) && (
                    <button type="button" className="slice-table__cancel"
                            disabled={busyId != null}
                            aria-label={job.state === "queued"
                              ? `Cancel ${job.name}` : `Clear ${job.name}`}
                            onClick={() => handleCancel(job.id)}>
                      {job.state === "queued" ? "Cancel" : "Clear"}
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  );
}
