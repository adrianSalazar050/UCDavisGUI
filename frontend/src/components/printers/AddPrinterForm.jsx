import { useState } from "react";
import { addPrinter } from "../../api/printer.js";
import Button from "../ui/Button.jsx";
import Field from "../ui/Field.jsx";

const BLANK = { host: "", serial: "", access_code: "", name: "", capture: false };

export default function AddPrinterForm() {
  const [form, setForm] = useState(BLANK);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);

  const set = (k) => (e) =>
    setForm((f) => ({
      ...f,
      [k]: e.target.type === "checkbox" ? e.target.checked : e.target.value,
    }));

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    setErr(null);
    try {
      await addPrinter({
        host: form.host.trim(),
        serial: form.serial.trim(),
        access_code: form.access_code.trim(),
        name: form.name.trim(),
        capture: form.capture,
      });
      setForm(BLANK); // /ws pushes the new card in on its own
    } catch (e2) {
      setErr(e2.message);
    } finally {
      setBusy(false);
    }
  };

  const ready = form.host.trim() && form.serial.trim() && form.access_code.trim();

  return (
    <form className="add-form" onSubmit={submit}>
      <div className="add-form__row">
        <Field label="IP address" value={form.host} onChange={set("host")}
               placeholder="192.168.137.2"
               help="Printer screen: Settings → WLAN" />
        <Field label="Serial" value={form.serial} onChange={set("serial")}
               placeholder="0300CA633005010"
               help="Settings → Device, or the sticker" />
      </div>
      <div className="add-form__row">
        <Field label="LAN access code" value={form.access_code}
               onChange={set("access_code")} placeholder="31661007"
               help="Usually 8 characters. Rotates on some firmware updates." />
        <Field label="Name (optional)" value={form.name} onChange={set("name")}
               placeholder="A1-bench" help="Defaults to the IP address" />
      </div>
      <label className="add-form__check">
        <input type="checkbox" checked={form.capture} onChange={set("capture")} />
        This printer is the one the webcam points at
      </label>
      {err && <div className="add-form__error">{err}</div>}
      <div className="add-form__actions">
        {/*
          Button.jsx computes `disabled={busy || rest.disabled}` but then
          spreads {...rest} AFTER that attribute in its JSX — so when a
          caller passes its own `disabled` prop, that raw value silently
          overwrites Button's merged one, dropping `busy` entirely. Verified
          empirically: passing busy={true} disabled={false} together yields
          a DOM button with disabled=false, not true. Can't fix Button.jsx
          (out of scope), so pre-merge `busy` into the value passed down —
          the overwrite then lands on the already-correct result.
        */}
        <Button type="submit" variant="primary" busy={busy} disabled={busy || !ready}>
          Connect
        </Button>
        <span className="ui-field__help">
          Requires LAN-only Mode and Developer Mode on the printer.
        </span>
      </div>
    </form>
  );
}
