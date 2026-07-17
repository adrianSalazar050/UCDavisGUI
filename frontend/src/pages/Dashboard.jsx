import CameraCard from "../components/dashboard/CameraCard.jsx";
import HmsCard from "../components/dashboard/HmsCard.jsx";
import PrintInfoCard from "../components/dashboard/PrintInfoCard.jsx";
import Columns from "../components/ui/Columns.jsx";
import PageFrame from "../components/ui/PageFrame.jsx";
import Stack from "../components/ui/Stack.jsx";
import StatTile from "../components/ui/StatTile.jsx";

const deg = (v) => (v == null ? "—" : `${Number(v).toFixed(0)}°`);

export default function Dashboard({ printers, selected }) {
  const s = printers.find((p) => p.serial === selected) ?? {};
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
        <CameraCard />
        <Stack gap={5}>
          <PrintInfoCard summary={s} />
          <HmsCard summary={s} />
        </Stack>
      </Columns>
    </PageFrame>
  );
}
