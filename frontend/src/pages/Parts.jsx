import { useCallback, useEffect, useRef, useState } from "react";
import { addRecipe, archiveRecipe, fetchPart, fetchParts, sliceFromPart,
         updateRecipe, uploadPartModel } from "../api/printer.js";
import PartForm from "../components/parts/PartForm.jsx";
import PartList from "../components/parts/PartList.jsx";
import RecipeEditor from "../components/parts/RecipeEditor.jsx";
import Card from "../components/ui/Card.jsx";
import PageFrame from "../components/ui/PageFrame.jsx";

const POLL_MS = 8000;

export default function Parts({ printers = [] }) {
  const [parts, setParts] = useState([]);
  const [selectedId, setSelectedId] = useState(null);
  const [detail, setDetail] = useState(null);
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState(null);   // slice-queued confirmation
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
  const slice = useCallback(async (recipeId, serial) => {
    setBusy(true); setNotice(null);
    try {
      await sliceFromPart(serial, detail.part.id, recipeId);
      const name = printers.find((p) => p.serial === serial)?.name || serial;
      setNotice(`Slicing started for ${name}. Watch it on the Slice page; the `
                + `finished job appears on that printer's Queue.`);
      setErr(null);
    } catch (e) { setErr(e.message); } finally { setBusy(false); }
  }, [detail, printers]);

  return (
    <PageFrame>
      <div className="stack">
        {err && <p className="error">{err}</p>}
        {notice && <p className="muted">{notice}</p>}
        <Card title="Add a part"><PartForm onCreated={(id) => {
          loadParts(); setSelectedId(id);
        }} /></Card>
        <Card title="Parts">
          <PartList parts={parts} selectedId={selectedId}
                    onSelect={setSelectedId} />
        </Card>
        {detail && (
          <Card title={`${detail.part.part_number} rev ${detail.part.revision}`
                       + (detail.part.name ? ` — ${detail.part.name}` : "")}>
            <RecipeEditor
              part={detail.part}
              recipes={detail.recipes}
              defaultRecipeId={detail.default_recipe_id}
              printers={printers}
              busy={busy}
              onUploadModel={(file) => act(() => uploadPartModel(detail.part.id, file))}
              onAddRecipe={(body) => act(() => addRecipe(detail.part.id, body))}
              onMakeDefault={(rid) => act(() => updateRecipe(detail.part.id, rid, { is_default: true }))}
              onArchiveRecipe={(rid) => act(() => archiveRecipe(detail.part.id, rid).then(() => ({})))}
              onSlice={slice}
            />
          </Card>
        )}
      </div>
    </PageFrame>
  );
}
