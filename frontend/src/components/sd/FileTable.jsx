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

export default function FileTable({ entries, onOpen }) {
  if (entries.length === 0) {
    return <div className="empty">This folder is empty.</div>;
  }
  return (
    <table className="file-table">
      <thead>
        <tr><th>Name</th><th>Size</th><th>Modified</th></tr>
      </thead>
      <tbody>
        {entries.map((e, i) => (
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
            <td className="file-table__muted">{when(e.mtime)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
