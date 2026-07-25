import { useEffect, useState } from "react";

const MAX_BACKOFF_MS = 10000;

// Live list of every registered printer over /ws with auto-reconnect.
// Returns { printers, wsUp }: printers is the last received list (empty until
// the first message), wsUp is whether the socket is currently open.
export function usePrinters() {
  const [printers, setPrinters] = useState([]);
  const [robot, setRobot] = useState(null);
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
      ws.onmessage = (e) => {
        // A malformed frame must not take down the socket or the app — log
        // and keep the last-known-good printer list instead of throwing out
        // of the handler (which would otherwise just be an uncaught
        // exception; it would NOT close the connection or crash React, but
        // dropping the update silently would be worse than noting it).
        try {
          const payload = JSON.parse(e.data);
          setPrinters(payload.printers ?? []);
          if ("robot" in payload) setRobot(payload.robot);
        } catch (err) {
          console.error("usePrinters: malformed WS message", err);
        }
      };
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

  return { printers, robot, wsUp };
}
