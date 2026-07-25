import { useState } from "react";
import Button from "../ui/Button.jsx";
import Field from "../ui/Field.jsx";

const TIERS = ["standard", "fine", "draft"];
const BLANK = { name: "", preset_tier: "standard", filament_material: "",
                copies_per_plate: 1, supports: false, is_default: false };

// Recipes for one part, plus the model-file upload. All mutations go through
// the page's `act(fn)` so the whole part payload refreshes after each -- same
// pattern as the History page's piece edits.
export default function RecipeEditor({ part, recipes, defaultRecipeId,
                                       printers = [], busy,
                                       onUploadModel, onAddRecipe,
                                       onMakeDefault, onArchiveRecipe,
                                       onSlice }) {
  const [form, setForm] = useState(BLANK);
  // Which printer a "Slice" button targets. Default to the first printer.
  const [target, setTarget] = useState(printers[0]?.serial || "");
  const canSlice = onSlice && part.model_filename && printers.length > 0;
  const set = (k) => (e) => setForm((f) => ({
    ...f, [k]: e.target.type === "checkbox" ? e.target.checked : e.target.value }));

  const add = async (e) => {
    e.preventDefault();
    await onAddRecipe({
      name: form.name.trim() || `${form.preset_tier} recipe`,
      preset_tier: form.preset_tier,
      filament_material: form.filament_material.trim() || null,
      copies_per_plate: Number(form.copies_per_plate) || 1,
      supports: form.supports,
      is_default: form.is_default,
    });
    setForm(BLANK);
  };

  return (
    <div className="stack">
      <div className="row">
        <label className="ui-btn ui-btn--secondary">
          {part.model_filename ? "Replace model" : "Upload model"}
          <input type="file" style={{ display: "none" }} disabled={busy}
                 accept=".stl,.3mf,.step,.stp,.obj"
                 onChange={(e) => {
                   const f = e.target.files?.[0];
                   if (f) onUploadModel(f);
                   e.target.value = "";
                 }} />
        </label>
        {part.model_filename && (
          <span className="ui-field__help">
            {part.model_filename} ({part.model_bytes} bytes)
          </span>
        )}
        {canSlice && (
          <label className="ui-field__help">
            Slice for:{" "}
            <select value={target} disabled={busy}
                    onChange={(e) => setTarget(e.target.value)}>
              {printers.map((p) => (
                <option key={p.serial} value={p.serial}>
                  {p.name || p.serial}
                </option>
              ))}
            </select>
          </label>
        )}
      </div>

      <table className="table">
        <thead>
          <tr><th>Recipe</th><th>Tier</th><th>Material</th><th>Copies</th>
              <th>Default</th><th></th><th></th></tr>
        </thead>
        <tbody>
          {recipes.length === 0 && (
            <tr><td colSpan={7} className="muted">No recipes yet.</td></tr>
          )}
          {recipes.map((r) => (
            <tr key={r.id}>
              <td>{r.name}</td>
              <td>{r.preset_tier || "—"}</td>
              <td>{r.filament_material || "—"}</td>
              <td>{r.copies_per_plate}</td>
              <td>
                {r.id === defaultRecipeId
                  ? <span className="pill pill-ok">default</span>
                  : <Button size="sm" disabled={busy}
                            onClick={() => onMakeDefault(r.id)}>Make default</Button>}
              </td>
              <td>
                {canSlice && (
                  <Button size="sm" variant="primary" disabled={busy || !target}
                          onClick={() => onSlice(r.id, target)}>Slice</Button>
                )}
              </td>
              <td>
                <Button size="sm" disabled={busy}
                        onClick={() => onArchiveRecipe(r.id)}>Remove</Button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      <form className="add-form" onSubmit={add}>
        <div className="add-form__row">
          <Field label="Recipe name" value={form.name} onChange={set("name")}
                 placeholder="Standard PLA" />
          <Field label="Quality">
            <select value={form.preset_tier} onChange={set("preset_tier")}>
              {TIERS.map((t) => <option key={t} value={t}>{t}</option>)}
            </select>
          </Field>
        </div>
        <div className="add-form__row">
          <Field label="Filament (optional)" value={form.filament_material}
                 onChange={set("filament_material")} placeholder="PLA" />
          <Field label="Copies per plate" type="number" min="1"
                 value={form.copies_per_plate} onChange={set("copies_per_plate")} />
        </div>
        <label className="add-form__check">
          <input type="checkbox" checked={form.supports} onChange={set("supports")} />
          Tree supports
        </label>
        <label className="add-form__check">
          <input type="checkbox" checked={form.is_default} onChange={set("is_default")} />
          Make this the default recipe
        </label>
        <div className="add-form__actions">
          <Button type="submit" variant="primary" busy={busy}>Add recipe</Button>
        </div>
      </form>
    </div>
  );
}
