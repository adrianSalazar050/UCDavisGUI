// Shared formatting for the Queue page: job/totals time+grams, and the
// projected-finish clock. Kept as plain functions (no component state) so
// both QueueTable and TotalsBar render identical text for the same values.

// seconds -> "Xh Ym" (or "Ym" when under an hour); null -> "—". Used for both
// a single job's estimated time and the queue's running total (the backend
// never sends totals.seconds as null -- it's 0 for an empty/all-manual
// queue -- so this also renders that case as "0m").
export function formatDuration(seconds) {
  if (seconds == null) return "—";
  const h = Math.floor(seconds / 3600);
  const m = Math.round((seconds % 3600) / 60);
  return h > 0 ? `${h}h ${m}m` : `${m}m`;
}

// grams -> "N g"; null -> "—" (a manual/unparsed job has no filament estimate).
export function formatGrams(grams) {
  return grams == null ? "—" : `${grams} g`;
}

// A unix epoch (totals.finish_epoch) -> a local clock time, e.g. "3:45 PM".
// It's a planner hint (labeled "~" by the caller), not a guarantee.
export function formatClock(epochSeconds) {
  return new Date(epochSeconds * 1000)
    .toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}
