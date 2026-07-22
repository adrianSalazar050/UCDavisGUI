import { useEffect, useRef, useState } from "react";

import { HANDLES, applyHandleDrag, clampRoi } from "./roiGeometry.js";

// Draggable detection-region overlay on the live camera view.
//
// WHY THIS EXISTS: the ROI used to be four % text fields whose only feedback
// was a rectangle burned into the JPEG by detect.py -- which cannot change
// until the config saves, the supervisor respawns the detector, and a new
// frame arrives, 5-10s later. So you were aiming blind at the one setting
// that silently disables detection when it is wrong (master.md 4.1).
//
// TWO BOXES ON PURPOSE. The burned-in outline stays, and this one sits on
// top of it in a different colour:
//   burned-in (detect.py) = what the detector is using RIGHT NOW
//   this one (browser)    = what you are about to apply
// They converge when you hit Apply, which turns a confusing artifact into a
// before/after.
//
// Everything is in FRACTIONS positioned with CSS %, so no pixel maths and it
// survives any display size. The only pixel conversion is the drag delta,
// which divides by the rendered element's size.
export default function RoiEditor({ src, roi, onChange, disabled }) {
  const boxRef = useRef(null);
  // The box as it was when this drag started, plus which handle is held.
  // Deltas are always measured from here rather than accumulated per-move --
  // see applyHandleDrag's comment for why accumulation drifts.
  const drag = useRef(null);
  const [dragging, setDragging] = useState(false);

  const start = (handle) => (e) => {
    if (disabled) return;
    e.preventDefault();
    e.stopPropagation();
    const rect = boxRef.current?.getBoundingClientRect();
    if (!rect?.width || !rect?.height) return;
    drag.current = { handle, rect, startX: e.clientX, startY: e.clientY,
                     startRoi: roi };
    setDragging(true);
    // Capture on the element so the drag survives the pointer leaving the
    // image -- without this, dragging an edge off-frame silently stops.
    e.currentTarget.setPointerCapture?.(e.pointerId);
  };

  useEffect(() => {
    if (!dragging) return undefined;
    const move = (e) => {
      const d = drag.current;
      if (!d) return;
      const dx = (e.clientX - d.startX) / d.rect.width;
      const dy = (e.clientY - d.startY) / d.rect.height;
      onChange(applyHandleDrag(d.startRoi, d.handle, dx, dy));
    };
    const end = () => { drag.current = null; setDragging(false); };
    // On window, not the element: a pointerup outside the browser viewport
    // would otherwise never arrive and the box would stay stuck to the mouse.
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", end);
    window.addEventListener("pointercancel", end);
    return () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", end);
      window.removeEventListener("pointercancel", end);
    };
  }, [dragging, onChange]);

  const [x, y, w, h] = clampRoi(roi);
  const pct = (n) => `${n * 100}%`;

  return (
    <div className="roi" ref={boxRef}>
      {src
        ? <img className="roi__img" src={src} alt="Detector view" draggable={false} />
        : <div className="roi__placeholder">Waiting for a frame…</div>}

      {/* Dimming everything outside the box makes the crop legible at a
          glance -- four panels rather than a box-shadow so it degrades
          predictably at any aspect ratio. */}
      <div className="roi__veil" style={{ inset: `0 0 ${pct(1 - y)} 0` }} />
      <div className="roi__veil" style={{ inset: `${pct(y + h)} 0 0 0` }} />
      <div className="roi__veil"
           style={{ inset: `${pct(y)} ${pct(1 - x)} ${pct(1 - y - h)} 0` }} />
      <div className="roi__veil"
           style={{ inset: `${pct(y)} 0 ${pct(1 - y - h)} ${pct(x + w)}` }} />

      <div className={`roi__box${dragging ? " is-dragging" : ""}`}
           style={{ left: pct(x), top: pct(y), width: pct(w), height: pct(h) }}
           onPointerDown={start("move")}>
        {!disabled && HANDLES.map((handle) => (
          <span key={handle}
                className={`roi__handle roi__handle--${handle}`}
                onPointerDown={start(handle)} />
        ))}
      </div>
    </div>
  );
}
