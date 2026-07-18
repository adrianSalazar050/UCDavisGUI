import { useEffect, useRef, useState } from "react";
import { fetchFiles } from "../../api/printer.js";

// Lists the SD card root, filtered to .3mf files, and hands a click off to
// the caller's onAdd(name) (which itself calls addQueueJob + refetches the
// queue). Root-only, non-recursive -- matches the plan's scope; a subfolder
// browser is future work, not part of this picker.
export default function SdPicker({ serial, onAdd }) {
  const [entries, setEntries] = useState([]);
  const [err, setErr] = useState(null);
  const [loading, setLoading] = useState(true);
  const [addingName, setAddingName] = useState(null);

  // Guards against setting state after this panel closes (the parent
  // unmounts SdPicker on a successful add, possibly before this component's
  // own in-flight requests settle).
  const alive = useRef(true);
  useEffect(() => () => { alive.current = false; }, []);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setErr(null);
    fetchFiles(serial, "/")
      .then((data) => {
        if (cancelled) return;
        setEntries(data.entries.filter(
          (e) => !e.is_dir && e.name.toLowerCase().endsWith(".3mf")));
      })
      .catch((e) => { if (!cancelled) setErr(e.message); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [serial]);

  const pick = async (name) => {
    setAddingName(name);
    try {
      await onAdd(name);
      // On success the parent closes this panel -- no local state to reset.
    } catch (e) {
      if (alive.current) {
        setErr(e.message);
        setAddingName(null);
      }
    }
  };

  return (
    <div className="queue-picker">
      {err ? (
        <div className="state-error"><span>{err}</span></div>
      ) : loading ? (
        <div className="empty">Reading the card…</div>
      ) : entries.length === 0 ? (
        <div className="empty">No .3mf files on the SD card root.</div>
      ) : (
        <table className="file-table">
          <thead><tr><th>Name</th></tr></thead>
          <tbody>
            {entries.map((e) => (
              <tr key={e.name}>
                <td>
                  <button type="button" className="file-table__dir"
                          disabled={addingName != null}
                          onClick={() => pick(e.name)}>
                    {addingName === e.name ? `Adding ${e.name}…` : e.name}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
