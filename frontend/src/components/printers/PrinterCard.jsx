import { useState } from "react";
import { removePrinter } from "../../api/printer.js";
import Button from "../ui/Button.jsx";
import StatusPill from "../ui/StatusPill.jsx";
import EditPrinterForm from "./EditPrinterForm.jsx";

// connection -> pill. When connected, show what the printer is actually doing.
function pill(p) {
  if (p.connection === "disconnected") return { status: "danger", label: "Offline" };
  if (p.connection === "stale") return { status: "warn", label: "Stale" };
  return { status: "ok", label: p.gcode_state ?? "Connected" };
}

export default function PrinterCard({ printer, selected, onSelect }) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const [editing, setEditing] = useState(false);
  const { status, label } = pill(printer);

  const remove = async () => {
    if (!window.confirm(`Remove ${printer.name}? It will stop being monitored.`))
      return;
    setBusy(true);
    setErr(null);
    try {
      await removePrinter(printer.serial);
      // No refetch: /ws pushes the new list within ~250 ms.
    } catch (e2) {
      setErr(e2.message);
      setBusy(false);
    }
  };

  // Replaces the whole card body -- the form has its own Save/Cancel, and
  // the same /ws push that refreshes add/remove refreshes this on save.
  if (editing) {
    return (
      <div className="printer-card">
        <EditPrinterForm printer={printer} onDone={() => setEditing(false)} />
      </div>
    );
  }

  const progress = printer.layer_num != null
    ? `layer ${printer.layer_num} / ${printer.total_layer_num ?? "?"}` +
      (printer.mc_percent != null ? ` · ${printer.mc_percent}%` : "")
    : "—";

  return (
    <div className={`printer-card${selected ? " printer-card--selected" : ""}`}>
      {/* The ::after on this button covers the card, so clicking anywhere
          selects — without nesting Remove inside another button.
          aria-current (not aria-pressed): this marks "the current item in
          a set of cards", not a two-state toggle you press on/off. */}
      <button type="button"
              className="printer-card__name printer-card__select"
              aria-current={selected}
              onClick={() => onSelect(printer.serial)}>
        {printer.name}
      </button>
      <div className="printer-card__meta">{printer.printer}</div>
      <StatusPill status={status}>{label}</StatusPill>
      <div className="printer-card__meta">{progress}</div>
      {printer.last_error && (
        <div className="printer-card__error">{printer.last_error}</div>
      )}
      {err && <div className="printer-card__error">{err}</div>}
      <div className="printer-card__foot">
        {printer.capture && <span className="ui-stattile__sub">camera</span>}
        <Button variant="ghost" size="sm" onClick={() => setEditing(true)}>
          Edit
        </Button>
        <Button variant="ghost" size="sm" busy={busy} onClick={remove}>
          Remove
        </Button>
      </div>
    </div>
  );
}
