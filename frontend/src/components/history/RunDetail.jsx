import Card from "../ui/Card.jsx";
import PieceGrid from "./PieceGrid.jsx";
import { formatDuration, elapsedSeconds, pieceRollup, runOutcome }
  from "./runFormat.js";

const END_STATES = [
  "FINISH", "FAILED", "STOPPED_BY_MONITOR", "STOPPED_BY_OPERATOR",
  "START_UNCONFIRMED", "UNKNOWN",
];

export default function RunDetail({ detail, busy, onBulk, onSetPiece,
                                    onCorrectEndState }) {
  if (!detail) return <p className="muted">Select a run.</p>;
  const { run, events, pieces, badges } = detail;
  const outcome = runOutcome(run);
  const rollup = pieceRollup(pieces);

  return (
    <div className="stack">
      <Card title={run.subtask_name ?? run.sd_path ?? "Run"}>
        <dl className="kv">
          <dt>Printer</dt><dd>{run.printer_name || run.printer_serial}</dd>
          <dt>Source</dt><dd>{run.source}</dd>
          <dt>Outcome</dt>
          <dd>
            <span className={`pill pill-${outcome.tone}`}>{outcome.label}</span>
            {run.end_state && (
              <select value={run.end_state} disabled={busy}
                      onChange={(e) => onCorrectEndState(e.target.value)}>
                {END_STATES.map((s) => <option key={s} value={s}>{s}</option>)}
              </select>
            )}
          </dd>
          <dt>Layers</dt>
          <dd>{run.last_layer ?? "—"} / {run.total_layers ?? "—"}</dd>
          <dt>Elapsed</dt>
          <dd>{formatDuration(elapsedSeconds(run.started_at, run.ended_at))}</dd>
          <dt>Filament</dt>
          <dd>
            {run.actual_grams == null ? "—" : `${run.actual_grams} g`}
            {run.actual_grams_basis
              ? ` (${run.actual_grams_basis})`
              : ""}
          </dd>
        </dl>
        {badges.length > 0 && (
          <p>{badges.map((b) => (
            <span key={b.badge_id} className="pill pill-warn">{b.label}</span>
          ))}</p>
        )}
      </Card>

      <Card title={`Pieces — ${rollup.good}/${rollup.total} good`
                   + (rollup.pending ? `, ${rollup.pending} unconfirmed` : "")}>
        <PieceGrid pieces={pieces} busy={busy} onBulk={onBulk}
                   onSetPiece={onSetPiece} />
      </Card>

      <Card title="Timeline">
        <ul className="timeline">
          {events.map((e) => (
            <li key={e.id}>
              <code>{(e.ts ?? "").replace("T", " ").slice(0, 19)}</code>{" "}
              <strong>{e.kind}</strong>{" "}
              {e.payload ? <span className="muted">
                {JSON.stringify(e.payload)}</span> : null}
            </li>
          ))}
        </ul>
      </Card>
    </div>
  );
}
