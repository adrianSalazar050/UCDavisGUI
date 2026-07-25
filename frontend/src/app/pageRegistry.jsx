import Dashboard from "../pages/Dashboard.jsx";
import Detection from "../pages/Detection.jsx";
import Overview from "../pages/Overview.jsx";
import Queue from "../pages/Queue.jsx";
import Robot from "../pages/Robot.jsx";
import SdFiles from "../pages/SdFiles.jsx";

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
  queue: { title: "Queue", group: "Monitor", component: Queue },
  robot: { title: "Robot", group: "Control", component: Robot },
};

export function navGroups() {
  const groups = {};
  for (const [key, page] of Object.entries(pages)) {
    (groups[page.group] ??= []).push({ key, title: page.title });
  }
  return groups;
}
