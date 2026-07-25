import { useEffect, useState } from "react";
import { navGroups, pages } from "./app/pageRegistry.jsx";
import NavGroup from "./components/ui/NavGroup.jsx";
import StatusPill from "./components/ui/StatusPill.jsx";
import { usePrinters } from "./hooks/usePrinters.js";

const CONN = {
  ok: { status: "ok", label: "Connected" },
  stale: { status: "warn", label: "Stale" },
  disconnected: { status: "danger", label: "Printer offline" },
};
const SERVER_DOWN = { status: "danger", label: "Server offline" };
const NO_PRINTERS = { status: "warn", label: "No printers" };

export default function App() {
  const [active, setActive] = useState("overview");
  const [selected, setSelected] = useState(null);
  const { printers, robot, wsUp } = usePrinters();

  // One printer is the common case — never make the user pick. Also repairs
  // the selection when the selected printer is removed.
  useEffect(() => {
    if (printers.length === 1) setSelected(printers[0].serial);
    else if (selected && !printers.some((p) => p.serial === selected)) {
      setSelected(printers[0]?.serial ?? null);
    }
  }, [printers, selected]);

  const select = (serial) => {
    setSelected(serial);
    setActive("dashboard");
  };

  const current = printers.find((p) => p.serial === selected) ?? null;
  const online = printers.filter((p) => p.connection === "ok").length;

  let conn;
  if (!wsUp) conn = SERVER_DOWN;
  else if (printers.length === 0) conn = NO_PRINTERS;
  else if (current) conn = CONN[current.connection] ?? CONN.stale;
  else conn = { status: "ok", label: `${online} of ${printers.length} online` };

  const Page = pages[active].component;

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="sidebar__brand">bambu monitor</div>
        {Object.entries(navGroups()).map(([label, items]) => (
          <NavGroup key={label} label={label} items={items}
                    activeKey={active} onSelect={setActive} />
        ))}
      </aside>
      <div className="main">
        <header className="topbar">
          <span className="topbar__title">{pages[active].title}</span>
          <span className="topbar__host">
            {printers.length > 0
              ? `${printers.length} printer${printers.length === 1 ? "" : "s"} · ${online} online`
              : ""}
          </span>
          <StatusPill status={conn.status}>{conn.label}</StatusPill>
        </header>
        <div className={!wsUp ? "dimmed" : ""}>
          <Page printers={printers} selected={selected} onSelect={select}
                robot={robot} wsUp={wsUp} />
        </div>
      </div>
    </div>
  );
}
