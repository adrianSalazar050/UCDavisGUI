import AutoStopCard from "../components/dashboard/AutoStopCard.jsx";
import CameraCard from "../components/dashboard/CameraCard.jsx";
import HmsCard from "../components/dashboard/HmsCard.jsx";
import PrintInfoCard from "../components/dashboard/PrintInfoCard.jsx";
import {
  printerPercent, printerProgress, printerTone,
} from "../components/printers/printerStatus.js";
import Button from "../components/ui/Button.jsx";
import Card from "../components/ui/Card.jsx";
import Columns from "../components/ui/Columns.jsx";
import EmptyState from "../components/ui/EmptyState.jsx";
import PageFrame from "../components/ui/PageFrame.jsx";
import ProgressBar from "../components/ui/ProgressBar.jsx";
import Stack from "../components/ui/Stack.jsx";
import StatTile from "../components/ui/StatTile.jsx";

const deg = (v) => (v == null ? "—" : `${Number(v).toFixed(0)}°`);

// The State tile's sub-label. printerTone() folds a dead link into the headline
// -- "Offline" beats a stale "RUNNING" -- but that headline says nothing about
// the temperatures and the bar beside it, which come off the same report. This
// line does: it is the difference between reading a live machine and reading
// its last known numbers. It also explains "Stale", which is otherwise jargon.
const FRESHNESS = {
  ok: "reporting live",
  stale: "numbers may be old",
  disconnected: "not reporting",
};

export default function Dashboard({ printers, selected, onNavigate }) {
  const s = printers.find((p) => p.serial === selected) ?? null;

  // Two different nothings, the same split History draws: an empty lab, which
  // the user can fix from here, and a lab with nothing pointed at -- fixed with
  // the topbar switcher, so that branch names the control rather than sending
  // anyone to another page for something the header already does.
  if (!s) {
    return (
      <PageFrame>
        {printers.length === 0 ? (
          <EmptyState
            title="No printers registered yet"
            action={<Button variant="primary"
                            onClick={() => onNavigate("printers")}>
                      Add a printer
                    </Button>}>
            This page follows one machine at a time. Register a printer — its
            address, serial and access code — and its state, temperatures and
            camera view appear here.
          </EmptyState>
        ) : (
          <EmptyState title="No printer selected">
            Choose a printer with the switcher at the top of the window. It
            swaps the machine this page follows without taking you off it.
          </EmptyState>
        )}
      </PageFrame>
    );
  }

  const state = printerTone(s);
  // printerProgress() is the same string the topbar switcher shows, so the two
  // can never disagree about how far along the print is. It returns null
  // exactly when the printer isn't reporting layers -- which is when the bar
  // should be labelled with what the machine IS doing instead.
  const progress = printerProgress(s);

  return (
    <PageFrame>
      {/* The headline of the landing page: how far along is the print, legible
          without reading anything else on the screen. printerPercent() is null
          when nothing is printing and ProgressBar draws null as an empty
          track, never as 0% -- "no print" and "0% done" are different
          answers, and an operator must not confuse them. */}
      <ProgressBar
        value={printerPercent(s)}
        label={progress ?? state.label}
        right={s.mc_remaining_time != null
          ? `${s.mc_remaining_time} min left` : null} />

      {/* Three tiles, not six: layer, percent and remaining time ARE the bar
          now, and nothing else in the summary earns a tile of its own -- the
          speed and the job name are one column right, in Print detail.
          .tile-row sizes itself from the tiles (auto-fit), so dropping three of
          them did not leave a hole where the other half of the row used to be. */}
      <div className="tile-row">
        <StatTile label="State" value={state.label}
                  sub={FRESHNESS[s.connection]} />
        <StatTile label="Nozzle" value={deg(s.nozzle_temper)}
                  sub={`target ${deg(s.nozzle_target_temper)}`} />
        <StatTile label="Bed" value={deg(s.bed_temper)}
                  sub={`target ${deg(s.bed_target_temper)}`} />
      </div>

      <Columns template="3fr 2fr">
        {/* Only a printer whose camera is actually switched on gets a view.
            Any number of printers may have one now -- each has its own
            built-in camera and its own detector writing its own frames -- but
            showing frames under a printer nobody enabled would still be a lie,
            so the gate stays. */}
        {s.capture ? (
          <CameraCard serial={s.serial} live={!!s.detection?.running} />
        ) : (
          <Card title="Camera">
            {s.detection_available === false ? (
              /* Same trap as the Detection page: with no detector wired
                 (the desktop build), marking a capture printer would not
                 produce a camera view, so don't advise it. */
              <EmptyState title="The live camera view isn’t available in this build">
                The desktop app ships without the detector that serves frames,
                so no printer can show one — run the full server
                (<code>python -m server</code>) for the camera view.
              </EmptyState>
            ) : (
              <EmptyState
                title="No camera on this printer"
                action={<Button onClick={() => onNavigate("printers")}>
                          Open Printers
                        </Button>}>
                “{s.name}” has its own built-in camera — it just isn’t switched
                on here yet. Edit it on Printers, under Setup, and tick the
                camera box. You can do that for as many printers as you want to
                watch at once.
              </EmptyState>
            )}
          </Card>
        )}
        <Stack gap={5}>
          {/* Ordered by how fast it changes: Auto-stop can act on the print
              within seconds, HMS is what the machine is complaining about
              right now, and the file and speed only change between jobs. */}
          {s.detection && <AutoStopCard serial={s.serial} d={s.detection} />}
          <HmsCard summary={s} />
          <PrintInfoCard summary={s} />
        </Stack>
      </Columns>
    </PageFrame>
  );
}
