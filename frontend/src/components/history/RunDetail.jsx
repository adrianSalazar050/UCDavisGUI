import Card from "../ui/Card.jsx";
import EmptyState from "../ui/EmptyState.jsx";
import PieceGrid from "./PieceGrid.jsx";
import { formatDuration, elapsedSeconds, pieceRollup, runOutcome }
  from "./runFormat.js";

const END_STATES = [
  "FINISH", "FAILED", "STOPPED_BY_MONITOR", "STOPPED_BY_OPERATOR",
  "START_UNCONFIRMED", "UNKNOWN",
];

// Three cards, ordered by what a run is opened for: what it was and how it
// ended, then the verdict on each piece, then the recorder's raw events last —
// everything above is derived from them, so they are reference, not the lead.
export default function RunDetail({ detail, busy, onBulk, onSetPiece,
                                    onCorrectEndState }) {
  if (!detail) {
    return (
      <EmptyState title="No run selected">
        Pick a row in the run log above to read its events, set a verdict on
        each piece it produced, and correct the outcome if the recorder read
        the machine wrong.
      </EmptyState>
    );
  }
  const { run, events, pieces, badges } = detail;
  const outcome = runOutcome(run);
  const rollup = pieceRollup(pieces);

  return (
    <div className="stack">
      <Card title={run.subtask_name ?? run.sd_path ?? "Run"}>
        {/* Outcome and badges lead: they are the fields an operator opens a
            run to check or to correct. Source and printer are provenance —
            true, rarely the question, so they read last. */}
        <dl className="kv">
          <dt>Outcome</dt>
          <dd>
            <div className="row">
              <span className={`pill pill-${outcome.tone}`}>
                {outcome.label}</span>
              {run.end_state && (
                <select value={run.end_state} disabled={busy}
                        aria-label="Correct the recorded outcome"
                        onChange={(e) => onCorrectEndState(e.target.value)}>
                  {END_STATES.map((s) => <option key={s} value={s}>{s}</option>)}
                </select>
              )}
            </div>
          </dd>
          {badges.length > 0 && (
            // A dt/dd pair rather than a loose <p> under the list: a lone
            // amber pill with no label does not say what it is claiming.
            <>
              <dt>Badges</dt>
              <dd>{badges.map((b) => (
                <span key={b.badge_id} className="pill pill-warn">{b.label}</span>
              ))}</dd>
            </>
          )}
          <dt>Started</dt>
          <dd>{run.started_at
            ? run.started_at.replace("T", " ").slice(0, 16)
            : "—"}</dd>
          <dt>Elapsed</dt>
          <dd>{formatDuration(elapsedSeconds(run.started_at, run.ended_at))}</dd>
          <dt>Layers</dt>
          <dd>{run.last_layer ?? "—"} / {run.total_layers ?? "—"}</dd>
          <dt>Filament</dt>
          <dd>
            {run.actual_grams == null ? "—" : `${run.actual_grams} g`}
            {run.actual_grams_basis
              ? ` (${run.actual_grams_basis})`
              : ""}
          </dd>
          <dt>Source</dt><dd>{run.source}</dd>
          <dt>Printer</dt><dd>{run.printer_name || run.printer_serial}</dd>
        </dl>
      </Card>

      <Card title={`Pieces — ${rollup.good}/${rollup.total} good`
                   + (rollup.pending ? `, ${rollup.pending} unconfirmed` : "")}>
        <PieceGrid pieces={pieces} busy={busy} onBulk={onBulk}
                   onSetPiece={onSetPiece} />
      </Card>

      <Card title="Event log">
        {events.length === 0 ? (
          <EmptyState title="No events">
            This run has no event rows in the ledger.
          </EmptyState>
        ) : (
          <ul className="timeline">
            {events.map((e) => (
              // Timestamp, kind, payload as adjacent siblings: .timeline li is
              // a flex row and its `gap` does the spacing, so a literal {" "}
              // between them would become another flex item and double it.
              <li key={e.id}>
                <code>{(e.ts ?? "").replace("T", " ").slice(0, 19)}</code>
                <strong>{e.kind}</strong>
                {e.payload ? <span className="muted">
                  {JSON.stringify(e.payload)}</span> : null}
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}
