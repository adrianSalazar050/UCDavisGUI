import { useCallback, useEffect, useRef, useState } from "react";
import { bulkPieces, fetchRun, fetchRuns, patchPiece, patchRun }
  from "../api/printer.js";
import RunDetail from "../components/history/RunDetail.jsx";
import RunTable from "../components/history/RunTable.jsx";
import Button from "../components/ui/Button.jsx";
import Card from "../components/ui/Card.jsx";
import EmptyState from "../components/ui/EmptyState.jsx";
import PageFrame from "../components/ui/PageFrame.jsx";

const POLL_MS = 5000;

// One printer's history. Mounted with key={serial} by History below, the same
// remount-instead-of-Effect-reset pattern SdFiles and Queue use. Now that the
// topbar switcher changes printer without leaving the page, that remount is
// also what drops the previous printer's selected run and its loaded detail —
// state that would otherwise still be on screen under the new printer's name.
function HistoryPanel({ printer }) {
  const [runs, setRuns] = useState([]);
  const [selectedId, setSelectedId] = useState(null);
  const [detail, setDetail] = useState(null);
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(false);

  // Only the most recently issued request may write state — an out-of-order
  // response must never clobber a newer one (same guard as SdBrowser).
  const requestId = useRef(0);

  const loadRuns = useCallback(async () => {
    const id = (requestId.current += 1);
    try {
      const data = await fetchRuns(printer.serial);
      if (id === requestId.current) {
        setRuns(data.runs);
        setErr(null);
      }
    } catch (e) {
      if (id === requestId.current) setErr(e.message);
    }
  }, [printer.serial]);

  const loadDetail = useCallback(async (runId) => {
    if (!runId) return setDetail(null);
    try {
      setDetail(await fetchRun(runId));
    } catch (e) {
      setErr(e.message);
    }
  }, []);

  useEffect(() => {
    loadRuns();
    const t = setInterval(loadRuns, POLL_MS);
    return () => clearInterval(t);
  }, [loadRuns]);

  useEffect(() => { loadDetail(selectedId); }, [selectedId, loadDetail]);

  const act = useCallback(async (fn) => {
    setBusy(true);
    try {
      const next = await fn();
      if (next?.run) setDetail(next);
      else await loadDetail(selectedId);
      await loadRuns();
      setErr(null);
    } catch (e) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  }, [loadDetail, loadRuns, selectedId]);

  return (
    <div className="stack">
      {/* .error, the lighter line, not .state-error: the table underneath is
          still the last good listing and the poll retries by itself every few
          seconds, so a failed request must not read as a dead page — and must
          not offer a Retry button for work already in hand. */}
      {err && <p className="error" role="alert">{err}</p>}
      {/* Not "Runs — <printer>": the topbar already names the page and the
          printer. What it cannot say is the order, which is what tells you the
          top row is the most recent print. */}
      <Card title="Recorded runs — newest first">
        <RunTable runs={runs} selectedId={selectedId}
                  onSelect={setSelectedId} />
      </Card>
      <RunDetail
        detail={detail}
        busy={busy}
        onBulk={(status, inspector) => act(() => bulkPieces(
          selectedId, { status, inspected_by: inspector || null }))}
        onSetPiece={(pieceId, status, inspector) => act(() => patchPiece(
          pieceId, { status, inspected_by: inspector || null }))}
        onCorrectEndState={(endState) => act(() => patchRun(
          selectedId, { end_state: endState }))}
      />
    </div>
  );
}

export default function History({ printers, selected, onNavigate }) {
  const printer = printers.find((p) => p.serial === selected);

  // Two different nothings: an empty lab, which the user can fix from here,
  // and a lab with nothing pointed at — fixed with the topbar switcher, so
  // this says where the control is instead of sending anyone to another page.
  if (!printer) {
    return (
      <PageFrame>
        {printers.length === 0 ? (
          <EmptyState
            title="No printers registered"
            action={<Button variant="primary"
                            onClick={() => onNavigate("printers")}>
                      Add a printer
                    </Button>}>
            History is kept per machine, so there is nothing to show yet.
            Register a printer and every print it runs from then on is recorded
            for you.
          </EmptyState>
        ) : (
          <EmptyState title="No printer selected">
            Choose a printer with the switcher at the top of the window to read
            its runs, its per-piece verdicts and the filament each print used.
          </EmptyState>
        )}
      </PageFrame>
    );
  }

  return (
    <PageFrame>
      <HistoryPanel key={printer.serial} printer={printer} />
    </PageFrame>
  );
}
