import { useState } from "react";
import { updateDetection } from "../api/printer.js";
import Card from "../components/ui/Card.jsx";
import Columns from "../components/ui/Columns.jsx";
import Field from "../components/ui/Field.jsx";
import PageFrame from "../components/ui/PageFrame.jsx";
import StatusPill from "../components/ui/StatusPill.jsx";
import CameraCard from "../components/dashboard/CameraCard.jsx";

const CLASSES = ["blobs", "cracks", "over_extrusion", "spaghetti",
                 "stringing", "under_extrusion"];

const ROI_FIELDS = [
  { key: "x", label: "Left %" }, { key: "y", label: "Top %" },
  { key: "w", label: "Width %" }, { key: "h", label: "Height %" },
];
// Measured off a real A1 mini frame (1680x1080), not guessed: the bed occupies
// the lower-left of that wide fisheye view. Generous on height because the A1
// is a bed-slinger -- the bed sweeps toward and away from the camera as it
// prints, so the region has to cover its whole travel. Tune from the overlay.
const DEFAULT_ROI_PCT = ["0", "40", "65", "60"];

const roiToPct = (roi) =>
  roi ? roi.map((v) => String(Math.round(v * 100))) : DEFAULT_ROI_PCT;

export default function Detection({ printers, selected }) {
  const s = printers.find((p) => p.serial === selected) ?? null;
  const d = s?.detection ?? null;
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  // Held as strings while editing so a half-typed value doesn't fight the
  // input; converted and validated only on Apply.
  const [roiPct, setRoiPct] = useState(() => roiToPct(d?.roi));
  const [roiError, setRoiError] = useState(null);

  const save = async (patch) => {
    setBusy(true);
    setErr(null);
    try {
      await updateDetection(s.serial, patch);   // WS pushes the new state
    } catch (e) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  };

  // Validate here as well as server-side: the server clamps a bad ROI to "whole
  // frame", which would look like the setting silently not taking.
  const saveRoi = () => {
    const nums = roiPct.map(Number);
    if (nums.some((n) => !Number.isFinite(n))) {
      return setRoiError("All four values must be numbers.");
    }
    const [x, y, w, h] = nums.map((n) => n / 100);
    if (w <= 0 || h <= 0) return setRoiError("Width and height must be above 0.");
    if (x < 0 || y < 0 || x + w > 1 || y + h > 1) {
      return setRoiError("The region must fit inside the frame.");
    }
    setRoiError(null);
    save({ roi: [x, y, w, h] });
  };

  if (!s) {
    return <PageFrame><div className="empty">No printer selected.</div></PageFrame>;
  }
  if (!d) {
    return (
      <PageFrame>
        <div className="empty">
          Detection runs on the capture printer. Mark “{s.name}” as the capture
          printer on the Overview page to configure it here.
        </div>
      </PageFrame>
    );
  }

  const toggleClass = (cls) => {
    const set = new Set(d.armed_classes ?? []);
    set.has(cls) ? set.delete(cls) : set.add(cls);
    save({ armed_classes: [...set] });
  };

  return (
    <PageFrame>
      <Columns template="2fr 3fr">
        <Card title="Detector">
          <div className="detect-settings">
            <label className="detect-row">
              <span>Enable detection</span>
              <input type="checkbox" checked={!!d.detect_enabled} disabled={busy}
                     onChange={(e) => save({ detect_enabled: e.target.checked })} />
            </label>

            <div className="detect-row">
              <span>Camera source</span>
              <span className="detect-seg">
                {["a1", "webcam"].map((src) => (
                  <button key={src} type="button" disabled={busy}
                          className={`detect-seg__btn${d.camera_source === src ? " is-on" : ""}`}
                          onClick={() => save({ camera_source: src })}>
                    {src === "a1" ? "A1 built-in" : "USB webcam"}
                  </button>
                ))}
              </span>
            </div>

            {d.camera_source === "webcam" && (
              <Field label="Webcam index" type="number" min="0"
                     defaultValue={d.camera_index}
                     onBlur={(e) => save({ camera_index: Number(e.target.value) })}
                     help="USB camera index (0, 1, …)" />
            )}

            <Field label={`Confidence threshold (${Number(d.conf).toFixed(2)})`}
                   type="range" min="0.05" max="0.9" step="0.05"
                   defaultValue={d.conf}
                   onMouseUp={(e) => save({ conf: Number(e.target.value) })}
                   onTouchEnd={(e) => save({ conf: Number(e.target.value) })} />

            <div className="detect-label">Detection region</div>
            <p className="detect-help">
              Run detection on the bed only. Everything outside the box is
              ignored — on the A1's wide, low view the rest of the frame is the
              room, and the model will happily find "failures" in furniture.
              Values are percentages of the frame; the region is outlined in the
              live view so you can tune it by eye.
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
              <button type="button" className="detect-seg__btn" disabled={busy}
                      onClick={saveRoi}>Apply region</button>
              <button type="button" className="detect-seg__btn" disabled={busy || !d.roi}
                      onClick={() => { setRoiPct(DEFAULT_ROI_PCT); save({ roi: null }); }}>
                Use whole frame
              </button>
              {roiError && <span className="detect-roi__error">{roiError}</span>}
            </div>

            <div className="detect-label">Arm these classes</div>
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

            <div className="detect-health">
              <StatusPill status={d.running ? "ok" : "warn"}>
                {d.running ? `running · ${d.fps ?? "?"} fps` : "not running"}
              </StatusPill>
              {d.error && <div className="add-form__error">{d.error}</div>}
            </div>
            {err && <div className="add-form__error">{err}</div>}
          </div>
        </Card>

        <CameraCard serial={s.serial} live={!!d.running} />
      </Columns>
    </PageFrame>
  );
}
