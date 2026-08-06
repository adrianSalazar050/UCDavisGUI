import { useState } from "react";
import { reconnectPrinter, removePrinter } from "../../api/printer.js";
import Button from "../ui/Button.jsx";
import StatusPill from "../ui/StatusPill.jsx";
import EditPrinterForm from "./EditPrinterForm.jsx";
import { printerProgress, printerTone } from "./printerStatus.js";

export default function PrinterCard({ printer, selected, onSelect }) {
  const [busy, setBusy] = useState(false);
  const [reconnecting, setReconnecting] = useState(false);
  const [err, setErr] = useState(null);
  const [editing, setEditing] = useState(false);
  // printerTone/printerProgress instead of the local copies this file used to
  // carry: the topbar switcher renders the same machine's state a few pixels
  // above this card, and two implementations disagreeing about "Stale" or
  // "Connected" would be visible side by side.
  const { tone, label } = printerTone(printer);

  // Forces an immediate reconnect. The service already retries on its own
  // every 10s, so this is for "I just fixed it, don't make me wait" and for
  // rebuilding a wedged client. It will NOT help if the IP changed -- that
  // needs Edit, which rebuilds on a host change.
  const reconnect = async () => {
    setReconnecting(true);
    setErr(null);
    try {
      await reconnectPrinter(printer.serial);
      // No refetch: /ws pushes the new summary within ~250 ms.
    } catch (e2) {
      setErr(e2.message);
    } finally {
      setReconnecting(false);
    }
  };

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

  const progress = printerProgress(printer);

  return (
    <div className={`printer-card${selected ? " printer-card--selected" : ""}`}>
      {/* The ::after on this button covers the card, so clicking anywhere
          selects — without nesting Remove inside another button.
          aria-current (not aria-pressed): this marks "the current item in
          a set of cards", not a two-state toggle you press on/off. It must be
          the STRING "true" or nothing at all: React serialises the boolean
          false to aria-current="false", and a grid where every card announces
          an aria-current is a grid with no current card. */}
      <button type="button"
              className="printer-card__name printer-card__select"
              aria-current={selected ? "true" : undefined}
              onClick={() => onSelect(printer.serial)}>
        {printer.name}
      </button>
      <div className="printer-card__meta">{printer.printer}</div>
      <div className="row">
        <StatusPill status={tone}>{label}</StatusPill>
        {/* The webcam points at exactly one machine, and both detection and the
            dashboard's live view only exist there -- so it is a badge next to
            the status, not the stray lowercase word in the footer that this
            replaces. No title attribute: the select overlay above swallows
            hover, so a tooltip anywhere in the card body would never open. */}
        {printer.capture && <span className="pill pill-ok">Camera printer</span>}
      </div>
      {/* Dropped, not em-dashed, when the printer reports no layers:
          printerProgress returns null precisely so the line can disappear. */}
      {progress && <div className="printer-card__meta">{progress}</div>}
      {printer.last_error && (
        <div className="printer-card__error">{printer.last_error}</div>
      )}
      {err && <div className="printer-card__error">{err}</div>}
      {/* Selecting lives in the topbar now, which makes the card's click a
          shortcut -- and an unlabelled one, since the overlay is invisible
          until you hover it. It says where it goes, and it carries the word
          "Selected" because the border ring alone was the only sign of which
          machine the app is pointed at. */}
      <div className="printer-card__meta">
        {selected ? "Selected · opens its dashboard" : "Opens its dashboard"}
      </div>
      {/* These three must stay inside .printer-card__foot: that selector is
          what lifts them above the select overlay. */}
      <div className="printer-card__foot">
        <Button variant="ghost" size="sm" busy={reconnecting} onClick={reconnect}>
          Reconnect
        </Button>
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
