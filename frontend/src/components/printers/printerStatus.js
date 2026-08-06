// One place that turns a printer summary into a status tone + label.
//
// Before this, three components each decided it for themselves: PrinterCard's
// local pill(), App's CONN table, and the topbar's fleet line. That was
// survivable while the only way to see a printer's state was to look at its
// card -- now the topbar switcher shows the same state next to the page you
// are on, and two components disagreeing about whether a machine is "Stale"
// or "Connected" would be visible side by side.
//
// Pure: no React, no fetch. Tested in printerStatus.test.js, the same
// extract-the-maths-for-testability rule roiGeometry.js and runFormat.js follow.

// -> { tone, label } where tone is a StatusPill status.
//
// Order matters: connection is checked before gcode_state, because a stale or
// disconnected printer's last-known gcode_state is a lie -- it may still say
// RUNNING minutes after the machine dropped off the network.
export function printerTone(printer) {
  if (!printer) return { tone: "warn", label: "No printer" };
  if (printer.connection === "disconnected") {
    return { tone: "danger", label: "Offline" };
  }
  if (printer.connection === "stale") return { tone: "warn", label: "Stale" };
  // `||` not `??`: the printer publishes "" before its first full report
  // (bambu_link's push_all round trip), and an empty pill reads as a broken
  // UI rather than as a connected machine that hasn't said what it's doing.
  return { tone: "ok", label: printer.gcode_state || "Connected" };
}

// "layer 12 / 100 · 34%" -- or null when the printer isn't reporting layers.
// Returning null (rather than an em dash) lets callers omit the line entirely.
export function printerProgress(printer) {
  if (!printer || printer.layer_num == null) return null;
  const layers = `layer ${printer.layer_num} / ${printer.total_layer_num ?? "?"}`;
  return printer.mc_percent != null
    ? `${layers} · ${printer.mc_percent}%`
    : layers;
}

// Percent complete as a number in 0..100, or null. Clamped, because mc_percent
// comes off the wire and a bar drawn at 140% would overflow its track.
//
// The null/"" guard is NOT redundant with the isFinite check below it:
// Number(null) and Number("") are both 0, not NaN. Without it, a printer that
// reports no percent at all draws a bar at 0% -- which claims the print has
// just started rather than that nothing is printing. Caught by the test.
export function printerPercent(printer) {
  const value = printer?.mc_percent;
  if (value == null || value === "") return null;
  const raw = Number(value);
  if (!Number.isFinite(raw)) return null;
  return Math.min(100, Math.max(0, raw));
}

// "3 printers · 1 online", or "No printers" -- the topbar's fleet line.
export function fleetSummary(printers) {
  const list = printers ?? [];
  if (list.length === 0) return "No printers";
  const online = list.filter((p) => p.connection === "ok").length;
  return `${list.length} printer${list.length === 1 ? "" : "s"} · ${online} online`;
}

// Roving-focus arithmetic for the switcher's menu: which index a key should
// move to, or null for a key this doesn't handle (so the caller can leave the
// event alone rather than swallowing it).
//
// Wraps deliberately -- a menu of three printers is short enough that walking
// off the end and landing back at the top is faster than reversing direction.
export function moveFocus(current, key, count) {
  if (count <= 0) return null;
  const at = current == null ? -1 : current;
  switch (key) {
    case "ArrowDown": return at < 0 ? 0 : (at + 1) % count;
    case "ArrowUp": return at < 0 ? count - 1 : (at - 1 + count) % count;
    case "Home": return 0;
    case "End": return count - 1;
    default: return null;
  }
}
