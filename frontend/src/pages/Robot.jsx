import { useEffect, useMemo, useRef, useState } from "react";

import {
  cancelRobotCommand,
  configureRobotCamera,
  configureRobotGripper,
  fetchRobotCameras,
  sendRobotCommand,
} from "../api/robot.js";
import { blockerText, gripperLabel, gripperOutputView }
  from "../components/robot/gripperOutput.js";
import Button from "../components/ui/Button.jsx";
import Card from "../components/ui/Card.jsx";
import Field from "../components/ui/Field.jsx";
import PageFrame from "../components/ui/PageFrame.jsx";
import Section from "../components/ui/Section.jsx";
import StatusPill from "../components/ui/StatusPill.jsx";


const DEFAULT_JOINTS_DEG = ["0", "-15", "20", "0", "0", "0"];
const EMPTY_POSE = ["", "", "", "", "", ""];
const RAD = Math.PI / 180;
// Mirrors MARKER_ROLES in server/robot.py; the server rejects anything else.
const MARKER_ROLES = ["printer", "box", "scrape", "other"];
// The thresholds VisionCommissioning.solve() applies before it will write
// calibration/<robot>_hand_eye.json. Shown so a rejected solve explains itself.
const SOLVE_LIMITS = { translationM: 0.01, rotationDeg: 3.0 };


function parseVector(values, labels) {
  const parsed = values.map((value) => Number(value));
  const bad = parsed.findIndex((value) => !Number.isFinite(value));
  if (bad !== -1) throw new Error(`${labels[bad]} must be a number`);
  return parsed;
}


function commandBusy(robot) {
  return Boolean(robot?.active_command) ||
    ["queued", "executing", "cancelling"].includes(robot?.state);
}


function robotPill(robot) {
  if (!robot) return { status: "warn", label: "Not configured" };
  if (!robot.available || robot.state === "error") {
    return { status: "danger", label: robot.state === "error" ? "Error" : "Unavailable" };
  }
  if (commandBusy(robot)) return { status: "warn", label: robot.state };
  return { status: "ok", label: "Ready" };
}


function VectorFields({ values, setValues, labels, step }) {
  const update = (index) => (event) => {
    const value = event.target.value;
    setValues((current) =>
      current.map((item, itemIndex) => itemIndex === index ? value : item));
  };
  return (
    <div className="robot-vector">
      {labels.map((label, index) => (
        <Field key={label} label={label} type="number" step={step}
               value={values[index]} onChange={update(index)} />
      ))}
    </div>
  );
}


function formatNumber(value, digits = 3) {
  return Number.isFinite(Number(value)) ? Number(value).toFixed(digits) : "—";
}


function RoleSelect({ label, value, onChange }) {
  return (
    <Field label={label}>
      <select value={value} onChange={(event) => onChange(event.target.value)}>
        {MARKER_ROLES.map((role) => (
          <option key={role} value={role}>{role}</option>
        ))}
      </select>
    </Field>
  );
}


// The detector no longer stacks a pose panel onto the video frame -- it made
// the camera image small in a browser preview. Marker detail comes off the
// telemetry instead and is rendered here, beside the feed.
function MarkerDetail({ marker, role }) {
  const orient = marker?.orientInWorld ?? {};
  const position = marker?.positionInWorld ?? [];
  return (
    <div className="robot-marker">
      <div className="robot-marker__head">
        <strong>ArUco {marker?.id}</strong>
        <StatusPill status={marker?.estimated ? "warn" : "ok"}>
          {marker?.estimated ? "Estimated" : "Detected"}
        </StatusPill>
      </div>
      <div className="robot-marker__meta">
        {marker?.dict_name ?? "unknown"}
        {role ? ` · ${role}` : ""}
        {Number(marker?.distanceFromCamera) > 0
          ? ` · ${formatNumber(marker.distanceFromCamera, 3)} m from camera`
          : ""}
      </div>
      <div className="robot-marker__pose">
        <span>X {formatNumber(position[0])}</span>
        <span>Y {formatNumber(position[1])}</span>
        <span>Z {formatNumber(position[2])}</span>
        <span>R {formatNumber(orient.roll, 1)}°</span>
        <span>P {formatNumber(orient.pitch, 1)}°</span>
        <span>Y {formatNumber(orient.yaw, 1)}°</span>
      </div>
    </div>
  );
}


function SolveReadout({ title, solve }) {
  if (!solve) return null;
  const metrics = solve.metrics ?? {};
  const translation = solve.translation_m ?? [];
  return (
    <div className={solve.quality_passed ? "state-ok robot-notice"
                                         : "state-warn robot-notice"}>
      <strong>
        {title} · {solve.method ?? "—"} · {solve.sample_count ?? 0} samples ·{" "}
        {solve.quality_passed ? "accepted" : "rejected"}
      </strong>
      <div>
        Translation RMSE {formatNumber(metrics.translation_rmse_m, 4)} m
        (limit {SOLVE_LIMITS.translationM.toFixed(4)}) · rotation RMSE{" "}
        {formatNumber(metrics.rotation_rmse_deg, 2)}°
        (limit {SOLVE_LIMITS.rotationDeg.toFixed(2)})
      </div>
      <div>
        Camera offset from the gripper: X {formatNumber(translation[0], 4)} ·
        Y {formatNumber(translation[1], 4)} ·
        Z {formatNumber(translation[2], 4)} m
      </div>
    </div>
  );
}

