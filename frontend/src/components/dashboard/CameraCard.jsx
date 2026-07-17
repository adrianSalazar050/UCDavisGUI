import { useEffect, useState } from "react";
import { fetchLatestFrame, detectionFrameUrl } from "../../api/printer.js";
import Card from "../ui/Card.jsx";

const POLL_MS = 2000;

// When `live` (a detector is running for `serial`), poll the annotated
// detection frame; otherwise fall back to the capture layer frame.
export default function CameraCard({ serial = null, live = false }) {
  const [frame, setFrame] = useState(null);

  useEffect(() => {
    let alive = true;
    let currentUrl = null;

    const tick = async () => {
      let f = null;
      if (live && serial) {
        // Detector frames are plain JPEG at a fixed URL; cache-bust each poll.
        f = { url: `${detectionFrameUrl(serial)}?t=${Date.now()}`, revoke: false };
      } else {
        f = await fetchLatestFrame();          // {url, layer, run} object-URL or null
      }
      if (!alive) {
        if (f?.revoke) URL.revokeObjectURL(f.url);
        return;
      }
      if (currentUrl && frame?.revoke) URL.revokeObjectURL(currentUrl);
      currentUrl = f ? f.url : null;
      setFrame(f);
    };

    tick();
    const id = setInterval(tick, POLL_MS);
    return () => {
      alive = false;
      clearInterval(id);
      if (currentUrl && frame?.revoke) URL.revokeObjectURL(currentUrl);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [serial, live]);

  return (
    <Card title="Camera">
      {frame ? (
        <>
          <img className="camera-frame" src={frame.url}
               alt={live ? "Live detection" : `Print at layer ${frame.layer}`} />
          <div className="camera-caption">
            {live ? "Live detection feed" : frame.layer != null
              ? `Layer ${frame.layer} — ${frame.run}` : ""}
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
