import { useState } from "react";
import { armDetection } from "../../api/printer.js";
import Button from "../ui/Button.jsx";
import Card from "../ui/Card.jsx";
import StatusPill from "../ui/StatusPill.jsx";

// The compact Dashboard card: armed state, the Arm toggle, the current top
// detection, a countdown while a fault is building, and the stopped-by-monitor
// latch. `d` is the printer's live `detection` object (never null here).
export default function AutoStopCard({ serial, d }) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);

  const toggle = async () => {
    setBusy(true);
    setErr(null);
    try {
      await armDetection(serial, !d.armed);   // WS pushes the new armed state
    } catch (e) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  };

  const top = (d.detections ?? [])[0];
  const counting = d.seconds_to_stop != null;

  return (
    <Card title="Auto-stop">
      <div className="autostop">
        <div className="autostop__row">
          <StatusPill status={d.armed ? "ok" : "warn"}>
            {d.armed ? "Armed" : "Disarmed"}
          </StatusPill>
          <Button size="sm" variant={d.armed ? "secondary" : "primary"}
                  busy={busy} onClick={toggle}>
            {d.armed ? "Disarm" : "Arm"}
          </Button>
        </div>

        {d.stopped_by_monitor && (
          <div className="autostop__stopped">■ Stopped by monitor</div>
        )}

        {counting && (
          <div className="autostop__count">
            ⚠ {top?.cls ?? "fault"} — stopping in {Math.ceil(d.seconds_to_stop)}s
          </div>
        )}

        <div className="autostop__now">
          {top
            ? `Detecting: ${top.cls} ${Number(top.conf).toFixed(2)}`
            : d.running ? "No failures detected" : "Detector not running"}
        </div>

        {err && <div className="add-form__error">{err}</div>}
      </div>
    </Card>
  );
}
