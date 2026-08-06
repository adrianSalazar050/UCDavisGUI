import { modelName } from "../printers/printerModels.js";
import { formatDuration, formatGrams } from "./format.js";

// jobs: [{id, sd_path, name, seconds, grams, source}]. busyId disables every
// row's controls while any one mutation (remove/reorder) is in flight --
// simpler than per-button state, and the table is small enough that a brief
// blanket-disable is unnoticeable.
//
// Only the FIRST row gets a Print button: the server refuses to start anything
// but the head of the queue, so offering it elsewhere would just invite a 409.
// Reorder with ↑↓ to choose what prints next. `canStart` is false whenever the
// printer is disconnected or already printing.
export default function QueueTable({ jobs, busyId, onRemove, onMove, onStart,
                                     canStart, startBlockedReason,
                                     printerModelId }) {
  const busy = busyId != null;
  // Mirrors server/store.py's model_mismatch, including the rule that
  // matters most: unknown on EITHER side is not a mismatch. The server is
  // still the one that refuses the start; this only explains it up front.
  //
  // The second sentence names both ways out, and deliberately does NOT offer
  // the topbar printer switcher as one of them. That reads like the obvious
  // advice -- the file is startable one printer over -- but it cannot work:
  // queues are per-printer AND this .3mf sits on THIS printer's microSD card,
  // so switching lands on another printer's queue, which holds neither this
  // job nor this file. The only two exits are re-slicing for the printer we
  // are pointed at, or putting the file on the other printer's card and
  // queueing it over there.
  const mismatch = (job) =>
    printerModelId && job.model_id && printerModelId !== job.model_id
      ? `Sliced for ${modelName(job.model_id)}, but this printer is `
        + `${modelName(printerModelId)}. It cannot be started here — re-slice `
        + `it for ${modelName(printerModelId)}, or upload the file to the `
        + `${modelName(job.model_id)}’s own microSD card and queue it there.`
      : null;
  return (
    <table className="table">
      <thead>
        <tr>
          <th>Job</th>
          <th className="table__num">Time</th>
          <th className="table__num">Filament</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        {jobs.map((job, i) => {
          const warn = mismatch(job);
          return (
            // A mismatched row is tinted as well as badged: the badge carries
            // the full reason in a title attribute, which a glance never sees,
            // and this is the one row in the queue that cannot print here.
            <tr key={job.id} className={warn ? "row-warn" : undefined}>
              <td>
                {job.name}
                {warn && (
                  <span className="queue-table__warn" title={warn}>
                    ⚠ Sliced for {modelName(job.model_id)}
                  </span>
                )}
              </td>
              <td className="table__num">{formatDuration(job.seconds)}</td>
              <td className="table__num">{formatGrams(job.grams)}</td>
              <td>
                <div className="queue-table__actions">
                  {i === 0 && (
                    <button type="button" className="queue-table__start"
                            disabled={busy || !canStart}
                            title={canStart ? `Print ${job.name} now`
                                            : startBlockedReason}
                            aria-label={`Print ${job.name} now`}
                            onClick={() => onStart(job)}>
                      ▶ Print
                    </button>
                  )}
                  <button type="button" className="queue-table__move"
                          disabled={busy || i === 0}
                          aria-label={`Move ${job.name} up`}
                          onClick={() => onMove(i, -1)}>
                    ↑
                  </button>
                  <button type="button" className="queue-table__move"
                          disabled={busy || i === jobs.length - 1}
                          aria-label={`Move ${job.name} down`}
                          onClick={() => onMove(i, 1)}>
                    ↓
                  </button>
                  <button type="button" className="queue-table__remove"
                          disabled={busy}
                          aria-label={`Remove ${job.name}`}
                          onClick={() => onRemove(job.id)}>
                    ✕
                  </button>
                </div>
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}
