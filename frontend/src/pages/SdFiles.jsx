import { Fragment, useCallback, useEffect, useRef, useState } from "react";
import { fetchFiles, uploadFile } from "../api/printer.js";
import FileTable from "../components/sd/FileTable.jsx";
import Button from "../components/ui/Button.jsx";
import Card from "../components/ui/Card.jsx";
import EmptyState from "../components/ui/EmptyState.jsx";
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
  const [uploading, setUploading] = useState(false);
  const [notice, setNotice] = useState(null);
  const [noticeKind, setNoticeKind] = useState("ok");
  const fileInput = useRef(null);

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

  // Uploads always land in the card ROOT, whatever folder is being browsed:
  // the printer's start command is file:///sdcard/<name> with no path
  // component, so a file in a subfolder could be listed but never started.
  // After a successful STOR we reload the root so the new file is visible
  // where it actually went, not where the user happened to be standing.
  const onPick = async (e) => {
    const file = e.target.files?.[0];
    e.target.value = "";              // so picking the same file twice refires
    if (!file) return;
    setUploading(true);
    setErr(null);
    setNotice(null);
    try {
      const res = await uploadFile(printer.serial, file);
      // A raw .gcode uploads fine but the queue can't start it. That's a
      // warning, not an error -- the file IS on the card -- so it gets the
      // notice slot with its own styling rather than the error slot.
      setNotice(res.warning
        ?? `Uploaded ${res.path} (${Math.round(res.bytes / 1024)} kB)`);
      setNoticeKind(res.warning ? "warn" : "ok");
      if (path === "/") await load("/"); else setPath("/");
    } catch (ex) {
      setErr(ex.message);
    } finally {
      setUploading(false);
    }
  };

  return (
    // The topbar already names the printer, so the title says what this card
    // holds instead of repeating it.
    <Card title="Files on the card">
      <div className="sd-toolbar">
        <Crumbs path={path} onGo={setPath} />
        {/* Outside the `.row` below on purpose: `.row input` sizes bare
            controls, and this one is never seen. */}
        <input ref={fileInput} type="file" accept=".3mf,.gcode"
               style={{ display: "none" }} onChange={onPick} />
        {/* `.row` and not three more children of `.sd-toolbar`, which does not
            wrap: on a phone the hint would be squeezed into a column of
            two-word lines between the crumbs and the buttons. In a wrapping
            row it takes a line of its own and the buttons drop below it. */}
        <div className="row">
          {/* The upload rule was documented only in the comment on onPick
              above, so the first time anyone met it was when a file they sent
              from a subfolder was not in that subfolder. It belongs beside the
              control that causes it. `.ui-field__help` is the kit's help-text
              look, and this IS help for a control — just not one in a Field. */}
          <span className="ui-field__help">
            Uploads land in the card root, not in the folder shown — the printer
            starts a file by name, not by path.
          </span>
          {/* Upload writes to the card and Refresh only re-reads it. Same size,
              but only the one with a consequence reads as the primary action. */}
          <Button size="sm" variant="primary" busy={uploading}
                  onClick={() => fileInput.current?.click()}>
            Upload…
          </Button>
          <Button size="sm" busy={loading} onClick={() => load(path)}>
            Refresh
          </Button>
        </div>
      </div>
      {notice ? (
        <div className={noticeKind === "warn" ? "state-warn" : "state-ok"}>
          {notice}
        </div>
      ) : null}
      {err ? (
        <div className="state-error">
          <span>{err}</span>
          <Button size="sm" onClick={() => load(path)}>Retry</Button>
        </div>
      ) : loading && entries.length === 0 ? (
        <EmptyState title="Reading the card…">
          Every folder opened here is a fresh FTPS handshake with the printer,
          so a listing takes a moment — and needs the machine powered on.
        </EmptyState>
      ) : (
        <FileTable entries={entries}
                   onOpen={(name) => setPath(join(path, name))} />
      )}
    </Card>
  );
}

export default function SdFiles({ printers, selected, onNavigate }) {
  const printer = printers.find((p) => p.serial === selected) ?? null;

  if (!printer) {
    // With no printers at all the fix is registering one, so offer it. With
    // printers registered the fix is one click away in the topbar switcher and
    // does not leave this page, so naming the switcher is the whole answer.
    return (
      <PageFrame>
        <EmptyState
          title="No printer selected"
          action={printers.length === 0 ? (
            <Button variant="primary" onClick={() => onNavigate("printers")}>
              Register a printer
            </Button>
          ) : null}
        >
          {printers.length === 0
            ? "A card listing is read off one machine over FTPS, so there is "
              + "nothing to browse until a printer is registered."
            : "Choose one with the printer switcher in the topbar — the card "
              + "listed here is whichever printer is selected there."}
        </EmptyState>
      </PageFrame>
    );
  }

  return (
    <PageFrame>
      <SdBrowser key={printer.serial} printer={printer} />
    </PageFrame>
  );
}
