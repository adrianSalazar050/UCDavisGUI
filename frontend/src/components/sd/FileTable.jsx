import EmptyState from "../ui/EmptyState.jsx";

const UNITS = ["B", "KB", "MB", "GB"];

function humanSize(bytes) {
  if (bytes == null) return "—";
  let n = bytes;
  let u = 0;
  while (n >= 1024 && u < UNITS.length - 1) {
    n /= 1024;
    u += 1;
  }
  return `${u === 0 ? n : n.toFixed(1)} ${UNITS[u]}`;
}

// The LIST fallback reports no mtime (see server/sdcard.py) — render that as
// an em dash rather than an empty cell.
function when(mtime) {
  if (!mtime) return "—";
  const d = new Date(mtime);
  return Number.isNaN(d.getTime()) ? mtime : d.toLocaleString();
}

// What a row IS, in the terms that decide what you can do with it. A listing
// where the only difference between a folder, a startable job and a file this
// app can never launch is a trailing slash makes the reader open things to
// find out. The two printable shapes are NOT interchangeable, and the upload
// route already says so in the same words (server/main.py upload_file):
//   .gcode.3mf — the queue can start it (project_file points at
//                Metadata/plate_N.gcode inside the zip, master.md 5.4)
//   .gcode     — the printer's own screen prints it, but there is no verified
//                MQTT command to launch one, so the queue cannot
// Classification only: it changes what a row SAYS, never what clicking it does.
function kindOf(entry) {
  if (entry.is_dir) return { label: "Folder", tone: null, hint: null };
  const lowered = entry.name.toLowerCase();
  if (lowered.endsWith(".gcode.3mf")) {
    return {
      label: "Queue-startable",
      tone: "ok",
      hint: "A sliced job: the Queue page can add it and start it from here.",
    };
  }
  if (lowered.endsWith(".gcode")) {
    return {
      label: "Screen only",
      tone: "warn",
      hint: "Raw .gcode prints from the printer's own screen. The queue here "
            + "cannot start it — slice to .gcode.3mf for that.",
    };
  }
  return { label: "File", tone: null, hint: null };
}

export default function FileTable({ entries, onOpen }) {
  if (entries.length === 0) {
    return (
      <EmptyState title="This folder is empty">
        Nothing at this path on the card. Uploads always land in the card root,
        whichever folder you are browsing, so a file you just sent is never in
        a subfolder.
      </EmptyState>
    );
  }
  return (
    // Size stays the SECOND column: its right alignment comes from an
    // nth-child(2) rule in styles.css, not from a class on the cell, so Type
    // goes after it rather than beside the name.
    <table className="file-table">
      <thead>
        <tr><th>Name</th><th>Size</th><th>Type</th><th>Modified</th></tr>
      </thead>
      <tbody>
        {entries.map((e, i) => {
          const kind = kindOf(e);
          return (
            // Names are unique within a real filesystem directory, but the
            // listing comes off the wire (MLSD/LIST parsed from a printer's
            // FTPS server) — a corrupt or duplicated line shouldn't produce a
            // duplicate React key. Index disambiguates without hiding the
            // duplicate entry itself.
            <tr key={`${i}-${e.name}`}>
              <td>
                {e.is_dir ? (
                  <button type="button" className="file-table__dir"
                          onClick={() => onOpen(e.name)}>
                    {e.name}/
                  </button>
                ) : e.name}
              </td>
              <td className={e.size == null ? "file-table__muted" : undefined}>
                {humanSize(e.size)}
              </td>
              <td>
                {/* A pill only where the answer changes what you can do with
                    the row; a folder and an ordinary file are facts, not
                    states, so they stay quiet text. */}
                {kind.tone ? (
                  <span className={`pill pill-${kind.tone}`} title={kind.hint}>
                    {kind.label}
                  </span>
                ) : (
                  <span className="file-table__muted">{kind.label}</span>
                )}
              </td>
              <td className="file-table__muted">{when(e.mtime)}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}
