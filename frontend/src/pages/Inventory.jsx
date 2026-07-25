import { useCallback, useEffect, useRef, useState } from "react";
import { archiveSpool, fetchLoadedSpool, fetchSpools, setLoadedSpool }
  from "../api/printer.js";
import LoadedSpool from "../components/inventory/LoadedSpool.jsx";
import SpoolForm from "../components/inventory/SpoolForm.jsx";
import SpoolList from "../components/inventory/SpoolList.jsx";
import Card from "../components/ui/Card.jsx";
import PageFrame from "../components/ui/PageFrame.jsx";

const POLL_MS = 8000;

// Filament inventory: fleet-wide spool list + the per-printer "which spool is
// loaded" control. Fleet-wide (a spool is not tied to one machine), so it does
// not key on the selected printer for the list -- but the loaded-spool control
// does use the selection.
export default function Inventory({ printers = [], selected }) {
  const printer = printers.find((p) => p.serial === selected) ?? null;
  const [spools, setSpools] = useState([]);
  const [loaded, setLoaded] = useState(null);
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(false);
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
        {err && <p className="error">{err}</p>}
        <Card title="Add a spool">
          <SpoolForm onCreated={loadSpools} />
        </Card>
        {printer && (
          <Card title={`Loaded spool — ${printer.name || printer.serial}`}>
            <LoadedSpool
              printer={printer} loaded={loaded} spools={loadable} busy={busy}
              onLoad={(id) => act(() => setLoadedSpool(printer.serial, id))}
              onUnload={() => act(() => setLoadedSpool(printer.serial, null))}
            />
          </Card>
        )}
        <Card title="Spools">
          <SpoolList spools={spools} busy={busy}
                     onArchive={(id) => act(() => archiveSpool(id))} />
        </Card>
      </div>
    </PageFrame>
  );
}
