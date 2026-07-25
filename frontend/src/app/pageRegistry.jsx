import Dashboard from "../pages/Dashboard.jsx";
import Detection from "../pages/Detection.jsx";
import History from "../pages/History.jsx";
import Inventory from "../pages/Inventory.jsx";
import Overview from "../pages/Overview.jsx";
import Parts from "../pages/Parts.jsx";
import Queue from "../pages/Queue.jsx";
import Robot from "../pages/Robot.jsx";
import SdFiles from "../pages/SdFiles.jsx";
import Slice from "../pages/Slice.jsx";

// Every page: key -> { title, group, component }. The sidebar and topbar
// are derived from this — add future pages (runs browser, print control)
// here and nowhere else.
//
// Every page receives the same props: { printers, selected, onSelect }.
export const pages = {
  overview: { title: "Overview", group: "Monitor", component: Overview },
  dashboard: { title: "Dashboard", group: "Monitor", component: Dashboard },
  detection: { title: "Detection", group: "Monitor", component: Detection },
  sdfiles: { title: "SD Files", group: "Monitor", component: SdFiles },
  // Before queue: slicing feeds the queue.
  slice: { title: "Slice", group: "Monitor", component: Slice },
  queue: { title: "Queue", group: "Monitor", component: Queue },
  // After queue: history is what the queue turns into once a job has run.
  history: { title: "History", group: "Monitor", component: History },
  parts: { title: "Parts", group: "Monitor", component: Parts },
  inventory: { title: "Inventory", group: "Monitor", component: Inventory },
  // Not a printer page: drives the arm, ignores the printers/selected props.
  robot: { title: "Robot Arm", group: "Control", component: Robot },
};

export function navGroups() {
  const groups = {};
  for (const [key, page] of Object.entries(pages)) {
    (groups[page.group] ??= []).push({ key, title: page.title });
  }
  return groups;
}
