import { useState } from "react";
import { updateDetection } from "../api/printer.js";
import Button from "../components/ui/Button.jsx";
import Card from "../components/ui/Card.jsx";
import Columns from "../components/ui/Columns.jsx";
import EmptyState from "../components/ui/EmptyState.jsx";
import Field from "../components/ui/Field.jsx";
import PageFrame from "../components/ui/PageFrame.jsx";
import Stack from "../components/ui/Stack.jsx";
import StatusPill from "../components/ui/StatusPill.jsx";
import RoiEditor from "../components/detection/RoiEditor.jsx";
import { pctToRoi, roiToPct } from "../components/detection/roiGeometry.js";
import useCameraFrame from "../hooks/useCameraFrame.js";

const CLASSES = ["blobs", "cracks", "over_extrusion", "spaghetti",
                 "stringing", "under_extrusion"];

const ROI_FIELDS = [
  { key: "x", label: "Left %" }, { key: "y", label: "Top %" },
  { key: "w", label: "Width %" }, { key: "h", label: "Height %" },
];
// THE ROI IS PER PRINTER MODEL. Do not copy one between machines -- measured
// 2026-07-21 the A1 mini and the A1 are close to inverted:
//
//   A1 mini (1680x1080): bed in the TOP half     -> 0,0,100,50
//   A1      (1536x1080): bed in the BOTTOM ~60%  -> 8,32,88,68
//
// Applying the mini's box to an A1 crops the bed out of frame ENTIRELY and
// leaves the detector looking at the room. This default is the A1's, because
// that is the hardware in use; on a mini, set it by hand on this page.
//
// The A1 numbers come from an IDLE frame and are provisional. The earlier mini
// default (0,40,65,60) was also measured idle and was completely wrong: while
// printing the bed rides high and that box held only the front panel -- no bed
// at all. An idle frame is not representative, because the bed parks somewhere
// quite different from where it prints. Always confirm from a frame mid-print.
//
// Generous on purpose -- a too-small ROI crops the failure out of view, which
// is a silent false negative and far worse than including some background.
const DEFAULT_ROI_PCT = ["8", "32", "88", "68"];


