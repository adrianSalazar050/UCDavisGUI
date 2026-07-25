import Button from "../ui/Button.jsx";

// "Which spool is loaded on this printer" -- the operator-set link that lets a
// finished run decrement the right spool (the printer can't reliably report
// its own spool). `loaded` is the current one (or null); `spools` is the
// fleet's non-empty spools to choose from.
export default function LoadedSpool({ printer, loaded, spools, busy,
                                      onLoad, onUnload }) {
  return (
    <div className="stack">
      <p className="ui-field__help">
        The spool loaded here is charged for filament when a print on{" "}
        {printer.name || printer.serial} finishes.
      </p>
      <div className="row">
        <span className="ui-field__label">Loaded:</span>
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
        {loaded && (
          <Button size="sm" disabled={busy} onClick={onUnload}>Unload</Button>
        )}
      </div>
      {loaded && loaded.remaining_grams != null && (
        <span className="ui-field__help">
          {Math.round(loaded.remaining_grams)} g remaining on {loaded.spool_code}.
        </span>
      )}
    </div>
  );
}
