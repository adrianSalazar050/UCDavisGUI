import { useEffect, useMemo, useRef, useState } from "react";

import {
  cancelRobotCommand,
  sendRobotCommand,
} from "../api/robot.js";
import Button from "../components/ui/Button.jsx";
import Card from "../components/ui/Card.jsx";
import Field from "../components/ui/Field.jsx";
import PageFrame from "../components/ui/PageFrame.jsx";
import Section from "../components/ui/Section.jsx";
import StatusPill from "../components/ui/StatusPill.jsx";


const DEFAULT_JOINTS_DEG = ["0", "-15", "20", "0", "0", "0"];
const EMPTY_POSE = ["", "", "", "", "", ""];
const RAD = Math.PI / 180;


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
  const jogDispatching = useRef(false);
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
    } catch (err) {
      setError(err.message);
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
      run("jog_pose", { axis, delta });
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
        setNotice(`jog_pose accepted · ${command.id.slice(0, 8)}`);
      })
      .catch((err) => setError(err.message))
      .finally(() => {
        jogDispatching.current = false;
        setSubmitting(false);
      });
  }, [busy, submitting, pendingJog]);

  const last = robot?.last_command;
  const pose = robot?.eef_pose?.xyz_rpy ?? [];

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
              <Button disabled={goalDisabled}
                      onClick={goal("gripper_open")}>Open gripper</Button>
              <Button disabled={goalDisabled}
                      onClick={goal("gripper_close")}>Close gripper</Button>
            </div>
            {robot?.sim && (
              <p className="robot-help">
                Gripper actuation is disabled by the automation package in simulation.
              </p>
            )}
          </Card>

          <Card title="Known markers">
            {robot?.markers && Object.keys(robot.markers).length > 0 ? (
              <div className="robot-marker-list">
                {Object.entries(robot.markers).map(([id, data]) => (
                  <div key={id}>
                    <strong>ArUco {id}</strong>
                    <span>{data?.estimated ? "Estimated" : "Detected"}</span>
                  </div>
                ))}
              </div>
            ) : (
              <p className="robot-help">No markers have been registered yet.</p>
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
    </PageFrame>
  );
}
