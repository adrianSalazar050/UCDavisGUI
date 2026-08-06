import { useEffect, useRef, useState } from "react";
import { fetchFiles } from "../../api/printer.js";
import Button from "../ui/Button.jsx";
import EmptyState from "../ui/EmptyState.jsx";

// Lists the SD card root, filtered to .3mf files, and hands a click off to
// the caller's onAdd(name) (which itself calls addQueueJob + refetches the
// queue). Root-only, non-recursive -- matches the plan's scope; a subfolder
// browser is future work, not part of this picker.
export default function SdPicker({ serial, onAdd, onNavigate }) {
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
      <h4 className="ui-card__title">Add a job from the microSD card</h4>
      {/* Both filters this list applies are invisible in a bare file list, and
          both are real: a raw .gcode uploads fine but the queue cannot start
          it, and the printer's start command names a file in the card root
          with no folder part (see SdFiles.jsx's onPick). Saying so here is
          what stops "my file is on the card but it isn't in this list".

          The copy says ".3mf files", NOT "sliced .3mf files": the filter above
          is a plain .3mf name test, so plain project .3mf files land in this
          list too (the SD Files page's Type column is what tells them apart).
          Tightening the filter to match the tidier claim would change which
          files can be queued at all -- behaviour, not wording -- and real
          cards are full of project .3mf files, so the wording is what gives. */}
      <p className="detect-help">
        Every .3mf file in the card root is listed: the printer starts a job by
        file name from the root, and a raw .gcode cannot be queued. A project
        .3mf that was never sliced shows up here as well, and the printer will
        refuse to start it — the SD Files page names each file's type.
      </p>
      {err ? (
        <div className="state-error"><span>{err}</span></div>
      ) : loading ? (
        <EmptyState>Reading the card…</EmptyState>
      ) : entries.length === 0 ? (
        <EmptyState
          title="No .3mf files in the card root"
          action={
            <>
              <Button variant="primary" onClick={() => onNavigate("slice")}>
                Slice a model
              </Button>
              <Button onClick={() => onNavigate("sdfiles")}>
                Upload to the card
              </Button>
            </>
          }
        >
          Slicing a model queues its .3mf for you. You can also upload one that
          was sliced elsewhere.
        </EmptyState>
      ) : (
        <table className="table">
          <thead><tr><th>File</th><th></th></tr></thead>
          <tbody>
            {entries.map((e) => (
              <tr key={e.name}>
                <td>{e.name}</td>
                <td>
                  {/* The file name used to be the button, which read like the
                      SD Files page's name links -- there, clicking a name
                      opens a folder; here it put a job in the queue. An
                      explicit Add says which. `addingName` still disables the
                      whole list and marks the row in flight. */}
                  <Button size="sm" busy={addingName === e.name}
                          disabled={addingName != null}
                          aria-label={`Add ${e.name} to the queue`}
                          onClick={() => pick(e.name)}>
                    Add
                  </Button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
