import useCameraFrame from "../../hooks/useCameraFrame.js";
import Card from "../ui/Card.jsx";

// Thin presentation over useCameraFrame -- the polling/revocation logic moved
// into that hook so the Detection page's ROI editor can render the same frame
// without a second poll. See the hook for the empty-poll and revoke rules.
export default function CameraCard({ serial = null, live = false }) {
  const frame = useCameraFrame(serial, live);

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
