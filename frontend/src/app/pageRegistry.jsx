import Dashboard from "../pages/Dashboard.jsx";

// Every page: key -> { title, group, component }. The sidebar and topbar
// are derived from this — add future pages (runs browser, print control)
// here and nowhere else.
export const pages = {
  dashboard: { title: "Dashboard", group: "Monitor", component: Dashboard },
};

export function navGroups() {
  const groups = {};
  for (const [key, page] of Object.entries(pages)) {
    (groups[page.group] ??= []).push({ key, title: page.title });
  }
  return groups;
}
