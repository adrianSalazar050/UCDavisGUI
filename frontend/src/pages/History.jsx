import { useCallback, useEffect, useRef, useState } from "react";
import { bulkPieces, fetchRun, fetchRuns, patchPiece, patchRun }
  from "../api/printer.js";
import RunDetail from "../components/history/RunDetail.jsx";
import RunTable from "../components/history/RunTable.jsx";
import Card from "../components/ui/Card.jsx";
import PageFrame from "../components/ui/PageFrame.jsx";

const POLL_MS = 5000;

// One printer's history. Mounted with key={serial} by History below, the same
// remount-instead-of-Effect-reset pattern SdFiles and Queue use.
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
      {err && <p className="error">{err}</p>}
      <Card title={`Runs — ${printer.name || printer.serial}`}>
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

export default function History({ printers, selected }) {
  const printer = printers.find((p) => p.serial === selected);
  return (
    <PageFrame>
      {printer
        ? <HistoryPanel key={printer.serial} printer={printer} />
        : <p className="muted">Select a printer.</p>}
    </PageFrame>
  );
}
