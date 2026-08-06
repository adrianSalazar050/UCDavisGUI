import Button from "../ui/Button.jsx";
import Field from "../ui/Field.jsx";

// "Which spool is loaded on this printer" -- the operator-set link that lets a
// finished run decrement the right spool (the printer can't reliably report
// its own spool). `loaded` is the current one (or null); `spools` is the
// fleet's non-empty spools to choose from.
export default function LoadedSpool({ printer, loaded, spools, busy,
                                      onLoad, onUnload }) {
  const name = printer.name || printer.serial;
  return (
    <div className="stack">
      {/* Says what the setting DOES and why a human has to keep it true. The
          consequence of getting it wrong is invisible for weeks: grams keep
          being charged, just to the wrong reel. */}
      <p className="muted">
        Filament for every print that finishes on {name} is charged against the
        spool named here. Nothing else can set it — the printer cannot tell the
        server which reel is on its holder.
      </p>
      {/* A Field, not the bare dropdown in a label-and-select strip this used
          to be: it is a saved setting, and it deserves to look like one next
          to the fleet table above it. */}
      <Field label={`Spool loaded on ${name}`}
             help="Change it the moment the filament is swapped at the machine.">
        <select value={loaded?.id || ""} disabled={busy}
                onChange={(e) => (e.target.value
                  ? onLoad(e.target.value)
                  : onUnload())}>
          <option value="">— none —</option>
          {spools.map((s) => (
            <option key={s.id} value={s.id}>
              {s.spool_code} ({s.material}{s.colour ? `, ${s.colour}` : ""})
            </option>
          ))}
        </select>
      </Field>
      {loaded && (
        <div className="row">
          {loaded.remaining_grams != null && (
            <span>
              <strong>{Math.round(loaded.remaining_grams)} g</strong> remaining
              on {loaded.spool_code}.
            </span>
          )}
          {/* Not "Unload": on a Bambu that is a filament procedure the machine
              performs. This only clears the record, which is also what the
              "— none —" option above does. */}
          <Button size="sm" disabled={busy} onClick={onUnload}>
            Set to none
          </Button>
        </div>
      )}
    </div>
  );
}