function IntrinsicReadout({ solve }) {
  if (!solve) return null;
  return (
    <div className={solve.quality_passed ? "state-ok robot-notice"
                                         : "state-warn robot-notice"}>
      <strong>
        Camera intrinsics · {solve.sample_count ?? 0} captures · {solve.quality_passed ? "accepted" : "rejected"}
      </strong>
      <div>
        Reprojection RMS {formatNumber(solve.rms_px, 3)} px
        {Number.isFinite(Number(solve.max_rms_px)) && ` (limit ${formatNumber(solve.max_rms_px, 1)} px)`}
      </div>
      {solve.image_size && <div>Resolution {solve.image_size.join(" × ")} px</div>}
    </div>
  );
}


export default function Robot({ robot, wsUp }) {
  const [armed, setArmed] = useState(false);
  const [jointValues, setJointValues] = useState(DEFAULT_JOINTS_DEG);
  const [poseValues, setPoseValues] = useState(EMPTY_POSE);
  const [markerId, setMarkerId] = useState("2");
  const [viewingDistance, setViewingDistance] = useState("0.20");
  const [sourceId, setSourceId] = useState("2");
  const [destinationId, setDestinationId] = useState("0");
  const [scrapeId, setScrapeId] = useState("1");
  const [jogStepMm, setJogStepMm] = useState("5");
  const [pendingJog, setPendingJog] = useState({ x: 0, y: 0, z: 0 });
  const [cameraDevices, setCameraDevices] = useState([]);
  const [cameraIndex, setCameraIndex] = useState("");
  const [cameraFrameKey, setCameraFrameKey] = useState(0);
  const [scanPosition, setScanPosition] = useState(["0.30", "0.00", "0.30"]);
  const [scanEuler, setScanEuler] = useState(["0", "0", "0"]);
  const [observationName, setObservationName] = useState("");
  const [observationMarkerId, setObservationMarkerId] = useState("");
  const [observationRole, setObservationRole] = useState("printer");
  const [confirmRole, setConfirmRole] = useState("printer");
  const [gripperOutput, setGripperOutput] = useState("");
  const jogDispatching = useRef(false);
  // The id of the jog the server has accepted but not yet reported finished.
  // See the release effect below for why 202 is not good enough.
  const outstandingJog = useRef(null);
  const [submitting, setSubmitting] = useState(false);
  const [notice, setNotice] = useState(null);
  const [error, setError] = useState(null);

  const busy = commandBusy(robot);
  const safetyReady = robot?.safety?.ready !== false;
  const controlsEnabled = Boolean(
    wsUp && robot?.available && safetyReady && armed && !busy);
  const pill = robotPill(robot);
  const jointsDeg = useMemo(
    () => (robot?.joints ?? []).map((value) => Number(value) / RAD),
    [robot?.joints],
  );

  const run = async (action, parameters = {}) => {
    setSubmitting(true);
    setError(null);
    setNotice(null);
    try {
      const command = await sendRobotCommand(action, parameters);
      setNotice(`${action} accepted · ${command.id.slice(0, 8)}`);
      return command;
    } catch (err) {
      setError(err.message);
      return null;
    } finally {
      setSubmitting(false);
    }
  };

  const moveJoints = async (event) => {
    event.preventDefault();
    try {
      const degrees = parseVector(
        jointValues, ["J1", "J2", "J3", "J4", "J5", "J6"]);
      await run("move_joints", { joints: degrees.map((value) => value * RAD) });
    } catch (err) {
      setError(err.message);
    }
  };

  const movePose = async (event) => {
    event.preventDefault();
    try {
      const values = parseVector(
        poseValues, ["X", "Y", "Z", "Roll", "Pitch", "Yaw"]);
      await run("move_pose", {
        position: values.slice(0, 3),
        euler: values.slice(3).map((value) => value * RAD),
      });
    } catch (err) {
      setError(err.message);
    }
  };

  const useMeasuredPose = () => {
    const measured = robot?.eef_pose?.xyz_rpy;
    if (!Array.isArray(measured) || measured.length !== 6 ||
        measured.some((value) => !Number.isFinite(Number(value)) ||
          Number(value) === -1)) {
      setError("A measured end-effector pose is not available yet");
      return;
    }
    setError(null);
    setPoseValues(measured.map((value, index) =>
      (index < 3 ? Number(value) : Number(value) / RAD).toFixed(
        index < 3 ? 4 : 1)));
  };

  const stop = async () => {
    const id = robot?.active_command?.id;
    if (!id) return;
    setSubmitting(true);
    setError(null);
    try {
      await cancelRobotCommand(id);
      setNotice(`Stop requested · ${id.slice(0, 8)}`);
    } catch (err) {
      setError(err.message);
    } finally {
      setSubmitting(false);
    }
  };

  const goal = (action, parameters) => () => run(action, parameters);
  const jog = (axis, direction) => {
    const delta = direction * Number(jogStepMm) / 1000;
    if (!busy && !submitting && !jogDispatching.current) {
      // Claim the dispatch slot SYNCHRONOUSLY. `busy` and `submitting` are
      // state, so they stay stale for the whole render -- several clicks in
      // one tick would all pass this guard, and only the first would get a
      // 202; the rest came back 409 and surfaced as an error, which is
      // exactly the flooding the comment below promises to prevent. A ref
      // updates immediately, so click 2 and 3 fall through and coalesce.
      jogDispatching.current = true;
      run("jog_pose", { axis, delta }).then((command) => {
        if (command) outstandingJog.current = command.id;
        // A REJECTED submit owes us nothing, so free the slot immediately --
        // otherwise one failed jog would wedge the pad until a reload.
        else jogDispatching.current = false;
      });
      return;
    }
    // While a jog is executing, retain at most 50 mm per axis. Repeated
    // clicks coalesce instead of flooding MoveIt or freezing the controls.
    setPendingJog((current) => ({
      ...current,
      [axis]: Math.max(-0.05, Math.min(0.05, current[axis] + delta)),
    }));
  };
  const marker = Number(markerId);
  const source = Number(sourceId);
  const destination = Number(destinationId);
  const scraper = Number(scrapeId);
  const goalDisabled = !controlsEnabled || submitting;
  const gripper = robot?.gripper;
  // Only disable on a POSITIVE "no gripper here". An older backend sends no
  // gripper key at all, and greying the controls out on a missing key would
  // hide a working gripper behind a telemetry gap.
  const gripperMissing = Boolean(
    gripper && (!gripper.kind || gripper.disabled));
  // The controller-output picker. `gripperOutput` holds only what the operator
  // has picked; an untouched picker follows the live setting from telemetry,
  // and shows CO0 when there is no arm to ask. Null means this gripper has no
  // output to select at all (Lite 6 service, MoveIt action).
  const outputView = gripperOutputView(
    { robot, gripper, selection: gripperOutput, busy });
  const vision = robot?.vision;
  const intrinsic = vision?.intrinsic;
  // Commissioning reads the camera and the current TF but never plans a goal,
  // so it deliberately does NOT require the movement interlock: the operator
  // captures samples while hand-guiding the wrist in teach mode, which is
  // exactly when the safety preflight is (correctly) refusing motion.
  const visionDisabled = Boolean(
    !wsUp || !robot?.available || !vision?.available || busy || submitting);
  const samples = Number(vision?.sample_count ?? 0);
  const markerRoles = vision?.marker_roles ?? {};
  const observations = vision?.observation_poses ?? [];
  const jogEnabled = Boolean(
    wsUp && robot?.available && safetyReady && armed &&
    (!busy || robot?.active_command?.action === "jog_pose"),
  );

  useEffect(() => {
    if (busy || submitting || jogDispatching.current) return;
    const next = Object.entries(pendingJog).find(
      ([, delta]) => Math.abs(delta) > 1e-9);
    if (!next) return;
    const [axis, delta] = next;
    jogDispatching.current = true;
    setPendingJog((current) => ({ ...current, [axis]: 0 }));
    setSubmitting(true);
    setError(null);
    sendRobotCommand("jog_pose", { axis, delta })
      .then((command) => {
        outstandingJog.current = command.id;
        setNotice(`jog_pose accepted · ${command.id.slice(0, 8)}`);
      })
      .catch((err) => {
        setError(err.message);
        jogDispatching.current = false;
      })
      .finally(() => setSubmitting(false));
    // `robot` is a dependency because the release effect below clears
    // jogDispatching through a REF, which cannot itself trigger a re-render.
    // Every WebSocket frame re-runs this, which is how a coalesced jog gets
    // dispatched once the previous one is confirmed done.
  }, [busy, submitting, pendingJog, robot]);

  // Free the jog slot only once the SERVER says our command finished.
  //
  // Neither of the two obvious signals is sufficient on its own. run()
  // resolves at HTTP 202 -- ACCEPTED, not completed -- so `submitting` goes
  // false while the arm is still moving. And `busy` is telemetry off the
  // WebSocket, so it lags by up to one poll interval and still reads "idle"
  // for a moment after a command is queued. Releasing on either one dispatches
  // the next jog into a robot that has not stopped, and the server correctly
  // refuses it with a 409 that surfaces as an error the operator did nothing
  // to deserve. Waiting for OUR id to appear as last_command is the only
  // signal that actually means "that jog is over".
  useEffect(() => {
    const id = outstandingJog.current;
    if (!id || !robot) return;
    if (robot.active_command?.id !== id && robot.last_command?.id === id) {
      outstandingJog.current = null;
      jogDispatching.current = false;
    }
  }, [robot]);

  const last = robot?.last_command;
  const pose = robot?.eef_pose?.xyz_rpy ?? [];

  const loadCameras = () => {
    fetchRobotCameras()
      .then((payload) => setCameraDevices(payload.devices ?? []))
      .catch((err) => setError(err.message));
  };

  useEffect(() => {
    loadCameras();
  }, []);

  useEffect(() => {
    if (robot?.camera?.color_age_s == null) return undefined;
    const timer = window.setInterval(
      // The backend serves the latest JPEG, so this does not increase camera
      // capture load. Five FPS is responsive enough for aiming a board while
      // avoiding a needless JPEG encode on every 30 FPS capture frame.
      () => setCameraFrameKey((value) => value + 1), 200);
    return () => window.clearInterval(timer);
  }, [robot?.camera?.color_age_s == null]);

  const applyCamera = async () => {
    setSubmitting(true);
    setError(null);
    try {
      await configureRobotCamera(cameraIndex === "" ? null : Number(cameraIndex));
      // configure_camera reopens the capture in place -- it does NOT restart
      // the backend, and saying so sent operators looking for a reconnect
      // that never happens.
      setNotice(cameraIndex === "" ? "Robot camera disabled" :
        `Camera /dev/video${cameraIndex} selected`);
    } catch (err) {
      setError(err.message);
    } finally {
      setSubmitting(false);
    }
  };

  const saveObservation = async () => {
    const name = observationName.trim();
    if (!name) {
      setError("Name the observation pose before saving it");
      return;
    }
    const command = await run("save_observation", {
      name,
      role: observationRole,
      marker_id: observationMarkerId === "" ? null : Number(observationMarkerId),
    });
    if (command) setObservationName("");
  };

  const applyGripperOutput = async () => {
    setSubmitting(true);
    setError(null);
    setNotice(null);
    try {
      const output = Number(outputView.value);
      await configureRobotGripper(output);
      setNotice(`Gripper output set to CO${output}`);
      // Hand the picker back to telemetry, which now carries the new pin.
      setGripperOutput("");
    } catch (err) {
      setError(err.message);
    } finally {
      setSubmitting(false);
    }
  };

  const scanEstimatedLocation = async () => {
    try {
      const position = parseVector(scanPosition, ["X", "Y", "Z"]);
      const eulerDeg = parseVector(scanEuler, ["Roll", "Pitch", "Yaw"]);
      await run("scan_location", {
        position,
        euler: eulerDeg.map((value) => value * RAD),
        viewing_distance: Number(viewingDistance),
      });
    } catch (err) {
      setError(err.message);
    }
  };

  return (
    <PageFrame>
      <Section title="Robot connection">
        <Card>
          <div className="robot-status">
            <div>
              <div className="robot-status__name">
                {robot?.robot?.toUpperCase() ?? "Robot backend"}
                {robot?.sim ? " · Gazebo" : ""}
              </div>
              <div className="robot-status__meta">
                {robot?.error ?? "Commands are serialized; only one goal can run at a time."}
              </div>
            </div>
            <StatusPill status={pill.status}>{pill.label}</StatusPill>
          </div>
          {!robot && (
            <div className="state-warn robot-notice">
              Start the server with <code>--robot-mode mock</code> or
              <code> --robot-mode ros --robot-sim</code>.
            </div>
          )}
          <label className="robot-enable">
            <input type="checkbox" checked={armed}
                   onChange={(event) => setArmed(event.target.checked)}
                   disabled={!robot?.available || !safetyReady} />
            Enable movement controls — I have verified the workspace is clear
          </label>
          {robot?.safety && (
            <div className={safetyReady ? "state-ok robot-notice" :
                                            "state-error robot-notice"}>
              <strong>
                Safety preflight: {safetyReady ? "ready" : "movement blocked"}
              </strong>
              <div>
                {(robot.safety.checks ?? []).map((check) => (
                  <div key={check.name}>
                    {check.ok ? "✓" : "✕"} {check.name}: {check.detail}
                  </div>
                ))}
              </div>
            </div>
          )}
          {!robot?.sim && robot?.available && !safetyReady && (
            <div className="robot-actions">
              <Button variant="primary" disabled={busy || submitting}
                      busy={submitting} onClick={() => run("prepare_motion")}>
                Prepare robot for GUI control
              </Button>
            </div>
          )}
          <div className="robot-actions">
            <Button variant="primary" disabled={!controlsEnabled}
                    busy={submitting} onClick={() => run("home")}>
              Home robot
            </Button>
            <Button variant="danger" disabled={!busy || submitting}
                    onClick={stop}>
              Stop active goal
            </Button>
          </div>
          {notice && <div className="state-ok robot-notice">{notice}</div>}
          {error && <div className="state-error robot-notice">{error}</div>}
        </Card>
      </Section>

      <Section title="Movement goals">
        <div className="robot-grid">
          <Card title="Joint goal">
            <form className="robot-form" onSubmit={moveJoints}>
              <VectorFields values={jointValues} setValues={setJointValues}
                            labels={["J1°", "J2°", "J3°", "J4°", "J5°", "J6°"]}
                            step="1" />
              <div className="robot-form__actions">
                <Button type="submit" variant="primary"
                        disabled={!controlsEnabled} busy={submitting}>
                  Move joints
                </Button>
                <Button onClick={() => setJointValues(DEFAULT_JOINTS_DEG)}>
                  Safe test preset
                </Button>
              </div>
            </form>
          </Card>

          <Card title="Cartesian pose goal">
            <form className="robot-form" onSubmit={movePose}>
              <VectorFields values={poseValues} setValues={setPoseValues}
                            labels={["X m", "Y m", "Z m", "Roll°", "Pitch°", "Yaw°"]}
                            step="0.01" />
              <div className="robot-form__actions">
                <Button type="submit" variant="primary"
                        disabled={!controlsEnabled} busy={submitting}>
                  Move to pose
                </Button>
                <Button onClick={useMeasuredPose}
                        disabled={!robot?.eef_pose?.xyz_rpy}>
                  Use measured pose
                </Button>
                <span className="ui-field__help">
                  Position uses the automation “good” frame; orientation is XYZ Euler.
                </span>
              </div>
            </form>
          </Card>

          <Card title="Cartesian jog">
            <div className="jog-layout">
              <div className="jog-pad" aria-label="XY Cartesian jog control">
                <button className="jog-pad__button jog-pad__button--north"
                        disabled={!jogEnabled} onClick={() => jog("y", 1)}
                        aria-label="Move positive Y">Y+</button>
                <button className="jog-pad__button jog-pad__button--west"
                        disabled={!jogEnabled} onClick={() => jog("x", -1)}
                        aria-label="Move negative X">X−</button>
                <div className="jog-pad__center">XY</div>
                <button className="jog-pad__button jog-pad__button--east"
                        disabled={!jogEnabled} onClick={() => jog("x", 1)}
                        aria-label="Move positive X">X+</button>
                <button className="jog-pad__button jog-pad__button--south"
                        disabled={!jogEnabled} onClick={() => jog("y", -1)}
                        aria-label="Move negative Y">Y−</button>
              </div>
              <div className="jog-z">
                <button className="jog-axis-button" disabled={!jogEnabled}
                        onClick={() => jog("z", 1)}>Z+</button>
                <span>Z</span>
                <button className="jog-axis-button" disabled={!jogEnabled}
                        onClick={() => jog("z", -1)}>Z−</button>
              </div>
            </div>
            <div className="jog-step">
              <label htmlFor="jog-step">Step</label>
              <select id="jog-step" value={jogStepMm}
                      onChange={(event) => setJogStepMm(event.target.value)}>
                <option value="1">1 mm</option>
                <option value="5">5 mm</option>
                <option value="10">10 mm</option>
                <option value="25">25 mm</option>
              </select>
            </div>
            <p className="robot-help">
              Each click plans one small MoveIt goal. Clicks made during a jog
              are safely combined and run next.
            </p>
            {Object.values(pendingJog).some((delta) => Math.abs(delta) > 1e-9) && (
              <p className="robot-help">
                Queued: X {(pendingJog.x * 1000).toFixed(0)} mm ·
                Y {(pendingJog.y * 1000).toFixed(0)} mm ·
                Z {(pendingJog.z * 1000).toFixed(0)} mm
              </p>
            )}
          </Card>
        </div>
      </Section>

      <Section title="Automation goals">
        <div className="robot-grid">
          <Card title="ArUco and plate actions">
            <div className="robot-goal-fields">
              <Field label="Marker ID" type="number" min="0" step="1"
                     value={markerId}
                     onChange={(event) => setMarkerId(event.target.value)} />
              <Field label="Scan distance (m)" type="number" min="0.05"
                     max="0.50" step="0.01" value={viewingDistance}
                     onChange={(event) => setViewingDistance(event.target.value)} />
            </div>
            <div className="robot-goal-buttons">
              <Button variant="primary" disabled={goalDisabled}
                      onClick={goal("scan_marker", {
                        marker_id: marker,
                        viewing_distance: Number(viewingDistance),
                      })}>Scan ArUco</Button>
              <Button disabled={goalDisabled}
                      onClick={goal("pickup", { marker_id: marker })}>
                Pick plate
              </Button>
              <Button disabled={goalDisabled}
                      onClick={goal("place", { marker_id: marker })}>
                Place plate
              </Button>
            </div>
          </Card>

          <Card title="Workflow actions">
            <div className="robot-goal-fields robot-goal-fields--three">
              <Field label="Source ID" type="number" min="0" step="1"
                     value={sourceId}
                     onChange={(event) => setSourceId(event.target.value)} />
              <Field label="Destination ID" type="number" min="0" step="1"
                     value={destinationId}
                     onChange={(event) => setDestinationId(event.target.value)} />
              <Field label="Scrape ID" type="number" min="0" step="1"
                     value={scrapeId}
                     onChange={(event) => setScrapeId(event.target.value)} />
            </div>
            <div className="robot-goal-buttons">
              <Button variant="primary" disabled={goalDisabled}
                      onClick={goal("transfer", {
                        source_id: source,
                        dest_id: destination,
                        rescan_id: scraper,
                      })}>Transfer plate</Button>
              <Button disabled={goalDisabled}
                      onClick={goal("scrape", {
                        source_id: source,
                        scrape_id: scraper,
                      })}>Scrape plate</Button>
            </div>
          </Card>

          <Card title="Gripper">
            <div className="robot-goal-buttons">
              <Button disabled={goalDisabled || gripperMissing}
                      onClick={goal("gripper_open")}>Open gripper</Button>
              <Button variant={gripper?.command === "close" ? "primary"
                                                            : "secondary"}
                      disabled={goalDisabled || gripperMissing}
                      onClick={goal("gripper_close")}>Close gripper</Button>
            </div>
            {gripper && (
              <div className="robot-readout">
                <div>
                  <span>Hardware</span>
                  <strong>{gripperLabel(gripper)}</strong>
                </div>
                <div>
                  <span>Last commanded</span>
                  <strong>{gripper.command === "close" ? "Closed"
                         : gripper.command === "open" ? "Open" : "Unknown"}</strong>
                </div>
              </div>
            )}
            {outputView ? (
              <div className="robot-output-config">
                <Field label="Controller output">
                  <select value={outputView.value}
                          onChange={(event) => setGripperOutput(event.target.value)}>
                    {outputView.outputs.map((index) => (
                      <option key={index} value={index}>CO{index}</option>
                    ))}
                  </select>
                </Field>
                <Button busy={submitting}
                        disabled={outputView.blocker !== null || submitting}
                        onClick={applyGripperOutput}>
                  Apply output
                </Button>
              </div>
            ) : (
              <p className="robot-help">
                {gripperLabel(gripper)} has no controller output to select.
              </p>
            )}
            {outputView && (
              <p className="robot-help">
                Which CO pin on the control box the gripper is wired to
                (CO0–CO{outputView.outputs.length - 1}). Applies on the next
                open/close, no restart.
                {blockerText(outputView.blocker) &&
                  ` ${blockerText(outputView.blocker)}`}
              </p>
            )}
            {gripperMissing && (
              <div className="state-warn robot-notice">
                No gripper is configured for this arm, so pick, place, transfer
                and scrape are blocked before their first waypoint rather than
                reporting a grasp that never happened.
              </div>
            )}
            {gripper && !gripper.sensed && !gripperMissing && (
              <p className="robot-help">
                “Last commanded” is what was asked for, not what the jaws did —
                this gripper has no feedback line. The output latches, so it
                survives a backend restart, and an emergency stop drops it
                without reporting anything: “Unknown” means unknown, not open.
              </p>
            )}
            {robot?.sim && (
              <p className="robot-help">
                Gripper actuation is disabled by the automation package in simulation.
              </p>
            )}
          </Card>

          <Card title="Known markers">
            {Array.isArray(robot?.markers) && robot.markers.length > 0 ? (
              <div className="robot-marker-list">
                {robot.markers.map((data) => (
                  <MarkerDetail key={`${data?.dict_name}-${data?.id}`}
                                marker={data}
                                role={markerRoles[String(data?.id)]} />
                ))}
              </div>
            ) : (
              <p className="robot-help">No markers have been registered yet.</p>
            )}
            <p className="robot-help">
              Positions are in the automation “good” frame; orientation is XYZ
              Euler in degrees. An estimated marker has never been measured by
              the camera — scan it before picking from it.
            </p>
          </Card>
          <Card title="ArUco detector">
            <div className="robot-readout">
              <div>
                <span>Color stream</span>
                <strong>{robot?.camera?.color_age_s == null ? "No frames" :
                  `${formatNumber(robot.camera.color_age_s, 2)} s ago`}</strong>
              </div>
              <div>
                <span>Calibration</span>
                <strong>{robot?.camera?.calibrated ? "Ready" : "Missing"}</strong>
              </div>
              <div>
                <span>Camera frame</span>
                <strong>{robot?.camera?.camera_frame ?? "—"}</strong>
              </div>
              <div>
                <span>Effective resolution</span>
                <strong>{robot?.camera?.image_size_px?.join(" × ") ?? "—"} px</strong>
              </div>
              <div>
                <span>Camera source rate</span>
                <strong>{robot?.camera?.effective_fps == null ? "Measuring…" : `${formatNumber(robot.camera.effective_fps, 1)} FPS`}</strong>
              </div>
              <div>
                <span>Visible / known</span>
                <strong>{robot?.camera ?
                  `${robot.camera.visible_marker_count} / ${robot.camera.known_marker_count}` : "—"}</strong>
              </div>
              <div>
                <span>In view now</span>
                <strong>{(robot?.camera?.visible_marker_ids ?? []).length > 0
                  ? robot.camera.visible_marker_ids.join(", ") : "None"}</strong>
              </div>
            </div>
            {robot?.camera?.last_error && (
              <div className="state-error robot-notice">{robot.camera.last_error}</div>
            )}
            {robot?.camera && (
              <p className="robot-help">
                Dictionaries: {(robot.camera.marker_dictionaries ?? []).join(", ")} ·
                Sizes: {(robot.camera.marker_sizes_m ?? []).join(", ")} m
              </p>
            )}
          </Card>
        </div>
      </Section>

      <Section title="Live feedback">
        <div className="robot-grid">
          <Card title="Measured joints">
            <div className="robot-readout">
              {Array.from({ length: 6 }, (_, index) => (
                <div key={index}>
                  <span>J{index + 1}</span>
                  <strong>{formatNumber(jointsDeg[index], 1)}°</strong>
                </div>
              ))}
            </div>
          </Card>
          <Card title="End-effector pose">
            <div className="robot-readout">
              {["X", "Y", "Z", "Roll", "Pitch", "Yaw"].map((label, index) => (
                <div key={label}>
                  <span>{label}</span>
                  <strong>{formatNumber(pose[index])}</strong>
                </div>
              ))}
            </div>
          </Card>
        </div>
        {last && (
          <Card title="Last command">
            <dl className="kv">
              <dt>Action</dt><dd>{last.action}</dd>
              <dt>State</dt><dd>{last.state}</dd>
              <dt>Command ID</dt><dd><code>{last.id}</code></dd>
              {last.error && <><dt>Error</dt><dd>{last.error}</dd></>}
            </dl>
          </Card>
        )}
      </Section>

      <Section title="Vision and ArUco commissioning">
        <div className="robot-grid">
          <Card title="USB camera">
            <div className="robot-camera-config">
              <select value={cameraIndex}
                      onChange={(event) => setCameraIndex(event.target.value)}>
                <option value="">Disabled (movement only)</option>
                {cameraDevices.map((device) => (
                  <option key={device.index} value={device.index}>
                    {device.path} · {device.name}
                  </option>
                ))}
              </select>
              <Button onClick={loadCameras}>Refresh cameras</Button>
              <Button onClick={applyCamera} busy={submitting}>Apply camera</Button>
            </div>
            <div className="robot-camera-preview">
              {robot?.camera?.color_age_s == null ? (
                <span>No camera frames</span>
              ) : (
                <img src={`/api/robot/camera/frame?v=${cameraFrameKey}`}
                     alt="Robot camera with ArUco overlay" />
              )}
            </div>
            <p className="robot-camera-detection" aria-live="polite">
              {(robot?.camera?.visible_marker_ids ?? []).length > 0
                ? `ArUco in view: ID ${(robot.camera.visible_marker_ids ?? []).join(", ")}`
                : "No ArUco detected in view"}
            </p>
          </Card>

          <Card title="1. Camera intrinsic calibration">
            {!vision?.available ? (
              <div className="state-warn robot-notice">
                Start the robot vision backend before calibrating the camera.
              </div>
            ) : (
              <>
                <p className="robot-help">
                  Do this once for the selected wrist camera before hand–eye.
                  Between captures, move the wrist camera/robot or the board
                  so the board covers the center, edges, and corners; keep
                  both still for each capture. Collect 20+ sharp views. Do
                  not mix cameras, resolutions, or zoom/focus settings in one
                  session.
                </p>
                <div className="robot-readout">
                  <div><span>Session</span><strong>{intrinsic?.session_active ? "Active" : "Stopped"}</strong></div>
                  <div><span>Captures</span><strong>{intrinsic?.sample_count ?? 0} / {intrinsic?.recommended_sample_count ?? 20}</strong></div>
                  <div><span>Board in view</span><strong>{intrinsic?.last_detection?.valid ? `${intrinsic.last_detection.corner_count} corners` : "Not detected"}</strong></div>
                </div>
                {intrinsic?.session_active && intrinsic?.last_detection?.error && (
                  <div className="state-warn robot-notice">{intrinsic.last_detection.error}</div>
                )}
                <div className="robot-goal-buttons">
                  {intrinsic?.session_active ? <>
                    <Button variant="primary" disabled={visionDisabled}
                            onClick={goal("intrinsic_capture")}>Capture camera view</Button>
                    <Button disabled={visionDisabled || !(intrinsic?.sample_count)}
                            onClick={goal("intrinsic_discard")}>Discard last</Button>
                    <Button disabled={visionDisabled || (intrinsic?.sample_count ?? 0) < 12}
                            onClick={goal("intrinsic_solve")}>Solve and apply intrinsics</Button>
                    <Button disabled={visionDisabled}
                            onClick={goal("intrinsic_stop")}>End session</Button>
                  </> : <>
                    <Button variant="primary" disabled={visionDisabled}
                            onClick={goal("intrinsic_start", { clear: false })}>
                      {(intrinsic?.sample_count ?? 0) ? "Resume captures" : "Start intrinsic calibration"}
                    </Button>
                    <Button disabled={visionDisabled || !(intrinsic?.sample_count)}
                            onClick={goal("intrinsic_start", { clear: true })}>Start over</Button>
                  </>}
                </div>
                <IntrinsicReadout solve={intrinsic?.last_solve} />
                <p className="robot-help">
                  Only an accepted result replaces <code>camera_matrix.npz</code>;
                  the prior file is kept as <code>camera_matrix.previous.npz</code>.
                  Then start a fresh hand–eye session — do not reuse its old samples.
                </p>
              </>
            )}
          </Card>

          <Card title="Manual guidance">
            <p className="robot-help">
              Support the arm and keep the emergency stop accessible before
              enabling UFACTORY teach mode. Entering teach mode hands the
              trajectory controller back from MoveIt, so the safety preflight
              below will show <code>trajectory_controller</code> red and refuse
              motion until you return to MoveIt. That is expected.
            </p>
            <div className="robot-form__actions">
              <Button disabled={!robot?.available || busy || submitting}
                      onClick={() => run("teach_enable")}>Enter teach mode</Button>
              <Button variant="primary"
                      disabled={!robot?.available || busy || submitting}
                      onClick={() => run("teach_disable")}>Return to MoveIt</Button>
            </div>
            {robot?.sim && (
              <p className="robot-help">
                Teach mode is physical-only; the automation package refuses it
                against Gazebo.
              </p>
            )}
          </Card>

          <Card title="Scan estimated location">
            <VectorFields values={scanPosition} setValues={setScanPosition}
                          labels={["X m", "Y m", "Z m"]} step="0.01" />
            <VectorFields values={scanEuler} setValues={setScanEuler}
                          labels={["Roll°", "Pitch°", "Yaw°"]} step="1" />
            <div className="robot-form__actions">
              <Button variant="primary" disabled={!controlsEnabled || submitting}
                      onClick={scanEstimatedLocation}>
                Scan location for ArUcos
              </Button>
            </div>
          </Card>

          <Card title="ChArUco hand-eye calibration">
            {!vision?.available ? (
              <div className="state-warn robot-notice">
                {vision?.error ??
                  "This backend has no vision commissioning module."}
              </div>
            ) : (
              <>
                <p className="robot-help">
                  Keep the board fixed and in view. Hand-guide the wrist with
                  teach mode and capture {vision.recommended_sample_count ?? 15}
                  {" "}varied poses — each must differ from the last by at
                  least 10 mm or 5°, and the arm must be stationary. A solve
                  that passes is written to the automation repo and applied
                  immediately, with no restart.
                </p>
                <div className="robot-readout">
                  <div>
                    <span>Session</span>
                    <strong>{vision.session_active ? "Active" : "Stopped"}</strong>
                  </div>
                  <div>
                    <span>Samples</span>
                    <strong>
                      {samples} / {vision.recommended_sample_count ?? 15}
                    </strong>
                  </div>
                  <div>
                    <span>Board in view</span>
                    <strong>{vision.last_detection?.valid
                      ? `${vision.last_detection.corner_count} corners`
                      : "Not detected"}</strong>
                  </div>
                </div>
                {vision.session_active && vision.last_detection?.error && (
                  <div className="state-warn robot-notice">
                    {vision.last_detection.error}
                  </div>
                )}
                <div className="robot-goal-buttons">
                  {vision.session_active ? (
                    <>
                      <Button variant="primary" disabled={visionDisabled}
                              onClick={goal("calibration_capture")}>
                        Capture sample
                      </Button>
                      <Button disabled={visionDisabled || samples === 0}
                              onClick={goal("calibration_discard")}>
                        Discard last
                      </Button>
                      <Button disabled={visionDisabled || samples < 8}
                              onClick={goal("calibration_solve")}>
                        Solve hand-eye
                      </Button>
                      <Button disabled={visionDisabled}
                              onClick={goal("calibration_stop")}>
                        End session
                      </Button>
                    </>
                  ) : (
                    <>
                      <Button variant="primary" disabled={visionDisabled}
                              onClick={goal("calibration_start", { clear: false })}>
                        {samples > 0 ? "Resume session" : "Start session"}
                      </Button>
                      <Button disabled={visionDisabled || samples === 0}
                              onClick={goal("calibration_start", { clear: true })}>
                        Start over
                      </Button>
                    </>
                  )}
                </div>
                {vision.session_active && samples < 8 && (
                  <p className="robot-help">
                    Solving needs at least 8 samples, spanning 50 mm of travel
                    and 20° of rotation.
                  </p>
                )}
                <SolveReadout title="Last solve" solve={vision.last_solve} />
                {vision.hand_eye ? (
                  <SolveReadout title="Active calibration"
                                solve={vision.hand_eye} />
                ) : (
                  <p className="robot-help">
                    No accepted calibration yet — marker poses fall back to the
                    URDF TF chain.
                  </p>
                )}
              </>
            )}
          </Card>

          <Card title="Observation poses">
            <p className="robot-help">
              Saves the arm's current configuration under a name so a scan can
              be repeated exactly. Saving reads the current TF and works in
              teach mode; replaying one moves the arm and needs the movement
              interlock above.
            </p>
            <div className="robot-goal-fields robot-goal-fields--three">
              <Field label="Name" value={observationName} maxLength="64"
                     onChange={(event) => setObservationName(event.target.value)} />
              <Field label="Marker ID (optional)" type="number" min="0" step="1"
                     value={observationMarkerId}
                     onChange={(event) => setObservationMarkerId(event.target.value)} />
              <RoleSelect label="Role" value={observationRole}
                          onChange={setObservationRole} />
            </div>
            <div className="robot-form__actions">
              <Button variant="primary" disabled={visionDisabled}
                      onClick={saveObservation}>
                Save current pose
              </Button>
            </div>
            {observations.length > 0 ? (
              <div className="robot-observation-list">
                {observations.map((item) => (
                  <div key={item.name} className="robot-observation">
                    <div>
                      <strong>{item.name}</strong>
                      <span className="robot-observation__meta">
                        {item.role}
                        {item.marker_id == null ? "" : ` · ArUco ${item.marker_id}`}
                      </span>
                    </div>
                    <Button size="sm" disabled={goalDisabled}
                            onClick={goal("goto_observation", { name: item.name })}>
                      Go
                    </Button>
                  </div>
                ))}
              </div>
            ) : (
              <p className="robot-help">No observation poses saved yet.</p>
            )}
          </Card>

          <Card title="Marker roles">
            <p className="robot-help">
              Records what a marker is for, once the camera has actually
              measured it. An estimated marker is refused: confirming one would
              record a guess as ground truth.
            </p>
            <div className="robot-goal-fields">
              <Field label="Marker ID" type="number" min="0" step="1"
                     value={markerId}
                     onChange={(event) => setMarkerId(event.target.value)} />
              <RoleSelect label="Role" value={confirmRole}
                          onChange={setConfirmRole} />
            </div>
            <div className="robot-form__actions">
              <Button variant="primary" disabled={visionDisabled}
                      onClick={goal("confirm_marker", {
                        marker_id: marker, role: confirmRole,
                      })}>
                Confirm marker {Number.isFinite(marker) ? marker : ""}
              </Button>
            </div>
            {Object.keys(markerRoles).length > 0 ? (
              <div className="robot-readout">
                {Object.entries(markerRoles).map(([id, role]) => (
                  <div key={id}>
                    <span>ArUco {id}</span>
                    <strong>{role}</strong>
                  </div>
                ))}
              </div>
            ) : (
              <p className="robot-help">No markers confirmed yet.</p>
            )}
          </Card>
        </div>
      </Section>
    </PageFrame>
  );
}
