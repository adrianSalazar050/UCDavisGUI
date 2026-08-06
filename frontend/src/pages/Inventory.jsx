import { useCallback, useEffect, useRef, useState } from "react";
import { archiveSpool, fetchLoadedSpool, fetchSpools, setLoadedSpool }
  from "../api/printer.js";
import LoadedSpool from "../components/inventory/LoadedSpool.jsx";
import SpoolForm from "../components/inventory/SpoolForm.jsx";
import SpoolList from "../components/inventory/SpoolList.jsx";
import Button from "../components/ui/Button.jsx";
import Card from "../components/ui/Card.jsx";
import EmptyState from "../components/ui/EmptyState.jsx";
import PageFrame from "../components/ui/PageFrame.jsx";

const POLL_MS = 8000;

// Filament inventory: fleet-wide spool list + the per-printer "which spool is
// loaded" control. Fleet-wide (a spool is not tied to one machine), so it does
// not key on the selected printer for the list -- but the loaded-spool control
// does use the selection.
export default function Inventory({ printers = [], selected, onNavigate }) {
  const printer = printers.find((p) => p.serial === selected) ?? null;
  const [spools, setSpools] = useState([]);
  const [loaded, setLoaded] = useState(null);
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(false);
  // Registering a reel is the rarest thing done on this page -- once, when a
  // box is opened -- so the form is a disclosure, closed by default. It used
  // to be the first card, above the inventory it adds to.
  const [adding, setAdding] = useState(false);
  const requestId = useRef(0);

  const loadSpools = useCallback(async () => {
    const id = (requestId.current += 1);
    try {
      const data = await fetchSpools();
      if (id === requestId.current) { setSpools(data.spools); setErr(null); }
    } catch (e) { if (id === requestId.current) setErr(e.message); }
  }, []);

  const loadLoaded = useCallback(async (serial) => {
    if (!serial) return setLoaded(null);
    try { setLoaded((await fetchLoadedSpool(serial)).spool); }
    catch (e) { setErr(e.message); }
  }, []);

  useEffect(() => {
    loadSpools();
    const t = setInterval(loadSpools, POLL_MS);
    return () => clearInterval(t);
  }, [loadSpools]);

  useEffect(() => { loadLoaded(printer?.serial); }, [printer?.serial, loadLoaded]);

  const act = useCallback(async (fn) => {
    setBusy(true);
    try {
      await fn();
      await loadSpools();
      await loadLoaded(printer?.serial);
      setErr(null);
    } catch (e) { setErr(e.message); } finally { setBusy(false); }
  }, [loadSpools, loadLoaded, printer]);

  // Only non-archived spools that aren't already loaded elsewhere are worth
  // offering; the server allows loading any, but keep the picker focused.
  const loadable = spools.filter((s) => s.status !== "retired");

  return (
    <PageFrame>
      <div className="stack">
        {/* A failed poll over a table that is still valid, so the light error
            line rather than the .state-error panel. role=alert because it
            appears on its own, with nothing else on screen moving. */}
        {err && <p className="error" role="alert">{err}</p>}
        <Card>
          {/* onAdd is withheld while the disclosure below is already open, the
              same guard Queue.jsx and Printers.jsx use. An unconditional
              () => setAdding(true) looks harmless, but a second press of the
              empty state's primary button would then do nothing at all --
              setAdding(true) on an open form is a no-op, and an inert primary
              button reads as a broken page. SpoolList renders that button only
              when onAdd is truthy, so passing null removes it instead. */}
          <SpoolList spools={spools} busy={busy}
                     onAdd={adding ? null : () => setAdding(true)}
                     onArchive={(id) => act(() => archiveSpool(id))} />
        </Card>
        <Card title="Loaded spool">
          {printer ? (
            <LoadedSpool
              printer={printer} loaded={loaded} spools={loadable} busy={busy}
              onLoad={(id) => act(() => setLoadedSpool(printer.serial, id))}
              onUnload={() => act(() => setLoadedSpool(printer.serial, null))}
            />
          ) : (
            // This whole card used to disappear when nothing was selected, so
            // the page looked as though it had no such feature at all. The
            // control that fixes it is the topbar switcher, two lines above --
            // there is nowhere to send anyone, unless there is no printer to
            // switch to in the first place.
            <EmptyState
              title={printers.length === 0
                ? "No printers registered"
                : "No printer chosen"}
              action={printers.length === 0
                ? (
                  <Button variant="primary"
                          onClick={() => onNavigate("printers")}>
                    Add a printer
                  </Button>
                )
                : null}
            >
              {printers.length === 0
                ? "A loaded spool belongs to a machine. Register a printer "
                  + "under Setup and you can mark which reel is on it."
                : "Which spool is loaded is set per printer. Choose one with "
                  + "the switcher in the header and its spool appears here; "
                  + "the table above stays the same, because a reel belongs to "
                  + "the lab rather than to one machine."}
            </EmptyState>
          )}
        </Card>
        <Card>
          <div className="stack">
            <div className="row">
              <Button variant={adding ? "secondary" : "primary"}
                      aria-expanded={adding}
                      onClick={() => setAdding((open) => !open)}>
                {adding ? "Done" : "Add a spool"}
              </Button>
              <span className="muted">
                One row per physical reel, under the code you scan or read off
                its label.
              </span>
            </div>
            {/* onCreated stays plain loadSpools -- the form clears itself, so
                leaving it open is what lets a whole delivery be entered in one
                pass, each reel appearing in the table above as it lands. */}
            {adding && <SpoolForm onCreated={loadSpools} />}
          </div>
        </Card>
      </div>
    </PageFrame>
  );
}
