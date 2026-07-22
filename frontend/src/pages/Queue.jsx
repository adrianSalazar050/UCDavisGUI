import { useCallback, useEffect, useRef, useState } from "react";
import { addQueueJob, fetchQueue, removeQueueJob, reorderQueue, startQueueJob } from "../api/printer.js";
import QueueTable from "../components/queue/QueueTable.jsx";
import SdPicker from "../components/queue/SdPicker.jsx";
import TotalsBar from "../components/queue/TotalsBar.jsx";
import Button from "../components/ui/Button.jsx";
import Card from "../components/ui/Card.jsx";
import PageFrame from "../components/ui/PageFrame.jsx";

const POLL_MS = 4000;
const EMPTY_TOTALS = { seconds: 0, grams: 0, finish_epoch: null };

// Owns the queue state for exactly ONE printer. Queue below mounts this with
// key={printer.serial}, same reasoning as SdFiles' SdBrowser: switching
// printers remounts from scratch (fresh poll, no stale-printer flash)
// instead of resetting via an Effect.
function QueuePanel({ printer }) {
  const [jobs, setJobs] = useState([]);
  const [totals, setTotals] = useState(EMPTY_TOTALS);
  const [err, setErr] = useState(null);
  const [initialLoaded, setInitialLoaded] = useState(false);
  const [busyId, setBusyId] = useState(null); // job id mid remove/reorder
  const [pickerOpen, setPickerOpen] = useState(false);
  const [notice, setNotice] = useState(null);  // start outcome, cleared on retry

  // Whether the printer can accept a print right now. Mirrors the server's
  // guard (server/printer.py BUSY_STATES) so the button is disabled rather
  // than offering an action that would come back 409.
  const gstate = (printer.gcode_state || "").toUpperCase();
  const online = printer.connection === "ok";
  const printing = ["RUNNING", "PREPARE", "PAUSE", "PAUSED", "SLICING"].includes(gstate);
  const canStart = online && !printing;
  const startBlockedReason = !online
    ? `${printer.name} is not connected`
    : printing
      ? `${printer.name} is already printing (${gstate})`
      : "";

  // Same-printer race guard as SdFiles: only the most recently issued
  // request may write to state.
  const requestId = useRef(0);

  const load = useCallback(async () => {
    const id = (requestId.current += 1);
    try {
      const data = await fetchQueue(printer.serial);
      if (id === requestId.current) {
        setJobs(data.jobs);
        setTotals(data.totals);
        setErr(null);
      }
    } catch (e) {
      if (id === requestId.current) setErr(e.message);
    } finally {
      if (id === requestId.current) setInitialLoaded(true);
    }
  }, [printer.serial]);

  // Queue state isn't pushed over the WebSocket (it's not high-frequency
  // like detection), so poll for external changes on top of the
  // refetch-after-every-mutation below.
  useEffect(() => {
    load();
    const iid = setInterval(load, POLL_MS);
    return () => clearInterval(iid);
  }, [load]);

  const handleAdd = async (name) => {
    setErr(null);
    await addQueueJob(printer.serial, "/" + name); // let the picker see failures
    setPickerOpen(false);
    await load();
  };

  const handleRemove = async (id) => {
    setBusyId(id);
    setErr(null);
    try {
      await removeQueueJob(printer.serial, id);
      await load();
    } catch (e) {
      setErr(e.message);
    } finally {
      setBusyId(null);
    }
  };

  // Starting moves real hardware, so it is the one queue action behind a
  // confirm. `started: false` is a normal 200 (command sent, printer never
  // reported starting) -- surfaced as a warning, and the job stays queued.
  const handleStart = async (job) => {
    if (!window.confirm(
          `Start printing "${job.name}" on ${printer.name} now?\n\n` +
          `Make sure the build plate is clear and the previous print has been removed.`)) {
      return;
    }
    setBusyId(job.id);
    setErr(null);
    setNotice(null);
    try {
      const data = await startQueueJob(printer.serial, job.id);
      setJobs(data.jobs);
      setTotals(data.totals);
      setNotice(data.started
        ? `Printing "${job.name}".`
        : `Sent "${job.name}", but the printer did not report a print starting — it is still queued.`);
    } catch (e) {
      setErr(e.message);
    } finally {
      setBusyId(null);
      load();
    }
  };

  const handleMove = async (index, dir) => {
    const target = index + dir;
    if (target < 0 || target >= jobs.length) return;
    const ids = jobs.map((j) => j.id);
    [ids[index], ids[target]] = [ids[target], ids[index]];
    setBusyId(jobs[index].id);
    setErr(null);
    try {
      const data = await reorderQueue(printer.serial, ids);
      setJobs(data.jobs);
      setTotals(data.totals);
    } catch (e) {
      setErr(e.message);
    } finally {
      setBusyId(null);
    }
  };

  return (
    <Card title={`Print Queue — ${printer.name}`}>
      <div className="queue-toolbar">
        <Button size="sm" onClick={() => setPickerOpen((v) => !v)}>
          {pickerOpen ? "Close" : "Add from SD"}
        </Button>
      </div>
      {pickerOpen && <SdPicker serial={printer.serial} onAdd={handleAdd} />}
      {err && (
        <div className="state-error">
          <span>{err}</span>
          <Button size="sm" onClick={load}>Retry</Button>
        </div>
      )}
      {notice && <div className="queue-notice">{notice}</div>}
      {!initialLoaded ? (
        <div className="empty">Loading queue…</div>
      ) : jobs.length === 0 ? (
        <div className="empty">
          Queue is empty — use "Add from SD" to plan a print.
        </div>
      ) : (
        <QueueTable jobs={jobs} busyId={busyId}
                    onRemove={handleRemove} onMove={handleMove}
                    onStart={handleStart} canStart={canStart}
                    startBlockedReason={startBlockedReason}
                    printerModelId={printer.model_id} />
      )}
      <TotalsBar totals={totals} count={jobs.length} />
    </Card>
  );
}

export default function Queue({ printers, selected }) {
  const printer = printers.find((p) => p.serial === selected) ?? null;

  if (!printer) {
    return (
      <PageFrame>
        <div className="empty">
          No printer selected — pick one on the Overview page.
        </div>
      </PageFrame>
    );
  }

  return (
    <PageFrame>
      <QueuePanel key={printer.serial} printer={printer} />
    </PageFrame>
  );
}
