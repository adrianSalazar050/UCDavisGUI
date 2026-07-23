import AutoStopCard from "../components/dashboard/AutoStopCard.jsx";
import CameraCard from "../components/dashboard/CameraCard.jsx";
import HmsCard from "../components/dashboard/HmsCard.jsx";
import PrintInfoCard from "../components/dashboard/PrintInfoCard.jsx";
import Card from "../components/ui/Card.jsx";
import Columns from "../components/ui/Columns.jsx";
import PageFrame from "../components/ui/PageFrame.jsx";
import Stack from "../components/ui/Stack.jsx";
import StatTile from "../components/ui/StatTile.jsx";

const deg = (v) => (v == null ? "—" : `${Number(v).toFixed(0)}°`);

export default function Dashboard({ printers, selected }) {
  const s = printers.find((p) => p.serial === selected) ?? null;

  if (!s) {
    return (
      <PageFrame>
        <div className="empty">
          No printer selected — pick one on the Overview page.
        </div>
      </PageFrame>
    );
  }

  return (
    <PageFrame>
      <div className="tile-row">
        <StatTile label="State" value={s.gcode_state} />
        <StatTile label="Layer"
                  value={s.layer_num != null
                    ? `${s.layer_num} / ${s.total_layer_num ?? "?"}` : null} />
        <StatTile label="Progress"
                  value={s.mc_percent != null ? `${s.mc_percent}%` : null} />
        <StatTile label="Remaining"
                  value={s.mc_remaining_time != null
                    ? `${s.mc_remaining_time} min` : null} />
        <StatTile label="Nozzle" value={deg(s.nozzle_temper)}
                  sub={`target ${deg(s.nozzle_target_temper)}`} />
        <StatTile label="Bed" value={deg(s.bed_temper)}
                  sub={`target ${deg(s.bed_target_temper)}`} />
      </div>
      <Columns template="3fr 2fr">
        {/* There is one webcam. Showing its frames on a printer it isn't
            pointed at would be a lie, so only the capture printer gets it. */}
        {s.capture ? (
          <CameraCard serial={s.serial} live={!!s.detection?.running} />
        ) : (
          <Card title="Camera">
            <div className="camera-placeholder">
              {s.detection_available === false
                ? /* Same trap as the Detection page: with no detector wired
                     (the desktop build), marking a capture printer would not
                     produce a camera view, so don't advise it. */
                  "The live camera view isn’t available in this build."
                : "No camera on this printer — mark it as the capture printer " +
                  "on the Overview page if the webcam points at it."}
            </div>
          </Card>
        )}
        <Stack gap={5}>
          {s.detection && <AutoStopCard serial={s.serial} d={s.detection} />}
          <PrintInfoCard summary={s} />
          <HmsCard summary={s} />
        </Stack>
      </Columns>
    </PageFrame>
  );
}
