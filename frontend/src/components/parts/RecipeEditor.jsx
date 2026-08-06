import { useState } from "react";
import Button from "../ui/Button.jsx";
import Card from "../ui/Card.jsx";
import Field from "../ui/Field.jsx";

const TIERS = ["standard", "fine", "draft"];
const BLANK = { name: "", preset_tier: "standard", filament_material: "",
                copies_per_plate: 1, supports: false, is_default: false };

// Recipes for one part, plus the model-file upload. All mutations go through
// the page's `act(fn)` so the whole part payload refreshes after each -- same
// pattern as the History page's piece edits.
//
// Three concerns, one card each, because they used to share a single flat
// stack: a file-upload button, a printer picker and a table all sat on the
// same line, so nothing said which control belonged to which job. Card 1 is
// the part's model file, card 2 is its recipes (and where a slice goes), card
// 3 adds a recipe.
export default function RecipeEditor({ part, recipes, defaultRecipeId,
                                       printers = [], selected, busy,
                                       onUploadModel, onAddRecipe,
                                       onMakeDefault, onArchiveRecipe,
                                       onSlice }) {
  const [form, setForm] = useState(BLANK);
  // Which printer a "Slice" button targets. `null` means "whatever the topbar
  // switcher is on": with a switcher always on screen, a second picker holding
  // its own independent value is a trap -- it silently keeps pointing at the
  // printer that happened to be selected when this mounted. Only an explicit
  // choice here overrides the topbar, and then only for these Slice buttons.
  const [override, setOverride] = useState(null);
  const target = override || selected || printers[0]?.serial || "";
  const canSlice = onSlice && part.model_filename && printers.length > 0;
  const overriding = target && selected && target !== selected;
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

  const heading = `${part.part_number} rev ${part.revision}`
                  + (part.name ? ` — ${part.name}` : "");

  return (
    <>
      <Card title={heading}>
        {/* The upload control must stay a flex item: `.ui-btn` sets a height,
            and a height does nothing on an inline <label>. */}
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
          <span className="ui-field__help">
            {part.model_filename
              ? `${part.model_filename} (${part.model_bytes} bytes)`
              : "No model stored yet. Every recipe below slices this one file, "
                + "so upload it first: STL, 3MF, STEP or OBJ."}
          </span>
        </div>
      </Card>

      <Card title="Recipes">
        <div className="stack">
          {canSlice ? (
            <Field
              label="Send a slice to"
              help={overriding
                ? "Overriding the topbar: these Slice buttons send to this "
                  + "printer, and nothing else on screen follows them."
                : "Follows the printer in the topbar. Change it to send just "
                  + "these slices to a different machine."}>
              <select value={target} disabled={busy}
                      onChange={(e) => setOverride(e.target.value)}>
                {printers.map((p) => (
                  <option key={p.serial} value={p.serial}>
                    {p.name || p.serial}
                  </option>
                ))}
              </select>
            </Field>
          ) : (
            <p className="muted">
              {part.model_filename
                ? "Slicing needs a registered printer."
                : "Slicing needs the model file above."}
            </p>
          )}

          <table className="table">
            <thead>
              <tr><th>Recipe</th><th>Tier</th><th>Material</th>
                  <th className="table__num">Copies</th>
                  <th>Default</th><th></th><th></th></tr>
            </thead>
            <tbody>
              {recipes.length === 0 && (
                <tr><td colSpan={7} className="muted">
                  No recipes yet — add one below to slice this part.
                </td></tr>
              )}
              {recipes.map((r) => (
                <tr key={r.id}>
                  <td>{r.name}</td>
                  <td>{r.preset_tier || "—"}</td>
                  <td>{r.filament_material || "—"}</td>
                  <td className="table__num">{r.copies_per_plate}</td>
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
        </div>
      </Card>

      <Card title="Add a recipe">
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
      </Card>
    </>
  );
}
