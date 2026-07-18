import { formatDuration, formatGrams } from "./format.js";

// jobs: [{id, sd_path, name, seconds, grams, source}]. busyId disables every
// row's controls while any one mutation (remove/reorder) is in flight --
// simpler than per-button state, and the table is small enough that a brief
// blanket-disable is unnoticeable.
export default function QueueTable({ jobs, busyId, onRemove, onMove }) {
  const busy = busyId != null;
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
            <td>{job.name}</td>
            <td className="queue-table__num">{formatDuration(job.seconds)}</td>
            <td className="queue-table__num">{formatGrams(job.grams)}</td>
            <td>
              <div className="queue-table__actions">
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
