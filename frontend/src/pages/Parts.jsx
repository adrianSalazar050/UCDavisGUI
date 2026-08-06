import { useCallback, useEffect, useRef, useState } from "react";
import { addRecipe, archiveRecipe, fetchPart, fetchParts, sliceFromPart,
         updateRecipe, uploadPartModel } from "../api/printer.js";
import PartForm from "../components/parts/PartForm.jsx";
import PartList from "../components/parts/PartList.jsx";
import RecipeEditor from "../components/parts/RecipeEditor.jsx";
import Button from "../components/ui/Button.jsx";
import Card from "../components/ui/Card.jsx";
import EmptyState from "../components/ui/EmptyState.jsx";
import PageFrame from "../components/ui/PageFrame.jsx";

const POLL_MS = 8000;

export default function Parts({ printers = [], selected, onNavigate }) {
  const [parts, setParts] = useState([]);
  const [selectedId, setSelectedId] = useState(null);
  const [detail, setDetail] = useState(null);
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState(null);   // slice-queued confirmation
  // The add-a-part form is a disclosure, closed by default. It used to be the
  // first card on the page, above the catalogue -- three fields you fill in
  // once a week standing in front of the list you came here to read.
  const [adding, setAdding] = useState(false);
  const requestId = useRef(0);

  const loadParts = useCallback(async () => {
    const id = (requestId.current += 1);
    try {
      const data = await fetchParts();
      if (id === requestId.current) { setParts(data.parts); setErr(null); }
    } catch (e) { if (id === requestId.current) setErr(e.message); }
  }, []);

  const loadDetail = useCallback(async (partId) => {
    if (!partId) return setDetail(null);
    try { setDetail(await fetchPart(partId)); }
    catch (e) { setErr(e.message); }
  }, []);

  useEffect(() => {
    loadParts();
    const t = setInterval(loadParts, POLL_MS);
    return () => clearInterval(t);
  }, [loadParts]);

  useEffect(() => { loadDetail(selectedId); }, [selectedId, loadDetail]);

  // Run a mutation, then refresh both the detail payload and the list. Any
  // mutation that returns a {part,...} payload updates detail directly.
  const act = useCallback(async (fn) => {
    setBusy(true);
    try {
      const next = await fn();
      if (next?.part) setDetail(next); else await loadDetail(selectedId);
      await loadParts();
      setErr(null);
    } catch (e) { setErr(e.message); } finally { setBusy(false); }
  }, [loadDetail, loadParts, selectedId]);

  // Slicing is not a part mutation -- it returns a job id, not a part payload,
  // so it gets its own notice rather than going through act()'s refresh.
  //
  // The notice names the target printer in full: a slice from here can be sent
  // to a printer other than the one the topbar is on, and Slice/Queue both
  // show the topbar's printer -- so "watch it on Slice" is only true once you
  // are looking at the right machine. (Deliberately does NOT read `selected`:
  // that would mean touching this callback's dependency list.)
  const slice = useCallback(async (recipeId, serial) => {
    setBusy(true); setNotice(null);
    try {
      await sliceFromPart(serial, detail.part.id, recipeId);
      const name = printers.find((p) => p.serial === serial)?.name || serial;
      setNotice(`Slicing started for ${name}. It appears under Print → Slice `
                + `and queues itself for ${name} when it finishes; both of `
                + `those pages follow the printer in the topbar, so switch `
                + `that to ${name} to watch it.`);
      setErr(null);
    } catch (e) { setErr(e.message); } finally { setBusy(false); }
  }, [detail, printers]);

  return (
    <PageFrame>
      <div className="stack">
        {err && <p className="error">{err}</p>}
        {notice && (
          <div className="queue-notice">
            <div className="row">
              <span>{notice}</span>
              {onNavigate && (
                <Button size="sm" onClick={() => onNavigate("slice")}>
                  Open Slice
                </Button>
              )}
            </div>
          </div>
        )}
        <Card>
          <PartList parts={parts} selectedId={selectedId}
                    onSelect={setSelectedId}
                    onAdd={() => setAdding(true)} />
        </Card>
        <Card>
          <div className="stack">
            <div className="row">
              <Button variant={adding ? "secondary" : "primary"}
                      aria-expanded={adding}
                      onClick={() => setAdding((open) => !open)}>
                {adding ? "Cancel" : "Add a part"}
              </Button>
              <span className="muted">
                A new part number, or a new revision of one you already have.
              </span>
            </div>
            {adding && <PartForm onCreated={(id) => {
              loadParts(); setSelectedId(id); setAdding(false);
            }} />}
          </div>
        </Card>
        {/* Keyed off selectedId, not detail: between the click and the fetch
            landing, detail is still null, and an empty state that blinks once
            per selection reads as a fault. */}
        {!selectedId && parts.length > 0 && (
          <EmptyState title="No part selected">
            Pick a row above to store its model file, keep its slice recipes,
            and slice one straight to a printer.
          </EmptyState>
        )}
        {detail && (
          <RecipeEditor
            part={detail.part}
            recipes={detail.recipes}
            defaultRecipeId={detail.default_recipe_id}
            printers={printers}
            selected={selected}
            busy={busy}
            onUploadModel={(file) => act(() => uploadPartModel(detail.part.id, file))}
            onAddRecipe={(body) => act(() => addRecipe(detail.part.id, body))}
            onMakeDefault={(rid) => act(() => updateRecipe(detail.part.id, rid, { is_default: true }))}
            onArchiveRecipe={(rid) => act(() => archiveRecipe(detail.part.id, rid).then(() => ({})))}
            onSlice={slice}
          />
        )}
      </div>
    </PageFrame>
  );
}
