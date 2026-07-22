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
  const mismatch = (job) =>
    printerModelId && job.model_id && printerModelId !== job.model_id
      ? `Sliced for ${modelName(job.model_id)}, but this printer is `
        + `${modelName(printerModelId)}. It cannot be started here.`
      : null;
  return (
    <table className="queue-table">
      <thead>
        <tr>
          <th>Name</th>
          <th>Time</th>
          <th>Filament</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        {jobs.map((job, i) => (
          <tr key={job.id}>
            <td>
              {job.name}
              {mismatch(job) && (
                <span className="queue-table__warn" title={mismatch(job)}>
                  ⚠ {modelName(job.model_id)}
                </span>
              )}
            </td>
            <td className="queue-table__num">{formatDuration(job.seconds)}</td>
            <td className="queue-table__num">{formatGrams(job.grams)}</td>
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
        ))}
      </tbody>
    </table>
  );
}
