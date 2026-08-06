import { useEffect, useState } from "react";
import { HOME, navGroups, pages } from "./app/pageRegistry.jsx";
import LoginScreen from "./components/auth/LoginScreen.jsx";
import NavGroup from "./components/ui/NavGroup.jsx";
import PrinterSwitcher from "./components/ui/PrinterSwitcher.jsx";
import StatusPill from "./components/ui/StatusPill.jsx";
import { fleetSummary } from "./components/printers/printerStatus.js";
import { checkAuth } from "./api/printer.js";
import { usePrinters } from "./hooks/usePrinters.js";

const SERVER_DOWN = { status: "danger", label: "Server offline" };
const NO_PRINTERS = { status: "warn", label: "No printers" };
const FLEET_SCOPE_HINT = "Most of this page covers the whole lab; only its "
                         + "per-printer controls follow the switcher.";

// Auth gate. Probes a protected route once on mount; the dashboard proper only
// mounts once we're allowed in, so usePrinters' WebSocket never opens (and
// never retries) against a server that would just close it with 1008.
// A localhost/desktop server has no auth, so the probe passes and this is
// invisible. If the probe itself fails (server down) we render the dashboard
// anyway -- that surfaces as "Server offline", which is the honest message,
// rather than a login box nobody can satisfy.
export default function App() {
  const [authed, setAuthed] = useState(null);   // null = still probing

  useEffect(() => {
    let alive = true;
    checkAuth()
      .then((ok) => alive && setAuthed(ok))
      .catch(() => alive && setAuthed(true));
    return () => { alive = false; };
  }, []);

  if (authed === null) return null;             // no flash of the wrong screen
  if (!authed) return <LoginScreen onSuccess={() => setAuthed(true)} />;
  return <Shell />;
}

function Shell() {
  const [active, setActive] = useState(HOME);
  const [selected, setSelected] = useState(null);
  // Narrow screens only: the sidebar folds away and this reveals it. Always
  // false on a desktop width, where CSS keeps the sidebar shown regardless.
  const [navOpen, setNavOpen] = useState(false);
  const { printers, wsUp } = usePrinters();

  // Never make the user pick before showing them anything, and repair the
  // selection when the selected printer is removed.
  //
  // This used to auto-select only when there was exactly ONE printer, because
  // the app opened on the printer grid and picking a card was how you got
  // anywhere. Now that the app opens on the dashboard, that left a lab with
  // three machines staring at an empty page — so a missing or stale selection
  // falls back to the first printer whatever the fleet size. Landing on a live
  // machine beats landing on a chooser, and the topbar switcher makes changing
  // it one click from wherever you are.
  useEffect(() => {
    if (printers.length === 0) {
      if (selected !== null) setSelected(null);
      return;
    }
    if (!printers.some((p) => p.serial === selected)) {
      setSelected(printers[0].serial);
    }
  }, [printers, selected]);

  const go = (key) => {
    setActive(key);
    setNavOpen(false);       // a narrow-screen tap on a nav item closes the menu
  };

  // Clicking a printer CARD means "show me this machine", so it selects and
  // jumps to the dashboard. The topbar switcher deliberately does NOT: changing
  // printer while reading a queue should leave you looking at that printer's
  // queue, not throw you back to the dashboard. Two different intentions, so
  // two different handlers over the same piece of state.
  const select = (serial) => {
    setSelected(serial);
    go("dashboard");
  };

  const page = pages[active] ?? pages[HOME];

  // The topbar pill is about the SERVER and the fleet; the switcher beside it
  // carries the selected printer's own state, so this no longer restates it.
  //
  // The tone has to AGREE with the text: hardcoding "ok" for any non-empty
  // fleet rendered "1 printer · 0 online" in green, which reads as "all well"
  // for a lab where nothing is reachable. So the tone counts online machines --
  // none online is danger, some-but-not-all is warn, all online is ok -- using
  // the same connection === "ok" test fleetSummary() counts with, or the pill
  // could say "0 online" while glowing green again.
  let conn;
  if (!wsUp) conn = SERVER_DOWN;
  else if (printers.length === 0) conn = NO_PRINTERS;
  else {
    const online = printers.filter((p) => p.connection === "ok").length;
    let status;
    if (online === 0) status = "danger";
    else if (online < printers.length) status = "warn";
    else status = "ok";
    conn = { status, label: fleetSummary(printers) };
  }

  const Page = page.component;

  return (
    <div className={`shell${navOpen ? " shell--nav-open" : ""}`}>
      <aside className="sidebar" id="sidebar-nav">
        <div className="sidebar__brand">bambu monitor</div>
        {Object.entries(navGroups()).map(([label, items]) => (
          <NavGroup key={label} label={label} items={items}
                    activeKey={active} onSelect={go} />
        ))}
      </aside>
      <div className="main">
        <header className="topbar">
          <button type="button" className="topbar__menu"
                  aria-label={navOpen ? "Hide navigation" : "Show navigation"}
                  aria-expanded={navOpen} aria-controls="sidebar-nav"
                  onClick={() => setNavOpen((v) => !v)}>
            <span aria-hidden="true">☰</span>
          </button>
          <div className="topbar__heading">
            <h1 className="topbar__title">{page.title}</h1>
            {/* "not one printer" was too strong for the pages this is actually
                applied to: Inventory's loaded-spool card and the printer a
                Parts recipe slices to BOTH follow the switcher, and both pages
                tell the reader to use it. The tooltip has to hold for every
                fleet page, so it now claims only what pageRegistry's `scope`
                claims: whole-lab page, per-printer controls excepted. */}
            {page.scope === "fleet" && (
              <span className="topbar__scope" title={FLEET_SCOPE_HINT}>
                Fleet-wide
              </span>
            )}
          </div>
          <PrinterSwitcher printers={printers} selected={selected}
                           onSwitch={setSelected}
                           onManage={() => go("printers")} />
          <StatusPill status={conn.status}>{conn.label}</StatusPill>
        </header>
        <div className={!wsUp ? "dimmed" : ""}>
          {page.description && (
            <p className="pagehead">{page.description}</p>
          )}
          <Page printers={printers} selected={selected}
                onSelect={select} onNavigate={go} />
        </div>
      </div>
    </div>
  );
}
