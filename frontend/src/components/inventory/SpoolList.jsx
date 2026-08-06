import Button from "../ui/Button.jsx";
import EmptyState from "../ui/EmptyState.jsx";

const LOW_G = 100; // highlight spools below this many grams remaining

// The null case says the word rather than showing a dash: in a column of
// weights an em dash invites the reading "nothing left", which is the one
// meaning it must never carry.
function grams(v) {
  return v == null ? "unknown" : `${Math.round(v)} g`;
}

// The lifecycle words the ledger stores (server/ledger.py SPOOL_STATUSES) are
// snake_case identifiers, and `in_use` IS the loaded marker set by
// set_loaded_spool -- so it reads here as the same word the Loaded spool card
// uses. An unrecognised value falls through untouched rather than vanishing.
const STATE = {
  sealed: "Sealed",
  in_use: "Loaded",
  spent: "Spent",
  retired: "Retired",
};

// The spool table. remaining_grams comes from the server (initial minus
// consumption); it is null when the spool has no known initial weight, and
// that must read as "unknown", never "0". A spool below LOW_G is flagged.
//
// `onAdd` opens the page's add-a-spool disclosure: an empty inventory is the
// one moment when that form is the only useful thing on the page, so the
// empty state offers it instead of leaving the reader to find it below a
// table that isn't there.
export default function SpoolList({ spools, busy, onArchive, onAdd }) {
  if (!spools.length) {
    return (
      <EmptyState
        title="No spools tracked yet"
        action={onAdd && (
          <Button variant="primary" onClick={onAdd}>Add the first spool</Button>
        )}
      >
        A spool here is one physical reel, under the code printed or stuck on
        it. Mark a reel as loaded on a printer and every print that finishes
        charges its filament against that reel — which is what lets this table
        keep saying how much is left without anyone weighing anything.
      </EmptyState>
    );
  }
  return (
    <div className="stack">
      <table className="table">
        <thead>
          <tr>
            <th>Spool code</th><th>Material</th><th>Colour</th><th>Brand</th>
            <th className="table__num">Remaining</th><th>State</th><th></th>
          </tr>
        </thead>
        <tbody>
          {spools.map((s) => {
            const low = s.remaining_grams != null && s.remaining_grams < LOW_G;
            return (
              <tr key={s.id} className={low ? "row-warn" : ""}>
                <td>{s.spool_code}</td>
                <td>{s.material || "—"}</td>
                <td>{s.colour || "—"}</td>
                <td>{s.brand || "—"}</td>
                {/* The one number this table exists for, so it is typeset as a
                    quantity: the weight carries the emphasis and the reel's
                    full capacity trails it as context. */}
                <td className="table__num">
                  {s.remaining_grams == null ? (
                    // Nothing was recorded to subtract from, so there is no
                    // quantity here to read -- muted and unemphasised, never
                    // dressed up as a weight of zero.
                    <span className="muted">{grams(s.remaining_grams)}</span>
                  ) : (
                    <>
                      <strong>{grams(s.remaining_grams)}</strong>
                      {s.initial_grams
                        ? <span className="muted">
                            {` / ${Math.round(s.initial_grams)}`}
                          </span>
                        : null}
                    </>
                  )}
                  {low && <>{" "}<span className="pill pill-warn">low</span></>}
                </td>
                <td>{STATE[s.status] ?? s.status}</td>
                <td className="table__num">
                  {/* archiveSpool sets the archived flag; the row and its
                      consumption history stay in the ledger, they just leave
                      this list. "Remove" read like scrapping the reel. */}
                  <Button size="sm" disabled={busy}
                          onClick={() => onArchive(s.id)}>Archive</Button>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <p className="muted">
        A row is tinted and badged once it drops below {LOW_G} g. Archiving a
        spool takes it off this list and keeps every gram already charged
        against it.
      </p>
    </div>
  );
}
