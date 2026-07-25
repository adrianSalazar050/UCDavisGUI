import { useState } from "react";
import { createPart } from "../../api/printer.js";
import Button from "../ui/Button.jsx";
import Field from "../ui/Field.jsx";

const BLANK = { part_number: "", revision: "A", name: "" };

// Add a catalogue part. On success, tells the page the new id so it can select
// it. part_number + revision are unique -- a duplicate comes back as a 409,
// surfaced in the error line.
export default function PartForm({ onCreated }) {
  const [form, setForm] = useState(BLANK);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const set = (k) => (e) => setForm((f) => ({ ...f, [k]: e.target.value }));

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true); setErr(null);
    try {
      const { part } = await createPart({
        part_number: form.part_number.trim(),
        revision: form.revision.trim() || "A",
        name: form.name.trim(),
      });
      setForm(BLANK);
      onCreated?.(part.id);
    } catch (e2) { setErr(e2.message); } finally { setBusy(false); }
  };

  return (
    <form className="add-form" onSubmit={submit}>
      <div className="add-form__row">
        <Field label="Part number" value={form.part_number}
               onChange={set("part_number")} placeholder="BRK-100" />
        <Field label="Revision" value={form.revision}
               onChange={set("revision")} placeholder="A" />
      </div>
      <Field label="Name (optional)" value={form.name} onChange={set("name")}
             placeholder="Bracket" />
      {err && <div className="add-form__error">{err}</div>}
      <div className="add-form__actions">
        <Button type="submit" variant="primary" busy={busy}
                disabled={!form.part_number.trim()}>Add part</Button>
      </div>
    </form>
  );
}
