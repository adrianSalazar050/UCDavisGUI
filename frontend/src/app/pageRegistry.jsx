import Dashboard from "../pages/Dashboard.jsx";
import Detection from "../pages/Detection.jsx";
import History from "../pages/History.jsx";
import Inventory from "../pages/Inventory.jsx";
import Parts from "../pages/Parts.jsx";
import Printers from "../pages/Printers.jsx";
import Queue from "../pages/Queue.jsx";
import Robot from "../pages/Robot.jsx";
import SdFiles from "../pages/SdFiles.jsx";
import Slice from "../pages/Slice.jsx";

// Every page: key -> { title, description, group, scope, component }. The
// sidebar, the topbar and each page's one-line description are all derived
// from this -- add future pages here and nowhere else.
//
// Every page receives the same props: { printers, selected, onSelect,
// onNavigate, robot, wsUp }. `onSelect(serial)` selects a printer AND jumps to
// its dashboard (what clicking a card on the Printers page means);
// `onNavigate(key)` goes to another page without touching the selection, which
// is what lets an empty state offer the fix rather than just naming it.
// `robot` is the live arm snapshot off the same WebSocket that carries
// `printers` (null when this server has no arm), and `wsUp` says whether that
// socket is currently open -- the Robot page needs both to decide whether it
// is safe to enable a motion control, so they ride the uniform contract rather
// than being threaded to one page specially.
//
// GROUPS ARE THE ANSWER TO A QUESTION, not a taxonomy of the code. All nine
// pages used to sit in one list called "Monitor", which said nothing about
// which of them you needed:
//
//   Monitor  -- what is this machine doing right now
//   Print    -- get a job onto it, in the order the work actually happens:
//               slice a model -> queue it -> the card it lives on
//   Library  -- fleet-wide records that outlive any one printer
//   Setup    -- registering machines, which you do once
//   Control  -- driving hardware that is not a printer: the arm that moves
//               plates between them. Its own group because it answers its own
//               question; folding it into "Print" would file a robot under a
//               heading about print jobs.
//
// `scope` records the distinction master.md section 7 already drew: a
// "printer" page shows the printer the topbar switcher is pointed at, a
// "fleet" page shows the whole lab and uses the selection only for its
// per-printer actions. The topbar badges the fleet ones, so switching printer
// and seeing the page not change is explained rather than surprising.
export const pages = {
  // ---- Monitor: this printer, live -------------------------------------
  dashboard: {
    title: "Dashboard",
    description: "Live state, progress, temperatures and the camera view for "
                 + "the selected printer.",
    group: "Monitor",
    scope: "printer",
    component: Dashboard,
  },
  detection: {
    title: "Detection",
    description: "Tune the failure detector, aim it at the bed, and arm the "
                 + "classes it may stop a print for.",
    group: "Monitor",
    scope: "printer",
    component: Detection,
  },
  history: {
    title: "History",
    description: "Every run this printer has recorded, with per-piece "
                 + "verdicts and the filament it consumed.",
    group: "Monitor",
    scope: "printer",
    component: History,
  },

  // ---- Print: getting a job onto the machine, in workflow order --------
  slice: {
    title: "Slice",
    description: "Turn a model into a startable job with Bambu Studio, then "
                 + "queue it automatically.",
    group: "Print",
    scope: "printer",
    component: Slice,
  },
  queue: {
    title: "Queue",
    description: "Plan what prints next, with time and filament totals — and "
                 + "start it.",
    group: "Print",
    scope: "printer",
    component: Queue,
  },
  sdfiles: {
    title: "SD Files",
    description: "Browse the printer's microSD card over FTPS, and upload to "
                 + "it.",
    group: "Print",
    scope: "printer",
    component: SdFiles,
  },

  // ---- Library: fleet-wide records -------------------------------------
  parts: {
    title: "Parts",
    description: "The catalogue: part numbers, revisions, model files and the "
                 + "slice recipes that produce them.",
    group: "Library",
    scope: "fleet",
    component: Parts,
  },
  inventory: {
    title: "Inventory",
    description: "Filament spools and what is left on them. Mark which spool "
                 + "is loaded so finished prints charge the right one.",
    group: "Library",
    scope: "fleet",
    component: Inventory,
  },

  // ---- Control: hardware that is not a printer -------------------------
  robot: {
    title: "Robot",
    description: "Drive the plate-handling arm: home it, jog it, and run "
                 + "pick, place and transfer against an ArUco marker.",
    group: "Control",
    // "fleet", not "printer": one arm serves the whole lab and none of its
    // controls are addressed to the printer the topbar switcher points at, so
    // switching printer must not look like it re-aims the robot.
    scope: "fleet",
    component: Robot,
  },

  // ---- Setup: done once ------------------------------------------------
  printers: {
    title: "Printers",
    description: "Register a printer, correct its address or model, and choose "
                 + "which one the camera is pointed at.",
    group: "Setup",
    scope: "fleet",
    component: Printers,
  },
};

// Sidebar order. Explicit rather than derived from `pages` insertion order so
// that adding a page to an existing group can never reshuffle the groups
// themselves, and so a page whose group is misspelled is visible as its own
// heading at the end instead of vanishing.
export const GROUP_ORDER = ["Monitor", "Print", "Library", "Setup",
                            "Control"];

// The page the app opens on. The dashboard, not the printer list: with one
// printer -- the common case -- App auto-selects it, so this lands on
// something live. With none, the dashboard's empty state offers "Add a
// printer" and takes you here.
export const HOME = "dashboard";

export function navGroups() {
  const groups = {};
  for (const [key, page] of Object.entries(pages)) {
    (groups[page.group] ??= []).push({ key, title: page.title });
  }
  const ordered = {};
  for (const label of GROUP_ORDER) {
    if (groups[label]) ordered[label] = groups[label];
  }
  // Anything in a group GROUP_ORDER doesn't know about still renders, last.
  for (const [label, items] of Object.entries(groups)) {
    if (!(label in ordered)) ordered[label] = items;
  }
  return ordered;
}
