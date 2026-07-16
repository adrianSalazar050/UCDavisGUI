import { useEffect, useState } from "react";

const MAX_BACKOFF_MS = 10000;

// Live printer summary over /ws with auto-reconnect.
// Returns { summary, wsUp }: summary is the last received payload (or null),
// wsUp is whether the socket is currently open.
export function usePrinter() {
  const [summary, setSummary] = useState(null);
  const [wsUp, setWsUp] = useState(false);

  useEffect(() => {
    let ws = null;
    let timer = null;
    let alive = true;
    let delay = 1000;

    const connect = () => {
      const proto = window.location.protocol === "https:" ? "wss" : "ws";
      ws = new WebSocket(`${proto}://${window.location.host}/ws`);
      ws.onopen = () => {
        setWsUp(true);
        delay = 1000;
      };
      ws.onmessage = (e) => setSummary(JSON.parse(e.data));
      ws.onclose = () => {
        // A torn-down effect's socket (StrictMode double-mount) must not
        // touch state owned by the effect that replaced it. Dev consoles
        // log "closed before the connection is established" here — expected.
        if (!alive) return;
        setWsUp(false);
        timer = setTimeout(connect, delay);
        delay = Math.min(delay * 2, MAX_BACKOFF_MS);
      };
    };

    connect();
    return () => {
      alive = false;
      clearTimeout(timer);
      if (ws) ws.close();
    };
  }, []);

  return { summary, wsUp };
}