// Owns the detector controls for exactly ONE printer: busy, err, roiPct and
// roiError, every one of them seeded from THIS printer's detection object at
// mount. Detection below mounts it with key={s.serial} — see the note on that
// key for why the remount is load-bearing rather than tidy.
//
// `printer` and `printer.detection` are both guaranteed here: the three guards
// in Detection run before this ever renders, and props can only change when the
// parent re-renders (which re-runs those guards first).
function DetectionPanel({ printer }) {
  const d = printer.detection;
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  // Held as strings while editing so a half-typed value doesn't fight the
  // input; converted and validated only on Apply.
  const [roiPct, setRoiPct] = useState(() => roiToPct(d?.roi, DEFAULT_ROI_PCT));
  const [roiError, setRoiError] = useState(null);
  const frame = useCameraFrame(printer.serial, !!d.running);

  // The draggable box and the four % inputs are two views of ONE value, kept
  // in roiPct (strings, so a half-typed "1" doesn't fight the input). The
  // editor writes fractions; the fields write strings; both land here.
  const roiDraft = pctToRoi(roiPct);
  const setRoiFromEditor = (next) =>
    setRoiPct(roiToPct(next, roiPct));

  const save = async (patch) => {
    setBusy(true);
    setErr(null);
    try {
      await updateDetection(printer.serial, patch);   // WS pushes the new state
    } catch (e) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  };

  // Validate here as well as server-side: the server clamps a bad ROI to "whole
  // frame", which would look like the setting silently not taking.
  const saveRoi = () => {
    const nums = pctToRoi(roiPct);
    if (nums === null) {
      return setRoiError("All four values must be numbers.");
    }
    const [x, y, w, h] = nums;
    if (w <= 0 || h <= 0) return setRoiError("Width and height must be above 0.");
    if (x < 0 || y < 0 || x + w > 1 || y + h > 1) {
      return setRoiError("The region must fit inside the frame.");
    }
    setRoiError(null);
    save({ roi: [x, y, w, h] });
  };

  const toggleClass = (cls) => {
    const set = new Set(d.armed_classes ?? []);
    set.has(cls) ? set.delete(cls) : set.add(cls);
    save({ armed_classes: [...set] });
  };

  return (
    <>
      {/* A failed write, not the detector's own error: any of the five groups
          below can produce it, so it sits above all of them rather than under
          whichever one happened to send the patch. */}
      {err && <p className="error">{err}</p>}
      <Columns template="2fr 3fr">
        <Stack gap={5}>
          {/* Health first. Every other control here is a setting whose effect
              you can only judge from a running detector, so "is it running,
              and how fast" is the question that comes before all of them --
              it used to be the last thing on the page. */}
          <Card title="Detector health">
            <div className="detect-health">
              <StatusPill status={d.running ? "ok" : "warn"}>
                {d.running ? `running · ${d.fps ?? "?"} fps` : "not running"}
              </StatusPill>
              {d.error && <div className="add-form__error">{d.error}</div>}
            </div>
          </Card>

          <Card title="Camera">
            <div className="detect-settings">
              <label className="detect-row">
                <span>Enable detection</span>
                <input type="checkbox" checked={!!d.detect_enabled} disabled={busy}
                       onChange={(e) => save({ detect_enabled: e.target.checked })} />
              </label>

              <div className="detect-row">
                <span>Camera source</span>
                <span className="detect-seg">
                  {/* The stored value is "a1" for historical reasons -- it is
                      the printer's own built-in camera, the same TCP-6000
                      stream the P1 and X1 series speak too, not something
                      only an A1 has. Labelling it "A1" made operators of any
                      other model think the option did not apply to them, so
                      the label says what it does and the value stays put
                      (store.py validates against CAMERA_SOURCES, and
                      printers.json already holds "a1" on disk). */}
                  {["a1", "webcam"].map((src) => (
                    <button key={src} type="button" disabled={busy}
                            className={`detect-seg__btn${d.camera_source === src ? " is-on" : ""}`}
                            onClick={() => save({ camera_source: src })}>
                      {src === "a1" ? "Printer’s built-in" : "USB webcam"}
                    </button>
                  ))}
                </span>
              </div>

              {d.camera_source === "webcam" && (
                <Field label="Webcam index" type="number" min="0"
                       defaultValue={d.camera_index}
                       onBlur={(e) => save({ camera_index: Number(e.target.value) })}
                       help="USB camera index (0, 1, …). Saves when you click away." />
              )}
            </div>
          </Card>

          {/* The slider is uncontrolled and saves on release, so the number in
              its label only catches up once the server pushes the new state
              back. Saying so in the help line is what stops that reading as a
              dropped setting. */}
          <Card title="Sensitivity">
            <Field label={`Confidence threshold (${Number(d.conf).toFixed(2)})`}
                   type="range" min="0.05" max="0.9" step="0.05"
                   defaultValue={d.conf}
                   onMouseUp={(e) => save({ conf: Number(e.target.value) })}
                   onTouchEnd={(e) => save({ conf: Number(e.target.value) })}
                   help={"Saves when you let go of the slider. Lower catches "
                         + "fainter failures and reports more false alarms."} />
          </Card>

          <Card title="Detection region">
            <div className="detect-settings">
              <p className="detect-help">
                Run detection on the bed only. Everything outside the box is
                ignored — on the A1's wide, low view the rest of the frame is the
                room, and the model will happily find "failures" in furniture.
                Drag the box or its handles on the live frame — it moves as you
                drag, before anything is saved. The dimmer outline burned into
                the picture is what the detector is using right now; they match
                once you hit Apply.
              </p>
              <div className="detect-roi">
                {ROI_FIELDS.map(({ key, label }, i) => (
                  <label key={key} className="detect-roi__field">
                    <span>{label}</span>
                    <input type="number" min="0" max="100" step="1" disabled={busy}
                           value={roiPct[i]}
                           onChange={(e) => setRoiPct(
                             roiPct.map((v, j) => (j === i ? e.target.value : v)))} />
                  </label>
                ))}
              </div>
              <div className="detect-roi__actions">
                <Button variant="primary" size="sm" disabled={busy}
                        onClick={saveRoi}>
                  Apply region
                </Button>
                <Button size="sm" disabled={busy || !d.roi}
                        onClick={() => { setRoiPct(DEFAULT_ROI_PCT); save({ roi: null }); }}>
                  Use whole frame
                </Button>
                {roiError && <span className="detect-roi__error">{roiError}</span>}
              </div>
            </div>
          </Card>

          <Card title="Auto-stop classes">
            <div className="detect-settings">
              <div className="detect-classes">
                {CLASSES.map((cls) => (
                  <label key={cls} className="detect-class">
                    <input type="checkbox"
                           checked={(d.armed_classes ?? []).includes(cls)}
                           disabled={busy} onChange={() => toggleClass(cls)} />
                    {cls}
                  </label>
                ))}
              </div>
              <p className="muted">
                A ticked class stops the print if it stays above the threshold
                for ten seconds straight. The others are still detected and
                drawn on the frame — they just never stop anything. Auto-stop
                also has to be armed, which is on the Dashboard.
              </p>
            </div>
          </Card>
        </Stack>

        <Card title="Live frame">
          <RoiEditor src={frame?.url ?? null}
                     roi={roiDraft ?? [0, 0, 1, 1]}
                     onChange={setRoiFromEditor}
                     disabled={busy || roiDraft === null} />
          <div className="camera-caption">
            {d.running ? "Live detection feed" : "Detector not running"}
          </div>
        </Card>
      </Columns>
    </>
  );
}


