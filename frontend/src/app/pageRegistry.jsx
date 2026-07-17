import Dashboard from "../pages/Dashboard.jsx";
import Overview from "../pages/Overview.jsx";
import SdFiles from "../pages/SdFiles.jsx";

// Every page: key -> { title, group, component }. The sidebar and topbar
// are derived from this — add future pages (runs browser, print control)
// here and nowhere else.
//
// Every page receives the same props: { printers, selected, onSelect }.
export const pages = {
  overview: { title: "Overview", group: "Monitor", component: Overview },
  dashboard: { title: "Dashboard", group: "Monitor", component: Dashboard },
  sdfiles: { title: "SD Files", group: "Monitor", component: SdFiles },
};

export function navGroups() {
  const groups = {};
  for (const [key, page] of Object.entries(pages)) {
    (groups[page.group] ??= []).push({ key, title: page.title });
  }
  return groups;
}
