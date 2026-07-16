import { useEffect, useState } from "react";
import { fetchLatestFrame } from "../../api/printer.js";
import Card from "../ui/Card.jsx";

const POLL_MS = 2000;

export default function CameraCard() {
  const [frame, setFrame] = useState(null);

  useEffect(() => {
    let alive = true;
    let currentUrl = null;

    const tick = async () => {
      const f = await fetchLatestFrame();
      if (!alive) {
        if (f) URL.revokeObjectURL(f.url);
        return;
      }
      if (currentUrl) URL.revokeObjectURL(currentUrl);
      currentUrl = f ? f.url : null;
      setFrame(f); // null -> placeholder (no active run)
    };

    tick();
    const id = setInterval(tick, POLL_MS);
    return () => {
      alive = false;
      clearInterval(id);
      if (currentUrl) URL.revokeObjectURL(currentUrl);
    };
  }, []);

  return (
    <Card title="Camera">
      {frame ? (
        <>
          <img className="camera-frame" src={frame.url}
               alt={`Print at layer ${frame.layer}`} />
          <div className="camera-caption">
            Layer {frame.layer} — {frame.run}
          </div>
        </>
      ) : (
        <div className="camera-placeholder">
          No active capture run — start capture.py
        </div>
      )}
    </Card>
  );
}
