import { formatDuration, elapsedSeconds, runOutcome } from "./runFormat.js";

// The run list for one printer. Selection is owned by the page above.
// Piece counts come from the server (run.piece_counts) rather than being
// derived here: the list endpoint deliberately does not ship every piece row.
export default function RunTable({ runs, selectedId, onSelect }) {
  if (!runs.length) {
    return <p className="muted">No runs recorded yet.</p>;
  }
  return (
    <table className="table">
      <thead>
        <tr>
          <th>Started</th><th>File</th><th>Outcome</th>
          <th>Layers</th><th>Time</th><th>Grams</th><th>Pieces</th>
        </tr>
      </thead>
      <tbody>
        {runs.map((run) => {
          const outcome = runOutcome(run);
          const rollup = run.piece_counts ?? { total: 0, good: 0 };
          return (
            <tr key={run.id}
                onClick={() => onSelect(run.id)}
                className={run.id === selectedId ? "selected" : ""}>
              <td>{(run.started_at ?? "").replace("T", " ").slice(0, 16)}</td>
              <td>{run.subtask_name ?? run.sd_path ?? "—"}</td>
              <td><span className={`pill pill-${outcome.tone}`}>
                {outcome.label}</span></td>
              <td>{run.last_layer ?? "—"}/{run.total_layers ?? "—"}</td>
              <td>{formatDuration(
                elapsedSeconds(run.started_at, run.ended_at))}</td>
              <td>{run.actual_grams == null
                ? "—"
                : `${run.actual_grams} g`}
                {run.actual_grams_basis && run.actual_grams_basis !== "manual"
                  ? " ~" : ""}</td>
              <td>{rollup.total ? `${rollup.good}/${rollup.total}` : "—"}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}
