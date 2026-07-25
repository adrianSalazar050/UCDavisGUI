import { useCallback, useEffect, useRef, useState } from "react";
import { fetchRobot, fetchRobotCommand, runRobotCommand } from "../api/robot.js";
import Button from "../components/ui/Button.jsx";
import Card from "../components/ui/Card.jsx";
import Field from "../components/ui/Field.jsx";
import PageFrame from "../components/ui/PageFrame.jsx";
import StatusPill from "../components/ui/StatusPill.jsx";

// Robot state isn't pushed over the printers WebSocket (different machine), so
// this page polls: faster while a command runs than when the arm is parked.
const POLL_IDLE_MS = 4000;
const POLL_BUSY_MS = 1000;

// Values are kept as strings while typing, so an empty box doesn't become 0,
// and converted on send.
function paramsToSend(spec, values) {
  const out = {};
  for (const p of spec.params) {
    const raw = values[p.name];
    if (raw === "" || raw === undefined) continue;   // let the robot default it
    out[p.name] = p.type === "number" ? Number(raw) : raw;
  }
  return out;
}

function defaultsFor(spec) {
  return Object.fromEntries(
    spec.params.map((p) => [p.name, p.default ?? ""]));
}

function CommandForm({ spec, disabled, dryRun, onRun }) {
  const [values, setValues] = useState(() => defaultsFor(spec));

  // reset the inputs when a different command is selected
  useEffect(() => { setValues(defaultsFor(spec)); }, [spec]);

  const submit = (e) => {
    e.preventDefault();
    // confirm only when the arm will really move; dry-run just prints the call
    if (spec.moves && !dryRun &&
        !window.confirm(
          `Run "${spec.name}" on the robot now?\n\n` +
          `THE ARM WILL MOVE. Make sure the workspace is clear.`)) {
      return;
    }
    onRun(spec, paramsToSend(spec, values));
  };

  return (
    <form className="robot-form" onSubmit={submit}>
      {spec.doc && <p className="robot-form__doc">{spec.doc}</p>}
      {spec.params.map((p) => (
        <Field
          key={p.name}
          label={p.name}
          type={p.type === "number" ? "number" : "text"}
          step="any"
          value={values[p.name] ?? ""}
          placeholder={p.default === null ? "" : String(p.default)}
          onChange={(e) =>
            setValues((v) => ({ ...v, [p.name]: e.target.value }))}
        />
      ))}
      <Button type="submit" variant="primary" disabled={disabled}>
        {spec.moves && !dryRun ? `Run ${spec.name} (moves arm)` : `Run ${spec.name}`}
      </Button>
    </form>
  );
}

function ResultLine({ record }) {
  if (!record) return null;
  const status = record.state === "done" ? "ok"
    : record.state === "failed" ? "error" : "warn";
  return (
    <div className="robot-result">
      <StatusPill status={status}>{record.state}</StatusPill>
      <span className="robot-result__name">{record.name}</span>
      {record.duration_s != null && (
        <span className="robot-result__time">{record.duration_s}s</span>
      )}
      <div className="robot-result__body">
        {record.state === "failed"
          ? record.error
          : typeof record.result === "object" && record.result !== null
            ? JSON.stringify(record.result)
            : String(record.result ?? "")}
      </div>
    </div>
  );
}

export default function Robot() {
  const [status, setStatus] = useState(null);
  const [selected, setSelected] = useState(null);   // command name
  const [err, setErr] = useState(null);
  const [sending, setSending] = useState(false);
  const [tracked, setTracked] = useState(null);     // the command we launched

  // only the most recent poll may write state
  const requestId = useRef(0);

  const load = useCallback(async () => {
    const id = (requestId.current += 1);
    try {
      const data = await fetchRobot();
      if (id === requestId.current) {
        setStatus(data);
        setErr(null);
      }
    } catch (e) {
      if (id === requestId.current) setErr(e.message);
    }
  }, []);

  useEffect(() => {
    load();
    const period = status?.busy ? POLL_BUSY_MS : POLL_IDLE_MS;
    const iid = setInterval(load, period);
    return () => clearInterval(iid);
  }, [load, status?.busy]);

  // Follow the command we launched until it finishes: /api/robot only reports
  // the currently running one.
  useEffect(() => {
    if (!tracked || tracked.state === "done" || tracked.state === "failed") return;
    const iid = setInterval(async () => {
      try {
        const record = await fetchRobotCommand(tracked.id);
        setTracked(record);
      } catch (e) {
        setErr(e.message);
      }
    }, POLL_BUSY_MS);
    return () => clearInterval(iid);
  }, [tracked]);

  const handleRun = async (spec, params) => {
    setSending(true);
    setErr(null);
    try {
      const receipt = await runRobotCommand(spec.name, params);
      // the robot has it; the result arrives later
      setTracked({ id: receipt.id, name: receipt.name, state: "running" });
      load();
    } catch (e) {
      setErr(e.message);
    } finally {
      setSending(false);
    }
  };

  if (!status) {
    return <PageFrame><div className="empty">Contacting robot…</div></PageFrame>;
  }

  if (!status.reachable) {
    return (
      <PageFrame>
        <Card title="Robot">
          <div className="state-error">
            <span>{status.error}</span>
            <Button size="sm" onClick={load}>Retry</Button>
          </div>
        </Card>
      </PageFrame>
    );
  }

  const commands = status.commands ?? [];
  const spec = commands.find((c) => c.name === selected) ?? commands[0] ?? null;
  const busy = status.busy || sending;

  return (
    <PageFrame>
      <Card title={`Robot — ${status.robot}`}>
        <div className="robot-status">
          <StatusPill status={status.busy ? "warn" : "ok"}>
            {status.busy ? `running ${status.current?.name}` : "idle"}
          </StatusPill>
          {status.dry_run && (
            <StatusPill status="warn">
              testing mode — commands are printed, not executed
            </StatusPill>
          )}
        </div>
        {err && (
          <div className="state-error">
            <span>{err}</span>
            <Button size="sm" onClick={load}>Retry</Button>
          </div>
        )}
      </Card>

      <Card title="Send a command">
        <div className="robot-cmdlist">
          {commands.map((c) => (
            <Button
              key={c.name}
              size="sm"
              variant={c.name === spec?.name ? "primary" : "secondary"}
              onClick={() => setSelected(c.name)}
            >
              {c.name}
            </Button>
          ))}
        </div>
        {spec
          ? <CommandForm spec={spec} disabled={busy} dryRun={status.dry_run}
                         onRun={handleRun} />
          : <div className="empty">The robot exposes no commands.</div>}
        {busy && !sending && (
          <div className="robot-busy-note">
            The arm is busy — one command runs at a time.
          </div>
        )}
      </Card>

      <Card title="Result">
        {tracked
          ? <ResultLine record={tracked} />
          : <div className="empty">No command sent yet.</div>}
      </Card>

      {status.history?.length > 0 && (
        <Card title="Recent commands">
          {status.history.map((h) => <ResultLine key={h.id} record={h} />)}
        </Card>
      )}
    </PageFrame>
  );
}
