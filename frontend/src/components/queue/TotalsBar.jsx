import Columns from "../ui/Columns.jsx";
import StatTile from "../ui/StatTile.jsx";
import { formatClock, formatDuration } from "./format.js";

// The four numbers that answer "is there room for another job, and when is
// this printer free again".
//
// They used to be one run-on line in the card's footer ("3 jobs · total 2h 10m
// · 45g · finish ≈ 14:30"), which put the value most often wanted -- the
// projected finish -- last, in the middle of a sentence, below the table. Same
// values and the same formatters, one tile each, above the queue.
//
// The finish tile shows "—" whenever totals.finish_epoch is null: the server
// sends null when there is nothing to time (an empty queue, or one where every
// job is manual and carries no estimate), so there is no clock to project.
export default function TotalsBar({ totals, count }) {
  const timed = totals.finish_epoch != null;
  return (
    <Columns template="repeat(4, minmax(0, 1fr))" gap={4}>
      <StatTile label="Jobs queued" value={count} sub="on this printer" />
      <StatTile label="Total print time" value={formatDuration(totals.seconds)}
                sub="slicer estimate" />
      <StatTile label="Total filament" value={`${totals.grams ?? 0} g`}
                sub="slicer estimate" />
      <StatTile label="Est. finish"
                value={timed ? formatClock(totals.finish_epoch) : null}
                sub={timed ? "if the queue starts now" : "nothing timed yet"} />
    </Columns>
  );
}
