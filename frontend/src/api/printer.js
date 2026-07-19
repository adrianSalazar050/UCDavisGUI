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

// Edit a registered printer. Blank access_code keeps the current one.
export async function updatePrinter(serial, { host, access_code, name, capture }) {
  const res = await fetch(`/api/printers/${encodeURIComponent(serial)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ host, access_code, name, capture }),
  });
  if (!res.ok) throw new Error(await detail(res));
  return res.json();
}

// { path, entries: [{ name, is_dir, size, mtime }] }
export async function fetchFiles(serial, path = "/") {
  const res = await fetch(
    `/api/printers/${encodeURIComponent(serial)}/files` +
    `?path=${encodeURIComponent(path)}`);
  if (!res.ok) throw new Error(await detail(res));
  return res.json();
}

// { jobs: [{id, sd_path, name, seconds, grams, source}],
//   totals: {seconds, grams, finish_epoch} }
export async function fetchQueue(serial) {
  const res = await fetch(`/api/printers/${encodeURIComponent(serial)}/queue`);
  if (!res.ok) throw new Error(await detail(res));
  return res.json();
}

// Fetches + parses one SD-card .gcode.3mf and appends it to the queue.
// Returns the added job. sd_path is the only client-supplied field -- the
// server derives id/name/seconds/grams/source from the fetched file.
export async function addQueueJob(serial, sd_path) {
  const res = await fetch(`/api/printers/${encodeURIComponent(serial)}/queue`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ sd_path }),
  });
  if (!res.ok) throw new Error(await detail(res));
  return res.json();
}

export async function removeQueueJob(serial, id) {
  const res = await fetch(
    `/api/printers/${encodeURIComponent(serial)}/queue/${encodeURIComponent(id)}`,
    { method: "DELETE" });
  if (!res.ok) throw new Error(await detail(res));
}

// Reorders the queue to match `ids` (unknown ids are dropped server-side).
// Returns the fresh { jobs, totals } envelope, same shape as fetchQueue.
export async function reorderQueue(serial, ids) {
  const res = await fetch(`/api/printers/${encodeURIComponent(serial)}/queue`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ids }),
  });
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

// Send the first queued job to the printer. Resolves to
// { started, job, detail?, jobs, totals } — note `started: false` is a normal
// 200, not an error: it means the command went out but the printer never
// reported a print starting, and the job is still queued.
export async function startQueueJob(serial, jobId) {
  const res = await fetch(
    `/api/printers/${encodeURIComponent(serial)}/queue/${encodeURIComponent(jobId)}/start`,
    { method: "POST" });
  if (!res.ok) throw new Error(await detail(res));
  return res.json();
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
