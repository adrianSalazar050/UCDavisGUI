import { useState } from "react";
import { navGroups, pages } from "./app/pageRegistry.jsx";
import NavGroup from "./components/ui/NavGroup.jsx";
import StatusPill from "./components/ui/StatusPill.jsx";
import { usePrinter } from "./hooks/usePrinter.js";

const CONN = {
  ok: { status: "ok", label: "Connected" },
  stale: { status: "warn", label: "Stale" },
  disconnected: { status: "danger", label: "Printer offline" },
};
const SERVER_DOWN = { status: "danger", label: "Server offline" };

export default function App() {
  const [active, setActive] = useState("dashboard");
  const { summary, wsUp } = usePrinter();

  const Page = pages[active].component;
  const conn = wsUp ? (CONN[summary?.connection] ?? CONN.stale) : SERVER_DOWN;

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
          <span className="topbar__host">{summary?.printer ?? ""}</span>
          <StatusPill status={conn.status}>{conn.label}</StatusPill>
        </header>
        <div className={conn.status === "danger" ? "dimmed" : ""}>
          <Page summary={summary} />
        </div>
      </div>
    </div>
  );
}
