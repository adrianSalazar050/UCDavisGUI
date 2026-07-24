// Pure helpers for the History page. Extracted so they can be tested —
// the same reasoning as detection/roiGeometry.js, which is the only other
// frontend module with unit tests. Keep anything with a fetch or a hook OUT
// of this file.

const OUTCOMES = {
  FINISH: { label: "Finished", tone: "ok" },
  FAILED: { label: "Failed", tone: "bad" },
  STOPPED_BY_MONITOR: { label: "Stopped by monitor", tone: "bad" },
  STOPPED_BY_OPERATOR: { label: "Stopped by operator", tone: "warn" },
  START_UNCONFIRMED: { label: "Never started", tone: "warn" },
  UNKNOWN: { label: "Unknown", tone: "warn" },
};

export function runOutcome(run) {
  if (!run?.end_state) return { label: "Running", tone: "ok" };
  return OUTCOMES[run.end_state] ?? { label: run.end_state, tone: "warn" };
}

export function pieceRollup(pieces) {
  const out = { total: 0, good: 0, scrap: 0, rework: 0, pending: 0 };
  for (const piece of pieces ?? []) {
    out.total += 1;
    if (piece.status === "good") out.good += 1;
    else if (piece.status === "scrap") out.scrap += 1;
    else if (piece.status === "rework") out.rework += 1;
    else out.pending += 1;
  }
  return out;
}

export function formatDuration(seconds) {
  if (!seconds) return "—";
  const total = Math.round(seconds);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  return h ? `${h}h ${m}m` : `${m}m`;
}

// Seconds between two ISO-8601 stamps, or null if either is missing.
export function elapsedSeconds(startedAt, endedAt) {
  if (!startedAt || !endedAt) return null;
  const a = Date.parse(startedAt);
  const b = Date.parse(endedAt);
  if (Number.isNaN(a) || Number.isNaN(b)) return null;
  return Math.max(0, (b - a) / 1000);
}
