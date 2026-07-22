import { useEffect, useState } from "react";

import { fetchDetectionFrame, fetchLatestFrame } from "../api/printer.js";

// Matches the detector's capture interval (detect.py --interval, 5 s): polling
// faster than the camera captures just re-fetches a frame we already have.
export const POLL_MS = 5000;

// When `live` (a detector is running for `serial`), poll the annotated
// detection frame; otherwise fall back to the capture-layer frame. Both return
// an object URL (or null), so a missing frame is a clean placeholder — never a
// broken <img> — and every URL is revoked exactly once.
//
// A poll that comes back empty (detector mid-restart, a write we raced, a blip)
// KEEPS the frame already on screen rather than blanking to the placeholder:
// the last picture stays up until a new one actually replaces it. The
// placeholder is therefore only ever seen before the first successful frame.
//
// Extracted from CameraCard so the ROI editor can show the same frame without
// running a second poll against the same file.
export default function useCameraFrame(serial = null, live = false) {
  const [frame, setFrame] = useState(null);

  useEffect(() => {
    let alive = true;
    let currentUrl = null;

    const tick = async () => {
      const f = live && serial
        ? await fetchDetectionFrame(serial)
        : await fetchLatestFrame();
      if (!alive || !f) {
        // Unmounted, or nothing new. Revoke only the URL we are discarding —
        // never `currentUrl`, which is still the <img>'s source.
        if (f) URL.revokeObjectURL(f.url);
        return;
      }
      if (currentUrl) URL.revokeObjectURL(currentUrl);
      currentUrl = f.url;
      setFrame(f);
    };

    // Switching printer/source: drop the previous printer's picture rather
    // than leaving a stale frame captioned as if it belonged to the new one.
    setFrame(null);
    tick();
    const id = setInterval(tick, POLL_MS);
    return () => {
      alive = false;
      clearInterval(id);
      if (currentUrl) URL.revokeObjectURL(currentUrl);
    };
  }, [serial, live]);

  return frame;
}
