// Fetch wrappers for the dashboard backend. WebSocket lives in usePrinters.

// FastAPI puts HTTPException messages in {"detail": "..."}. For a 422 the
// detail is a list of validation objects, not a string — flatten it so a
// future caller never surfaces "[object Object]".
async function detail(res) {
  try {
    const { detail } = await res.json();
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) {
      return detail.map((d) => d.msg ?? JSON.stringify(d)).join("; ");
    }
    return `HTTP ${res.status}`;
  } catch {
    return `HTTP ${res.status}`;
  }
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

// Update detection config for a printer. Body may include any of:
// { camera_source, camera_index, conf, armed_classes, detect_enabled }.
export async function updateDetection(serial, body) {
  const res = await fetch(`/api/printers/${encodeURIComponent(serial)}/detection`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(await detail(res));
  return res.json();
}

// Arm or disarm the auto-stop (runtime-only). Returns the detection snapshot.
export async function armDetection(serial, armed) {
  const res = await fetch(
    `/api/printers/${encodeURIComponent(serial)}/detection/arm`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ armed }),
    });
  if (!res.ok) throw new Error(await detail(res));
  return res.json();
}

// Latest annotated detector frame as an object URL (caller must revoke), or
// null when there's no frame yet: HTTP 404 (detector still starting, or --mock
// which writes status but no JPEG) or a network error. Same shape as
// fetchLatestFrame so CameraCard treats both sources uniformly.
export async function fetchDetectionFrame(serial) {
  try {
    const res = await fetch(
      `/api/printers/${encodeURIComponent(serial)}/detection/frame?t=${Date.now()}`,
      { cache: "no-store" });
    if (!res.ok) return null;
    const blob = await res.blob();
    return { url: URL.createObjectURL(blob), live: true };
  } catch {
    return null;
  }
}
