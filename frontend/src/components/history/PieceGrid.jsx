import { useState } from "react";
import Button from "../ui/Button.jsx";
import EmptyState from "../ui/EmptyState.jsx";
import Field from "../ui/Field.jsx";

const STATUSES = [
  ["good", "Good"],
  ["rework", "Rework"],
  ["scrap", "Scrap"],
  ["pending_inspection", "Pending"],
];

// Piece verdicts for one run. The bulk row is the primary control: a plate of
// eight has to be confirmable in one action, or the verdicts stop being
// entered at all and piece-level traceability becomes fiction.
export default function PieceGrid({ pieces, busy, onBulk, onSetPiece }) {
  const [inspector, setInspector] = useState("");

  if (!pieces.length) {
    return (
      <EmptyState title="No pieces yet">
        Pieces are created when the run ends, one row per part on the plate.
        Until then there is nothing to pass or fail.
      </EmptyState>
    );
  }

  return (
    <div className="stack">
      {/* The bulk strip sits above the table and carries the one primary
          button on the page, because "all good" is the verdict a finished
          plate usually gets and the table below is the slow way to say it. */}
      <div className="row">
        <Field label="Inspected by" value={inspector}
               placeholder="Name or initials"
               help="Saved with every verdict you set here."
               onChange={(e) => setInspector(e.target.value)} />
        {STATUSES.slice(0, 3).map(([value, label]) => (
          <Button key={value} size="sm" disabled={busy}
                  variant={value === "good" ? "primary" : "secondary"}
                  onClick={() => onBulk(value, inspector)}>
            All {label.toLowerCase()}
          </Button>
        ))}
        <span className="muted">
          Sets all {pieces.length} pieces at once.</span>
      </div>
      <table className="table">
        <thead>
          <tr>
            <th className="table__num">#</th>
            <th>Status</th><th>Badges</th><th>Inspected by</th>
          </tr>
        </thead>
        <tbody>
          {pieces.map((piece) => (
            <tr key={piece.id}>
              <td className="table__num">{piece.index_in_run}</td>
              <td>
                <select value={piece.status} disabled={busy}
                        aria-label={`Piece ${piece.index_in_run} verdict`}
                        onChange={(e) => onSetPiece(
                          piece.id, e.target.value, inspector)}>
                  {STATUSES.map(([value, label]) => (
                    <option key={value} value={value}>{label}</option>
                  ))}
                </select>
              </td>
              {/* Pills, not a comma-joined string: a piece's badge is the same
                  kind of fact as a run's, so it gets the same amber badge here
                  as it does in RunDetail rather than reading as prose. */}
              <td>{(piece.badges ?? []).length
                ? (piece.badges ?? []).map((b) => (
                    <span key={b.badge_id} className="pill pill-warn">
                      {b.label}</span>
                  ))
                : "—"}</td>
              <td>{piece.inspected_by ?? "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
