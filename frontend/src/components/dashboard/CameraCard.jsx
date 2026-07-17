import { useEffect, useState } from "react";
import { fetchLatestFrame, fetchDetectionFrame } from "../../api/printer.js";
import Card from "../ui/Card.jsx";

const POLL_MS = 2000;

// When `live` (a detector is running for `serial`), poll the annotated detection
// frame; otherwise fall back to the capture layer frame. Both return an object
// URL (or null), so a missing frame is a clean placeholder — never a broken
// <img> — and every URL is revoked exactly once.
export default function CameraCard({ serial = null, live = false }) {
  const [frame, setFrame] = useState(null);

  useEffect(() => {
    let alive = true;
    let currentUrl = null;

    const tick = async () => {
      const f = live && serial
        ? await fetchDetectionFrame(serial)
        : await fetchLatestFrame();
      if (!alive) {
        if (f) URL.revokeObjectURL(f.url);
        return;
      }
      if (currentUrl) URL.revokeObjectURL(currentUrl);
      currentUrl = f ? f.url : null;
      setFrame(f); // null -> placeholder
    };

    tick();
    const id = setInterval(tick, POLL_MS);
    return () => {
      alive = false;
      clearInterval(id);
      if (currentUrl) URL.revokeObjectURL(currentUrl);
    };
  }, [serial, live]);

  return (
    <Card title="Camera">
      {frame ? (
        <>
          <img className="camera-frame" src={frame.url}
               alt={frame.live ? "Live detection" : `Print at layer ${frame.layer}`} />
          <div className="camera-caption">
            {frame.live ? "Live detection feed"
              : frame.layer != null ? `Layer ${frame.layer} — ${frame.run}` : ""}
          </div>
        </>
      ) : (
        <div className="camera-placeholder">
          {live ? "Waiting for detector frames…" : "No active capture run — start capture.py"}
        </div>
      )}
    </Card>
  );
}
