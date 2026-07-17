// Fetch wrappers for the dashboard backend. WebSocket lives in usePrinters.

// FastAPI puts HTTPException messages in {"detail": "..."}.
async function detail(res) {
  try {
    const body = await res.json();
    return body.detail ?? `HTTP ${res.status}`;
  } catch {
    return `HTTP ${res.status}`;
  }
}

export async function fetchPrinters() {
  const res = await fetch("/api/printers");
  if (!res.ok) throw new Error(await detail(res));
  return (await res.json()).printers ?? [];
}

export async function addPrinter({ host, serial, access_code, name, capture }) {
  const res = await fetch("/api/printers", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ host, serial, access_code, name, capture }),
  });
  if (!res.ok) throw new Error(await detail(res));
  return res.json();
}

export async function removePrinter(serial) {
  const res = await fetch(`/api/printers/${encodeURIComponent(serial)}`,
                          { method: "DELETE" });
  if (!res.ok) throw new Error(await detail(res));
}

// { path, entries: [{ name, is_dir, size, mtime }] }
export async function fetchFiles(serial, path = "/") {
  const res = await fetch(
    `/api/printers/${encodeURIComponent(serial)}/files` +
    `?path=${encodeURIComponent(path)}`);
  if (!res.ok) throw new Error(await detail(res));
  return res.json();
}

// Returns { url, layer, run } (url is an object URL the caller must revoke)
// or null when there is no active run (HTTP 404) or on network error.
export async function fetchLatestFrame() {
  try {
    const res = await fetch(`/api/frame/latest?t=${Date.now()}`, { cache: "no-store" });
    if (!res.ok) return null;
    const blob = await res.blob();
    return {
      url: URL.createObjectURL(blob),
      layer: res.headers.get("X-Frame-Layer"),
      run: res.headers.get("X-Frame-Run"),
    };
  } catch {
    return null; // network error or body stream failure — same as "no frame"
  }
}
