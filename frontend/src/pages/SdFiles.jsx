import { Fragment, useCallback, useEffect, useRef, useState } from "react";
import { fetchFiles } from "../api/printer.js";
import FileTable from "../components/sd/FileTable.jsx";
import Button from "../components/ui/Button.jsx";
import Card from "../components/ui/Card.jsx";
import PageFrame from "../components/ui/PageFrame.jsx";

const join = (path, name) => (path === "/" ? `/${name}` : `${path}/${name}`);

function Crumbs({ path, onGo }) {
  const parts = path.split("/").filter(Boolean);
  return (
    <nav className="crumbs" aria-label="Folder path">
      <button type="button" className="crumbs__btn" onClick={() => onGo("/")}
              disabled={parts.length === 0}>
        SD card
      </button>
      {parts.map((part, i) => (
        // A Fragment (not a wrapping <span>) keeps the separator and the
        // button as direct children of the flex `.crumbs` container, so its
        // `gap` lands evenly around every piece ("SD card / a / b"). A
        // wrapping element would put the gap around each {sep, button} pair
        // instead, leaving the separator glued to the following name.
        <Fragment key={`${i}-${part}`}>
          <span className="crumbs__sep">/</span>
          <button type="button" className="crumbs__btn"
                  disabled={i === parts.length - 1}
                  onClick={() => onGo("/" + parts.slice(0, i + 1).join("/"))}>
            {part}
          </button>
        </Fragment>
      ))}
    </nav>
  );
}

// Owns the path/entries/error/loading state for exactly ONE printer.
// SdFiles below mounts this with key={printer.serial}, so switching printers
// remounts it from scratch instead of resetting via an Effect.
//
// The Effect-reset alternative (setPath("/") in an Effect keyed on the
// printer) sounds equivalent but isn't: that reset Effect and the
// fetch-on-path-change Effect both fire in the SAME commit when the printer
// changes (the fetch Effect's dependency is `load`, which is also recreated
// on printer change), and at that point `path` state still holds whatever
// the PREVIOUS printer had navigated to. So it fires one fetch for the new
// printer at the old, unrelated path, then a second fetch a render later
// once setPath("/") actually lands — two FTPS handshakes (each a real TLS
// negotiation against hardware that may be off) where one was needed, plus a
// visible flash of a wrong or erroring listing under the new printer's name.
// Remounting sidesteps the whole class of bug: fresh state, one Effect, one
// fetch, and any in-flight request from the discarded instance lands on a
// closure nothing renders from anymore.
function SdBrowser({ printer }) {
  const [path, setPath] = useState("/");
  const [entries, setEntries] = useState([]);
  const [err, setErr] = useState(null);
  const [loading, setLoading] = useState(false);

  // Same-printer race: open folder A, then quickly folder B, before A's
  // response lands. Only the response matching the most recently issued
  // request may write to state — an out-of-order arrival is silently
  // dropped rather than clobbering the newer path's listing.
  const requestId = useRef(0);

  const load = useCallback(async (target) => {
    const id = (requestId.current += 1);
    setLoading(true);
    setErr(null);
    try {
      const data = await fetchFiles(printer.serial, target);
      if (id === requestId.current) setEntries(data.entries);
    } catch (e) {
      if (id === requestId.current) {
        setErr(e.message);
        setEntries([]);
      }
    } finally {
      if (id === requestId.current) setLoading(false);
    }
  }, [printer.serial]);

  // An FTPS handshake is not instant — this is not a poller, it runs on
  // navigation and on Refresh only.
  useEffect(() => { load(path); }, [load, path]);

  return (
    <Card title={`microSD — ${printer.name}`}>
      <div className="sd-toolbar">
        <Crumbs path={path} onGo={setPath} />
        <Button size="sm" busy={loading} onClick={() => load(path)}>
          Refresh
        </Button>
      </div>
      {err ? (
        <div className="state-error">
          <span>{err}</span>
          <Button size="sm" onClick={() => load(path)}>Retry</Button>
        </div>
      ) : loading && entries.length === 0 ? (
        <div className="empty">Reading the card…</div>
      ) : (
        <FileTable entries={entries}
                   onOpen={(name) => setPath(join(path, name))} />
      )}
    </Card>
  );
}

export default function SdFiles({ printers, selected }) {
  const printer = printers.find((p) => p.serial === selected) ?? null;

  if (!printer) {
    return (
      <PageFrame>
        <div className="empty">
          No printer selected — pick one on the Overview page.
        </div>
      </PageFrame>
    );
  }

  return (
    <PageFrame>
      <SdBrowser key={printer.serial} printer={printer} />
    </PageFrame>
  );
}