export default function Detection({ printers, selected, onNavigate }) {
  const s = printers.find((p) => p.serial === selected) ?? null;
  const d = s?.detection ?? null;

  if (!s) {
    return (
      <PageFrame>
        <EmptyState
          title="No printer selected"
          action={printers.length === 0
            ? <Button variant="primary" onClick={() => onNavigate("printers")}>
                Add a printer
              </Button>
            : null}
        >
          {printers.length === 0
            ? "No printers are registered yet. Add one under Setup, then come "
              + "back to tune its detector."
            : "Pick a printer with the switcher in the header to tune its "
              + "detector."}
        </EmptyState>
      </PageFrame>
    );
  }
  // Two different reasons there is no detection object, and they need
  // different advice. detection_available === false means the SERVER has no
  // detector wired at all -- the desktop build ships without it -- so telling
  // someone to mark a capture printer would send them after something that
  // cannot help (reported 2026-07-23). Only when a detector exists is "you
  // haven't marked the capture printer yet" the real answer.
  if (s.detection_available === false) {
    return (
      <PageFrame>
        <EmptyState title="Failure detection isn’t available in this build">
          The desktop app ships without the YOLO detector to keep the download
          small — run the full server (<code>python -m server</code>) to use
          detection and the live camera view.
        </EmptyState>
      </PageFrame>
    );
  }
  if (!d) {
    return (
      <PageFrame>
        <EmptyState
          title="This printer isn’t the capture printer"
          action={
            <Button variant="primary" onClick={() => onNavigate("printers")}>
              Open Printers
            </Button>
          }
        >
          Detection runs on the one printer the camera is pointed at. Mark
          “{s.name}” as the capture printer on Printers, under Setup, and its
          detector settings appear here.
        </EmptyState>
      </PageFrame>
    );
  }

  return (
    <PageFrame>
      {/* The key is the whole safety mechanism, for the same reason spelled out
          at length on SdBrowser in SdFiles.jsx: remount for fresh state instead
          of chasing each field with a reset Effect. It matters MORE here. The
          topbar switcher changes the selected printer WITHOUT navigating, so
          this page re-renders rather than remounting, and a useState
          initialiser never runs a second time. Un-keyed, the panel would keep
          printer A's roiPct in the four % inputs and in the draggable box while
          `d` is already printer B's detection object, and "Apply region" would
          call updateDetection(B.serial, A's roi) — read the DEFAULT_ROI_PCT
          note above for what that costs: the A1 and A1 mini regions are near
          inverted, so the wrong box crops the bed out of frame entirely and
          leaves the detector watching the room. That is a silent false
          negative, the worst outcome in this system. The key also drops a
          stale `err` from a failed save to A, which would otherwise still be
          rendered above B's controls. */}
      <DetectionPanel key={s.serial} printer={s} />
    </PageFrame>
  );
}
