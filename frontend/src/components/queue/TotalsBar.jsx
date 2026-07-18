import { formatClock, formatDuration } from "./format.js";

// "N job(s) · total Xh Ym · Zg · finish ≈ H:MM" -- the finish clause is
// omitted whenever totals.finish_epoch is null (nothing timed yet, e.g. an
// empty or all-manual queue).
export default function TotalsBar({ totals, count }) {
  const parts = [
    `${count} job${count === 1 ? "" : "s"}`,
    `total ${formatDuration(totals.seconds)}`,
    `${totals.grams ?? 0}g`,
  ];
  let text = parts.join(" · ");
  if (totals.finish_epoch != null) {
    text += ` · finish ≈ ${formatClock(totals.finish_epoch)}`;
  }
  return <div className="queue-totals">{text}</div>;
}
