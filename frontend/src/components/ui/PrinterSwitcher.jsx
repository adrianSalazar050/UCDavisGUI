import { useEffect, useRef, useState } from "react";
import { moveFocus, printerProgress, printerTone }
  from "../printers/printerStatus.js";
import StatusPill from "./StatusPill.jsx";

// The topbar printer picker: which machine every per-printer page is showing,
// and the only control that changes it.
//
// It lives in the topbar rather than on one page because the selection is
// global state that every page reads. Before this, the Printers page's card
// grid was the only way to change it, so "look at the other printer's queue"
// meant leaving the queue, clicking a card, and being navigated back to the
// dashboard -- three steps and a lost place for what is really one choice.
//
// Deliberately a menu, not a <select>: each row carries a status pill and the
// live layer/percent line, which a native option cannot render. That costs the
// keyboard behaviour a <select> gives for free, so it is implemented here --
// arrow keys, Home/End, Escape, click-outside, and roving tabindex so the menu
// is one tab stop rather than one per printer.
export default function PrinterSwitcher({ printers = [], selected,
                                          onSwitch, onManage }) {
  const [open, setOpen] = useState(false);
  const [cursor, setCursor] = useState(null);
  const rootRef = useRef(null);
  const buttonRef = useRef(null);
  const menuRef = useRef(null);

  const current = printers.find((p) => p.serial === selected) ?? null;
  const tone = printerTone(current);

  const close = () => setOpen(false);
  // The cursor is seeded HERE rather than in an Effect keyed on `printers`:
  // this component re-renders on every /ws push (up to 4 Hz), and an Effect
  // with `printers` in its deps would yank the highlight back to the selected
  // row every time a temperature changed, mid-keyboard-navigation.
  const openMenu = () => {
    const start = printers.findIndex((p) => p.serial === selected);
    setCursor(start >= 0 ? start : 0);
    setOpen(true);
  };

  // ...but the seeded value can go stale: `printers` is pushed from the server,
  // so another viewer on the --lan build can delete a printer while this menu
  // is open. `cursor` is therefore only a REQUEST for a row; `at` is a row that
  // actually exists. Reading `cursor` directly is what broke the menu: once it
  // pointed past the end, `i === cursor` was false for EVERY row, the roving
  // tabindex left the menu with zero tab stops, and a keyboard user could not
  // reach any printer at all (focus had fallen back to <body>, and the arrow
  // handler lives on the menu subtree).
  const at = cursor == null || printers.length === 0
    ? null
    : Math.min(cursor, printers.length - 1);

  // Escape and click-outside. Bound to the document only while open, and on
  // `pointerdown` rather than `click` so the menu is already gone by the time
  // a click lands on whatever is underneath it.
  useEffect(() => {
    if (!open) return undefined;
    const onPointerDown = (e) => {
      if (!rootRef.current?.contains(e.target)) close();
    };
    const onKeyDown = (e) => {
      if (e.key !== "Escape") return;
      // Read this BEFORE close(): only pull the focus ring back to the trigger
      // if the switcher still owns focus. This listener is on the document, so
      // an Escape pressed after the user has tabbed on to the sidebar would
      // otherwise yank them backwards across the page. (Checked first because
      // close() is what eventually unmounts the focused row and drops
      // document.activeElement to <body>.)
      const owns = rootRef.current?.contains(document.activeElement);
      close();
      if (owns) buttonRef.current?.focus();   // never orphan the focus ring
    };
    document.addEventListener("pointerdown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  // Move real DOM focus to the highlighted row. Keyed on the clamped index and
  // on printers.LENGTH -- deliberately not on `printers` itself, which is a new
  // array on every /ws push (up to 4 Hz) and would re-run this effect, yanking
  // focus back to the highlighted row every time a temperature changed.
  // Length is the safe signal because it only changes when a printer is added
  // or removed, which is exactly when the focused row can have unmounted and
  // left focus on <body>; a shrunk list then refocuses a row that still exists.
  useEffect(() => {
    if (!open || at == null) return;
    const rows = menuRef.current?.querySelectorAll("[data-printer-row]");
    rows?.[at]?.focus();
  }, [open, at, printers.length]);

  const onMenuKeyDown = (e) => {
    // `at`, not `cursor`: the arithmetic has to start from the row that is
    // really focused, or an ArrowDown after the list shrank looks like a no-op.
    const next = moveFocus(at, e.key, printers.length);
    if (next == null) return;      // not ours -- let Tab and Enter through
    e.preventDefault();
    setCursor(next);
  };

  // Tabbing out of the menu closes it. Without this the menu stayed open behind
  // the user, and the document-level Escape handler above could then reach back
  // into it.
  //
  // The relatedTarget != null guard is load-bearing: a null relatedTarget means
  // focus went nowhere in particular (the window lost focus, or Safari's
  // don't-focus-a-clicked-button behaviour fires blur on mousedown), and
  // treating that as "focus left the switcher" would unmount the row under the
  // pointer before its click landed. Clicks that genuinely land outside are
  // already handled by the document pointerdown listener.
  const onBlur = (e) => {
    if (e.relatedTarget && !rootRef.current?.contains(e.relatedTarget)) close();
  };

  const pick = (serial) => {
    close();
    buttonRef.current?.focus();
    if (serial !== selected) onSwitch(serial);
  };

  return (
    <div className="switcher" ref={rootRef} onBlur={onBlur}>
      <button
        ref={buttonRef}
        type="button"
        className="switcher__trigger"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => (open ? close() : openMenu())}
      >
        <span className={`switcher__dot switcher__dot--${tone.tone}`}
              aria-hidden="true" />
        <span className="switcher__name">
          {current ? current.name : "No printer"}
        </span>
        {/* Only when there IS one: printerTone(null)'s label is also
            "No printer", and the trigger read "No printer No printer". */}
        {current && <span className="switcher__state">{tone.label}</span>}
        <span className="switcher__caret" aria-hidden="true">▾</span>
      </button>

      {open && (
        <div className="switcher__menu" role="menu" ref={menuRef}
             aria-label="Switch printer" onKeyDown={onMenuKeyDown}>
          {printers.length === 0 ? (
            <div className="switcher__none">No printers registered yet.</div>
          ) : (
            printers.map((p, i) => {
              const t = printerTone(p);
              const progress = printerProgress(p);
              const isCurrent = p.serial === selected;
              return (
                <button
                  key={p.serial}
                  data-printer-row
                  type="button"
                  role="menuitemradio"
                  aria-checked={isCurrent}
                  // Roving tabindex: the menu is one tab stop, and the arrow
                  // keys move within it.
                  tabIndex={i === at ? 0 : -1}
                  className={`switcher__item${isCurrent ? " is-current" : ""}`}
                  onFocus={() => setCursor(i)}
                  onClick={() => pick(p.serial)}
                >
                  <span className="switcher__item-main">
                    <span className="switcher__item-name">{p.name}</span>
                    <span className="switcher__item-sub">
                      {progress ?? p.printer}
                    </span>
                  </span>
                  {p.capture && (
                    <span className="switcher__tag" title="The camera points here">
                      camera
                    </span>
                  )}
                  <StatusPill status={t.tone}>{t.label}</StatusPill>
                </button>
              );
            })
          )}
          {/* Same order as pick(): close, hand focus back to the trigger, THEN
              navigate. onManage() swaps the page out, so the focus call has to
              happen while the trigger is still mounted -- closing without it
              dropped document focus onto <body>. */}
          <button type="button" role="menuitem" className="switcher__manage"
                  onClick={() => {
                    close();
                    buttonRef.current?.focus();
                    onManage();
                  }}>
            {printers.length === 0 ? "Add a printer…" : "Manage printers…"}
          </button>
        </div>
      )}
    </div>
  );
}
