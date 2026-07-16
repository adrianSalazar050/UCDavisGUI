// Fetch wrappers for the dashboard backend. WebSocket lives in usePrinter.

export async function fetchStatus() {
  const res = await fetch("/api/status");
  if (!res.ok) throw new Error(`status ${res.status}`);
  return res.json();
}

// Returns { url, layer, run } (url is an object URL the caller must revoke)
// or null when there is no active run (HTTP 404) or on network error.
export async function fetchLatestFrame() {
  let res;
  try {
    res = await fetch(`/api/frame/latest?t=${Date.now()}`, { cache: "no-store" });
  } catch {
    return null;
  }
  if (!res.ok) return null;
  const blob = await res.blob();
  return {
    url: URL.createObjectURL(blob),
    layer: res.headers.get("X-Frame-Layer"),
    run: res.headers.get("X-Frame-Run"),
  };
}
