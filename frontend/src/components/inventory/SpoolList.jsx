import Button from "../ui/Button.jsx";

const LOW_G = 100; // highlight spools below this many grams remaining

function grams(v) {
  return v == null ? "—" : `${Math.round(v)} g`;
}

// The spool table. remaining_grams comes from the server (initial minus
// consumption); it is null when the spool has no known initial weight, and
// that must read as "unknown", never "0". A spool below LOW_G is flagged.
export default function SpoolList({ spools, busy, onArchive }) {
  if (!spools.length) return <p className="muted">No spools yet.</p>;
  return (
    <table className="table">
      <thead>
        <tr>
          <th>Code</th><th>Material</th><th>Colour</th><th>Brand</th>
          <th>Remaining</th><th>Status</th><th></th>
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
              <td>
                {grams(s.remaining_grams)}
                {s.initial_grams ? ` / ${Math.round(s.initial_grams)}` : ""}
                {low && <span className="pill pill-warn">low</span>}
              </td>
              <td>{s.status}</td>
              <td>
                <Button size="sm" disabled={busy}
                        onClick={() => onArchive(s.id)}>Remove</Button>
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}
