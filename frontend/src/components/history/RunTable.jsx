import EmptyState from "../ui/EmptyState.jsx";
import { formatDuration, elapsedSeconds, runOutcome } from "./runFormat.js";

// The run list for one printer. Selection is owned by the page above.
// Piece counts come from the server (run.piece_counts) rather than being
// derived here: the list endpoint deliberately does not ship every piece row.
export default function RunTable({ runs, selectedId, onSelect }) {
  if (!runs.length) {
    return (
      <EmptyState title="No runs recorded yet">
        Runs record themselves. The server watches this printer and opens a run
        the moment it starts printing — whether the job came from the queue,
        from the SD card or from the printer's own screen. Print something and
        it is listed here, with its outcome and the pieces it produced.
      </EmptyState>
    );
  }
  return (
    // Selectable: a click on a row loads it into the detail below, so the row
    // gets the pointer and the hover that promise it.
    <table className="table table--selectable">
      <thead>
        <tr>
          <th>Started</th>
          <th>File</th>
          <th>Outcome</th>
          <th className="table__num">Layers</th>
          <th className="table__num">Time</th>
          <th className="table__num">Grams</th>
          <th className="table__num">Pieces</th>
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
              <td className="table__num">
                {run.last_layer ?? "—"}/{run.total_layers ?? "—"}</td>
              <td className="table__num">{formatDuration(
                elapsedSeconds(run.started_at, run.ended_at))}</td>
              <td className="table__num">{run.actual_grams == null
                ? "—"
                : `${run.actual_grams} g`}
                {/* The tilde marks grams the server worked out (planned or
                    proportional basis) rather than a figure somebody weighed
                    and typed in, so one column can carry both without the
                    estimate passing for a measurement. */}
                {run.actual_grams_basis && run.actual_grams_basis !== "manual"
                  ? <span className="muted"
                          title={`Estimated (${run.actual_grams_basis})`}>
                      {" ~"}
                    </span>
                  : ""}</td>
              <td className="table__num">
                {rollup.total ? `${rollup.good}/${rollup.total}` : "—"}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}
