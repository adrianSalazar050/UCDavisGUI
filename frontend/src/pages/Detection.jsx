import { useState } from "react";
import { updateDetection } from "../api/printer.js";
import Card from "../components/ui/Card.jsx";
import Button from "../components/ui/Button.jsx";
import Columns from "../components/ui/Columns.jsx";
import Field from "../components/ui/Field.jsx";
import PageFrame from "../components/ui/PageFrame.jsx";
import StatusPill from "../components/ui/StatusPill.jsx";
import CameraCard from "../components/dashboard/CameraCard.jsx";

const CLASSES = ["blobs", "cracks", "over_extrusion", "spaghetti",
                 "stringing", "under_extrusion"];

export default function Detection({ printers, selected }) {
  const s = printers.find((p) => p.serial === selected) ?? null;
  const d = s?.detection ?? null;
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);

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
