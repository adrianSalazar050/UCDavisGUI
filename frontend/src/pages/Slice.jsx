import { useCallback, useEffect, useRef, useState } from "react";
import { fetchSliceOptions } from "../api/printer.js";
import SliceForm from "../components/slice/SliceForm.jsx";
import SliceJobList from "../components/slice/SliceJobList.jsx";
import Button from "../components/ui/Button.jsx";
import Card from "../components/ui/Card.jsx";
import EmptyState from "../components/ui/EmptyState.jsx";
import PageFrame from "../components/ui/PageFrame.jsx";

// Owns the slice options (presets/filaments/detected filament) and the
// job-list refresh trigger for exactly ONE printer. Slice below mounts this
// with key={printer.serial}, same reasoning as SdFiles' SdBrowser and Queue's
// QueuePanel: switching printers remounts from scratch (fresh options fetch,
// no stale-printer flash) instead of resetting via an Effect -- see the long
// comment in SdFiles.jsx for why the Effect-reset alternative is wrong.
function SlicePanel({ printer, onNavigate }) {
  const [options, setOptions] = useState(null);
  const [err, setErr] = useState(null);
  const [initialLoaded, setInitialLoaded] = useState(false);
  // Bumped after every successful submit so SliceJobList reloads right away
  // instead of waiting out its own poll interval for the new job to appear.
  const [refreshSignal, setRefreshSignal] = useState(0);

  // Same-printer race guard as SdFiles/QueuePanel: only the most recently
  // issued request may write to state.
  const requestId = useRef(0);

  const load = useCallback(async () => {
    const id = (requestId.current += 1);
    try {
      const data = await fetchSliceOptions(printer.serial);
      if (id === requestId.current) {
        setOptions(data);
        setErr(null);
      }
    } catch (e) {
      if (id === requestId.current) setErr(e.message);
    } finally {
      if (id === requestId.current) setInitialLoaded(true);
    }
  }, [printer.serial]);

  // Options describe what's available right now (model/nozzle/detected
  // filament) -- fetched once on arrival, like SdFiles' directory listing,
  // not polled continuously.
  useEffect(() => { load(); }, [load]);

  return (
    <>
      {/* The topbar already says "Slice" and which printer is selected, so the
          card names what it makes rather than restating either. */}
      <Card title="New slice">
        {err ? (
          <div className="state-error">
            <span>{err}</span>
            <Button size="sm" onClick={load}>Retry</Button>
          </div>
        ) : !initialLoaded ? (
          <EmptyState title="Loading slicer options…" />
        ) : (
          <SliceForm serial={printer.serial} options={options}
                     onNavigate={onNavigate}
                     onSubmitted={() => setRefreshSignal((n) => n + 1)} />
        )}
      </Card>
      <Card title="Slice jobs">
        <SliceJobList serial={printer.serial} refreshSignal={refreshSignal} />
      </Card>
    </>
  );
}

export default function Slice({ printers, selected, onNavigate }) {
  const printer = printers.find((p) => p.serial === selected) ?? null;

  if (!printer) {
    return (
      <PageFrame>
        {/* Two different problems wearing one message before: an empty lab
            needs a printer registered, a lab with printers just needs the
            topbar switcher pointed at one. Only the first has a page to go to. */}
        <EmptyState
          title="No printer selected"
          action={printers.length === 0 ? (
            <Button variant="primary" onClick={() => onNavigate("printers")}>
              Add a printer
            </Button>
          ) : null}
        >
          {printers.length === 0
            ? "Slicing is per printer: its model and nozzle decide which "
              + "profiles resolve, so register a printer first."
            : "Pick a printer with the switcher in the top bar — its model and "
              + "nozzle decide which presets and filaments are offered."}
        </EmptyState>
      </PageFrame>
    );
  }

  return (
    <PageFrame>
      <SlicePanel key={printer.serial} printer={printer}
                  onNavigate={onNavigate} />
    </PageFrame>
  );
}
