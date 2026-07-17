# Detection Frontend (Phase 1b) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Make the (already-built, hardware-verified) detection + auto-stop backend usable in the browser: a Dashboard **Auto-stop card** (armed state, Arm toggle, live detection, countdown) and a new **Detection page** (camera source selector, threshold, per-class arming, enable toggle, detector health + live preview).

**Architecture:** Pure React additions matching the existing component system (`Card`/`Button`/`Field`/`StatusPill`, class-prefixed CSS in `styles.css`). The `detection` object already rides the WebSocket `printers` list (`usePrinters()`), so the UI reads live state directly and only adds write wrappers (`PUT`/arm) + the frame URL. Every page receives `{ printers, selected, onSelect }`.

**Tech Stack:** React 18 + Vite (existing). **No JS test runner exists in this project**, so tasks verify via `npm run build` (compiles clean) and a real browser check under `python -m server --mock` — not unit tests. Build from `frontend/`.

**Spec:** `docs/superpowers/specs/2026-07-17-failure-detection-autostop-queue-design.md` (Detection UI shape = mockup B; camera source selector added in the revision).

**The `detection` object** (on each capture printer's summary; `null` otherwise): `{running, fps, camera_source, camera_index, conf, detect_enabled, armed, armed_classes, detections:[{cls,conf,box}], stopped_by_monitor, seconds_to_stop, error}`.

**Design rules:** match existing files exactly — functional components, the `ui-*` primitives, `2-space` indent, no new deps. Read `frontend/src/components/dashboard/PrintInfoCard.jsx`, `frontend/src/pages/Dashboard.jsx`, `frontend/src/api/printer.js`, and `frontend/src/styles.css` before writing. Detection runs only on the **capture printer**, so `s.detection` is non-null only there; a non-capture selection shows a "mark it capture on Overview" hint.

---

### Task 1: API write wrappers

**Files:**
- Modify: `frontend/src/api/printer.js`

- [ ] **Step 1: Implement** — append to `frontend/src/api/printer.js` (match the existing `addPrinter`/`detail` style):

```javascript
// Update detection config for a printer. Body may include any of:
// { camera_source, camera_index, conf, armed_classes, detect_enabled }.
export async function updateDetection(serial, body) {
  const res = await fetch(`/api/printers/${encodeURIComponent(serial)}/detection`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(await detail(res));
  return res.json();
}

// Arm or disarm the auto-stop (runtime-only). Returns the detection snapshot.
export async function armDetection(serial, armed) {
  const res = await fetch(
    `/api/printers/${encodeURIComponent(serial)}/detection/arm`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ armed }),
    });
  if (!res.ok) throw new Error(await detail(res));
  return res.json();
}

// URL for the detector's latest annotated frame. Cache-busted by the caller.
export function detectionFrameUrl(serial) {
  return `/api/printers/${encodeURIComponent(serial)}/detection/frame`;
}
```

- [ ] **Step 2: Verify build**

Run: `cd frontend && npm run build`
Expected: builds with no errors.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/api/printer.js
git commit -m "feat(frontend): detection API wrappers (updateDetection/armDetection/frameUrl)"
```

---

### Task 2: `AutoStopCard` (Dashboard)

**Files:**
- Create: `frontend/src/components/dashboard/AutoStopCard.jsx`
- Modify: `frontend/src/styles.css`

- [ ] **Step 1: Implement** — create `frontend/src/components/dashboard/AutoStopCard.jsx`:

```jsx
import { useState } from "react";
import { armDetection } from "../../api/printer.js";
import Button from "../ui/Button.jsx";
import Card from "../ui/Card.jsx";
import StatusPill from "../ui/StatusPill.jsx";

// The compact Dashboard card: armed state, the Arm toggle, the current top
// detection, a countdown while a fault is building, and the stopped-by-monitor
// latch. `d` is the printer's live `detection` object (never null here).
export default function AutoStopCard({ serial, d }) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);

  const toggle = async () => {
    setBusy(true);
    setErr(null);
    try {
      await armDetection(serial, !d.armed);   // WS pushes the new armed state
    } catch (e) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  };

  const top = (d.detections ?? [])[0];
  const counting = d.seconds_to_stop != null;

  return (
    <Card title="Auto-stop">
      <div className="autostop">
        <div className="autostop__row">
          <StatusPill status={d.armed ? "ok" : "warn"}>
            {d.armed ? "Armed" : "Disarmed"}
          </StatusPill>
          <Button size="sm" variant={d.armed ? "secondary" : "primary"}
                  busy={busy} onClick={toggle}>
            {d.armed ? "Disarm" : "Arm"}
          </Button>
        </div>

        {d.stopped_by_monitor && (
          <div className="autostop__stopped">■ Stopped by monitor</div>
        )}

        {counting && (
          <div className="autostop__count">
            ⚠ {top?.cls ?? "fault"} — stopping in {Math.ceil(d.seconds_to_stop)}s
          </div>
        )}

        <div className="autostop__now">
          {top
            ? `Detecting: ${top.cls} ${Number(top.conf).toFixed(2)}`
            : d.running ? "No failures detected" : "Detector not running"}
        </div>

        {err && <div className="add-form__error">{err}</div>}
      </div>
    </Card>
  );
}
```

- [ ] **Step 2: Styles** — append to `frontend/src/styles.css` (follow the file's existing spacing-var / color conventions; read the top of the file first):

```css
/* auto-stop card */
.autostop { display: flex; flex-direction: column; gap: 0.6rem; }
.autostop__row { display: flex; align-items: center; justify-content: space-between; }
.autostop__stopped { color: var(--danger, #dc2626); font-weight: 600; font-size: 0.85rem; }
.autostop__count { color: var(--warn, #d97706); font-weight: 600; font-size: 0.85rem; }
.autostop__now { color: var(--text-muted, #6b7280); font-size: 0.85rem; }
```
(If those CSS vars don't exist in this file, use the literal fallbacks shown or the nearest existing var — grep `styles.css` for `--` to see the palette.)

- [ ] **Step 3: Verify build** — `cd frontend && npm run build` (no errors).

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/dashboard/AutoStopCard.jsx frontend/src/styles.css
git commit -m "feat(frontend): Dashboard AutoStopCard (arm toggle, countdown, latch)"
```

---

### Task 3: Dashboard integration + CameraCard detection frame

**Files:**
- Modify: `frontend/src/pages/Dashboard.jsx`, `frontend/src/components/dashboard/CameraCard.jsx`

- [ ] **Step 1: CameraCard prefers the detector frame** — replace `frontend/src/components/dashboard/CameraCard.jsx` so it can show the detector's annotated frame when a detector is running, else fall back to the capture layer frame (today's behaviour). It takes optional `serial`/`live` props:

```jsx
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
```

> Note: the detection-frame branch uses a plain `<img src>` URL (the endpoint streams JPEG with `Cache-Control: no-store`), so there's no object-URL to revoke — the `revoke` flag distinguishes the two. Keep `fetchLatestFrame`'s object-URL path intact for the capture fallback.

- [ ] **Step 2: Dashboard renders it** — in `frontend/src/pages/Dashboard.jsx`, replace the camera/right-column block so the capture printer shows the live detector feed + the `AutoStopCard`. Import `AutoStopCard`, then in the render (where `s.capture` is checked today):

```jsx
      <Columns template="3fr 2fr">
        {s.capture ? (
          <CameraCard serial={s.serial} live={!!s.detection?.running} />
        ) : (
          <Card title="Camera">
            <div className="camera-placeholder">
              No camera on this printer — mark it as the capture printer on the
              Overview page if the webcam points at it.
            </div>
          </Card>
        )}
        <Stack gap={5}>
          {s.detection && <AutoStopCard serial={s.serial} d={s.detection} />}
          <PrintInfoCard summary={s} />
          <HmsCard summary={s} />
        </Stack>
      </Columns>
```
(Add `import AutoStopCard from "../components/dashboard/AutoStopCard.jsx";` at the top. `Card` is already imported.)

- [ ] **Step 3: Verify build** — `cd frontend && npm run build` (no errors).

- [ ] **Step 4: Commit**

```bash
git add frontend/src/pages/Dashboard.jsx frontend/src/components/dashboard/CameraCard.jsx
git commit -m "feat(frontend): Dashboard shows live detection feed + AutoStopCard"
```

---

### Task 4: Detection page + nav

**Files:**
- Create: `frontend/src/pages/Detection.jsx`
- Modify: `frontend/src/app/pageRegistry.jsx`, `frontend/src/styles.css`

- [ ] **Step 1: Implement the page** — create `frontend/src/pages/Detection.jsx`. It reads the selected printer's live `detection` object and writes config via `updateDetection`. Non-capture selection shows a hint.

```jsx
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
```

- [ ] **Step 2: Register the page** — in `frontend/src/app/pageRegistry.jsx`, import and add it after `dashboard`:

```jsx
import Detection from "../pages/Detection.jsx";
```
```jsx
  detection: { title: "Detection", group: "Monitor", component: Detection },
```

- [ ] **Step 3: Styles** — append to `frontend/src/styles.css`:

```css
/* detection page */
.detect-settings { display: flex; flex-direction: column; gap: 0.9rem; }
.detect-row { display: flex; align-items: center; justify-content: space-between; gap: 1rem; }
.detect-label { font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.04em;
  color: var(--text-muted, #6b7280); }
.detect-classes { display: grid; grid-template-columns: 1fr 1fr; gap: 0.35rem 1rem; }
.detect-class { display: flex; align-items: center; gap: 0.4rem; font-size: 0.9rem; }
.detect-seg { display: inline-flex; border: 1px solid var(--border, #d1d5db); border-radius: 6px; overflow: hidden; }
.detect-seg__btn { border: none; background: transparent; padding: 0.3rem 0.7rem; font-size: 0.8rem; cursor: pointer; }
.detect-seg__btn.is-on { background: var(--accent, #4f46e5); color: #fff; }
.detect-health { display: flex; align-items: center; gap: 0.6rem; }
```

- [ ] **Step 4: Verify build** — `cd frontend && npm run build` (no errors).

- [ ] **Step 5: Commit**

```bash
git add frontend/src/pages/Detection.jsx frontend/src/app/pageRegistry.jsx frontend/src/styles.css
git commit -m "feat(frontend): Detection page (source, threshold, class arming, health, preview)"
```

---

### Task 5: Visual verification (operator-run, `--mock` then real printer)

**Files:** none (verification only)

> Run by the operator. The mock path needs no hardware; the real path uses the connected printer.

- [ ] **Step 1: Build + run mock** — `cd frontend && npm run build`, then from the repo root `python -m server --mock`. Open the app.
- [ ] **Step 2: Dashboard** — select the capture mock printer (`mock-bench`): the **Auto-stop** card shows Disarmed + an Arm button; the camera shows the live feed once detection is enabled. Click **Arm**; within ~11 s the mock's synthetic spaghetti trips it → the card shows the countdown then "Stopped by monitor", and the printer state flips to `FAILED`.
- [ ] **Step 3: Detection page** — the new sidebar entry: toggle **Enable detection**, switch **Camera source** (A1 ↔ webcam), move the **confidence** slider, check/uncheck **arming classes**; confirm each persists (the health pill + values reflect the WS update).
- [ ] **Step 4: Real printer** — add the real printer (IP `192.168.137.108`), mark it capture, set source **A1 built-in**, enable detection: confirm the live A1 feed renders and the health pill shows `running · ~0.4 fps`. Leave auto-stop **disarmed** (the destructive stop is a separate, explicitly-gated test).
- [ ] **Step 5: Record** any visual issues; fix-forward with a follow-up commit if needed.

---

## Self-Review (completed while writing)

**Spec coverage (Detection UI, mockup B + source selector):** Dashboard auto-stop card w/ arm toggle + countdown + latch (T2/T3) ✅; live detection feed on the capture printer (T3) ✅; Detection page with source selector, webcam index (conditional), threshold, enable, per-class arming, health, preview (T4) ✅; nav entry (T4) ✅.

**Reads live state off the WS** (no redundant GET) — `detection` object from `usePrinters()`; writes via `updateDetection`/`armDetection` (T1), and the WS reflects changes on the next tick.

**Consistency:** uses only existing primitives (`Card`/`Button`/`Field`/`StatusPill`/`Columns`/`PageFrame`); new CSS is class-prefixed (`autostop`/`detect-*`) like the rest of `styles.css`. Non-capture selection is handled (hint), matching the backend's capture-only detection.

**No test framework** in this project → verification is `npm run build` + the browser check (T5), stated up front rather than pretending unit tests exist.

**Not in scope:** the queue (Phase 2, its own plan); the destructive stop-command hardware test (separate, gated).
