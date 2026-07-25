import { useState } from "react";
import Button from "../ui/Button.jsx";

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
    return <p className="muted">
      No pieces yet — they are created when the run ends.</p>;
  }

  return (
    <div className="stack">
      <div className="row">
        <input value={inspector} placeholder="Inspected by"
               onChange={(e) => setInspector(e.target.value)} />
        {STATUSES.slice(0, 3).map(([value, label]) => (
          <Button key={value} disabled={busy}
                  onClick={() => onBulk(value, inspector)}>
            All {label.toLowerCase()}
          </Button>
        ))}
      </div>
      <table className="table">
        <thead>
          <tr><th>#</th><th>Status</th><th>Badges</th><th>Inspected by</th></tr>
        </thead>
        <tbody>
          {pieces.map((piece) => (
            <tr key={piece.id}>
              <td>{piece.index_in_run}</td>
              <td>
                <select value={piece.status} disabled={busy}
                        onChange={(e) => onSetPiece(
                          piece.id, e.target.value, inspector)}>
                  {STATUSES.map(([value, label]) => (
                    <option key={value} value={value}>{label}</option>
                  ))}
                </select>
              </td>
              <td>{(piece.badges ?? []).map((b) => b.label).join(", ") || "—"}</td>
              <td>{piece.inspected_by ?? "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
