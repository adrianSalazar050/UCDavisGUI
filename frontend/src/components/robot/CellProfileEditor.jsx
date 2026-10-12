import { useEffect, useState } from "react";
import { captureAbsoluteTcpPose, capturePreviousTcpOffset, captureProfilePose, deleteCellProfile, fetchCellProfiles, runCellProfile, runCellProfileDryRun, saveCellProfile } from "../../api/cell.js";
import { sendRobotCommand } from "../../api/robot.js";
import Button from "../ui/Button.jsx";
import Field from "../ui/Field.jsx";

const emptyProfile = () => ({
  id: "", role: "printer", marker_id: "", purpose: "", risk_level: "medium",
  notes: "", requires_operator_confirmation: true,
  steps: [{ kind: "scan", label: "Confirm reference marker", distance_m: 0.15, timeout_s: 4 }],
});

const blankStep = (kind) => {
  if (kind === "pose") return { kind, label: "Marker-relative pose", reference: "marker", target_frame: "tcp", translation_frame: "marker", orientation_frame: "marker", offset: [0, 0, 0, 0, 0, 0], speed_scale: 0.15, acceleration_scale: 0.1 };
  if (kind === "gripper") return { kind, label: "Open gripper", action: "open", settle_s: 0.5 };
  if (kind === "hold") return { kind, label: "Hold position", duration_s: 0.5 };
  if (kind === "contact") return { kind, label: "Bounded fixture approach", duration_s: 0.5, direction: "-z", frame: "base", travel_m: 0.005, speed_mm_s: 3, physically_validated: false };
  return { kind: "scan", label: "Confirm reference marker", distance_m: 0.15, timeout_s: 4 };
};

const number = (value) => (value === "" ? "" : Number(value));

export default function CellProfileEditor() {
  const [profiles, setProfiles] = useState([]);
  const [profile, setProfile] = useState(emptyProfile);
  const [newKind, setNewKind] = useState("pose");
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const [capturing, setCapturing] = useState(null);
  const [capturingPrevious, setCapturingPrevious] = useState(null);
  const [capturingAbsolute, setCapturingAbsolute] = useState(null);
  const [changingTeachMode, setChangingTeachMode] = useState(false);
  const [dryRunArmed, setDryRunArmed] = useState(false);
  const [physicalRunArmed, setPhysicalRunArmed] = useState(false);
  const [runningProfile, setRunningProfile] = useState(null);

  const refresh = async () => {
    try { setProfiles((await fetchCellProfiles()).profiles ?? []); }
    catch (err) { setError(err.message); }
  };
  useEffect(() => { refresh(); }, []);
  const update = (key) => (event) => setProfile((p) => ({ ...p, [key]: event.target.value }));
  const updateStep = (index, patch) => setProfile((p) => ({ ...p,
    steps: p.steps.map((step, i) => i === index ? { ...step, ...patch } : step) }));
  const moveStep = (index, direction) => setProfile((p) => {
    const target = index + direction;
    if (target < 0 || target >= p.steps.length) return p;
    const steps = [...p.steps]; [steps[index], steps[target]] = [steps[target], steps[index]];
    return { ...p, steps };
  });
  const removeStep = (index) => setProfile((p) => ({ ...p, steps: p.steps.filter((_, i) => i !== index) }));
  const load = (item) => { setProfile({ ...item, marker_id: String(item.marker_id) }); setNotice(`Editing ${item.id} v${item.version}`); setError(""); };
  const save = async () => {
    setError(""); setNotice(""); setSaving(true);
    try {
      const saved = await saveCellProfile({ ...profile, marker_id: Number(profile.marker_id) });
      setProfile({ ...saved, marker_id: String(saved.marker_id) });
      setNotice(`${saved.id} saved as draft v${saved.version}`); await refresh();
    } catch (err) { setError(err.message); }
    finally { setSaving(false); }
  };
  const removeProfile = async (id) => {
    if (!window.confirm(`Delete profile “${id}”? This cannot be undone.`)) return;
    try { await deleteCellProfile(id); if (profile.id === id) setProfile(emptyProfile()); setNotice(`${id} deleted`); await refresh(); }
    catch (err) { setError(err.message); }
  };
  const capturePose = async (index) => {
    setError(""); setNotice("");
    if (profile.marker_id === "" || !Number.isInteger(Number(profile.marker_id))) {
      setError("Choose a whole-number reference ArUco ID before capturing a pose.");
      return;
    }
    setCapturing(index);
    try {
      const step = profile.steps[index];
      const captured = await captureProfilePose(
        profile.marker_id, step.target_frame ?? "tcp",
        step.translation_frame ?? "marker", step.orientation_frame ?? "marker");
      updateStep(index, { offset: [...captured.offset, ...captured.orientation] });
      setNotice(`Captured current ${captured.target_frame === "camera" ? "camera" : captured.tcp_frame} pose relative to marker ${captured.marker_id}. Review the values, then save the draft.`);
    } catch (err) { setError(err.message); }
    finally { setCapturing(null); }
  };
  const capturePrevious = async (index) => {
    setError(""); setNotice("");
    if (profile.marker_id === "" || !Number.isInteger(Number(profile.marker_id))) {
      setError("Choose a whole-number reference ArUco ID before capturing a previous-TCP offset.");
      return;
    }
    setCapturingPrevious(index);
    try {
      const captured = await capturePreviousTcpOffset(
        { ...profile, marker_id: Number(profile.marker_id) }, index);
      updateStep(index, { offset: [...captured.offset, ...captured.orientation] });
      setNotice(`Captured current TCP as an offset from step ${captured.previous_step}. The reference ArUco was used only to resolve that preceding target; review and save the draft.`);
    } catch (err) { setError(err.message); }
    finally { setCapturingPrevious(null); }
  };
  const captureAbsolute = async (index) => {
    setError(""); setNotice(""); setCapturingAbsolute(index);
    try {
      const captured = await captureAbsoluteTcpPose();
      updateStep(index, { offset: [...captured.position, ...captured.orientation] });
      setNotice(`Captured the current TCP target in ${captured.frame}. This pose does not depend on a visible ArUco; review and save the draft.`);
    } catch (err) { setError(err.message); }
    finally { setCapturingAbsolute(null); }
  };
  const setTeachMode = async (enabled) => {
    setError(""); setNotice(""); setChangingTeachMode(true);
    try {
      const command = await sendRobotCommand(enabled ? "teach_enable" : "teach_disable");
      setNotice(`${enabled ? "Teach mode" : "Robot motion mode"} request accepted · ${command.id.slice(0, 8)}.`);
    } catch (err) { setError(err.message); }
    finally { setChangingTeachMode(false); }
  };
  const dryRun = async (id) => {
    setError(""); setNotice(""); setRunningProfile(id);
    try {
      const command = await runCellProfileDryRun(id);
      setNotice(`Dry run for ${id} accepted · ${command.id.slice(0, 8)}. It runs scan, pose, and hold only; gripper and contact are skipped.`);
    } catch (err) { setError(err.message); }
    finally { setRunningProfile(null); }
  };
  const runPhysical = async (item) => {
    if (!window.confirm(`Run physical profile “${item.id}”? It may command CO0 and any validated contact step. Stay at the emergency stop.`)) return;
    setError(""); setNotice(""); setRunningProfile(item.id);
    try {
      const command = await runCellProfile(item.id);
      setNotice(`Physical run for ${item.id} accepted · ${command.id.slice(0, 8)}. Supervise it continuously; contact has no force feedback.`);
    } catch (err) { setError(err.message); }
    finally { setRunningProfile(null); }
  };

  return <div className="robot-form">
    <p className="robot-help">Profiles are marker-relative drafts. Validate each pose in MoveIt and run a supervised dry run first. A physical run can command the verified CO0 gripper. Contact is a slow bounded MoveIt approach, not force-controlled contact: it needs a separately validated fixture step and continuous supervision.</p>
    <div className="robot-goal-fields robot-goal-fields--three">
      <Field label="Profile ID" value={profile.id} onChange={update("id")} placeholder="printer_pickup_v1" />
      <Field label="Reference ArUco ID" type="number" min="0" value={profile.marker_id} onChange={update("marker_id")} />
      <Field label="Role"><select value={profile.role} onChange={update("role")}><option value="printer">Printer</option><option value="scrape">Scrape fixture</option><option value="box">Deposit box</option><option value="return">Return plate</option></select></Field>
      <Field label="Purpose" value={profile.purpose} onChange={update("purpose")} placeholder="Approach the plate pickup pose" />
      <Field label="Risk"><select value={profile.risk_level} onChange={update("risk_level")}><option value="low">Low</option><option value="medium">Medium</option><option value="high">High — review required</option></select></Field>
      <Field label="Operator confirmation"><select value={String(profile.requires_operator_confirmation)} onChange={(e) => setProfile((p) => ({ ...p, requires_operator_confirmation: e.target.value === "true" }))}><option value="true">Required</option><option value="false">Not required</option></select></Field>
    </div>
    <Field label="Setup / safety notes"><textarea rows="3" value={profile.notes} onChange={update("notes")} placeholder="Clearance assumptions, fixture, plate type, and required checks." /></Field>

    {profile.steps.map((step, index) => <div className="robot-form robot-notice" key={`${step.kind}-${index}`}>
      <div className="robot-status"><strong>{index + 1}. {step.kind}</strong><span><Button size="sm" disabled={index === 0} onClick={() => moveStep(index, -1)}>↑</Button> <Button size="sm" disabled={index === profile.steps.length - 1} onClick={() => moveStep(index, 1)}>↓</Button> <Button size="sm" disabled={profile.steps.length === 1} onClick={() => removeStep(index)}>Remove</Button></span></div>
      <Field label="Step label" value={step.label} onChange={(e) => updateStep(index, { label: e.target.value })} />
      {step.kind === "pose" && <>
        <div className="robot-goal-fields">
          <Field label="Pose reference"><select value={step.reference ?? "marker"} onChange={(e) => updateStep(index, e.target.value === "previous_tcp"
            ? { reference: "previous_tcp", label: "Previous-TCP offset", translation_frame: "base", orientation_frame: "tool", target_frame: "tcp" }
            : e.target.value === "base"
              ? { reference: "base", label: "Absolute link_base TCP pose", translation_frame: "base", orientation_frame: "base", target_frame: "tcp" }
              : { reference: "marker", label: "Marker-relative pose", target_frame: "tcp", translation_frame: "marker", orientation_frame: "marker" })}>
            <option value="marker">Relative to reference ArUco</option><option value="previous_tcp">Relative to previous TCP pose</option><option value="base">Absolute link_base TCP pose</option>
          </select></Field>
          {(step.reference ?? "marker") === "marker" && <>
            <Field label="Target point"><select value={step.target_frame ?? "tcp"} onChange={(e) => updateStep(index, { target_frame: e.target.value })}>
              <option value="tcp">TCP / end effector</option><option value="camera">Wrist camera (requires hand–eye)</option>
            </select></Field>
            <Field label="Translation axes"><select value={step.translation_frame ?? "marker"} onChange={(e) => updateStep(index, { translation_frame: e.target.value })}>
              <option value="marker">ArUco local axes</option><option value="base">link_base — robot axes</option>
            </select></Field>
            <Field label="Rotation axes"><select value={step.orientation_frame ?? "marker"} onChange={(e) => updateStep(index, { orientation_frame: e.target.value })}>
              <option value="marker">ArUco local axes</option><option value="base">link_base — robot axes</option>
            </select></Field>
          </>}
          {(step.reference ?? "marker") === "previous_tcp" && <>
            <Field label="Translation axes"><select value={step.translation_frame ?? "base"} onChange={(e) => updateStep(index, { translation_frame: e.target.value })}><option value="base">link_base — recommended for lift/retract</option><option value="tool">TCP/tool frame</option></select></Field>
            <Field label="Rotation axes"><select value={step.orientation_frame ?? "tool"} onChange={(e) => updateStep(index, { orientation_frame: e.target.value })}><option value="tool">TCP/tool frame</option><option value="base">link_base frame</option></select></Field>
          </>}
        </div>
        {(step.reference ?? "marker") === "marker" ? <>
          <div className="robot-form__actions"><Button size="sm" busy={capturing === index} onClick={() => capturePose(index)}>Capture current {(step.target_frame ?? "tcp") === "camera" ? "camera" : "TCP"} pose</Button><span className="robot-help">Reads the visible marker and current target point; it never moves the robot.</span></div>
          <p className="robot-help">For a horizontal 10 cm camera standoff in the robot Y direction: choose <strong>Wrist camera</strong>, <strong>link_base — robot axes</strong>, then set Y to +0.100 or −0.100 m for the safe side of the marker. X and Z are independent components, not a diagonal distance.</p>
        </> : (step.reference ?? "marker") === "previous_tcp" ? <><p className="robot-help">Increment from the preceding planned TCP pose. For lifting the plate, use link_base and a positive Z delta. Capture resolves the preceding target from the live reference ArUco, so keep that marker visible while capturing; the profile may later move without seeing it.</p><div className="robot-form__actions"><Button size="sm" busy={capturingPrevious === index} onClick={() => capturePrevious(index)}>Capture current TCP offset</Button><Button size="sm" busy={changingTeachMode} onClick={() => setTeachMode(true)}>Enter teach mode</Button><Button size="sm" busy={changingTeachMode} onClick={() => setTeachMode(false)}>Exit teach mode</Button></div></> : <><p className="robot-help">A fixed TCP target in link_base. It never needs an ArUco or camera at capture/run time; use it only for a physically fixed, measured cell layout.</p><div className="robot-form__actions"><Button size="sm" busy={capturingAbsolute === index} onClick={() => captureAbsolute(index)}>Capture current absolute TCP pose</Button><Button size="sm" busy={changingTeachMode} onClick={() => setTeachMode(true)}>Enter teach mode</Button><Button size="sm" busy={changingTeachMode} onClick={() => setTeachMode(false)}>Exit teach mode</Button></div></>}
        <div className="robot-vector">{[((step.reference ?? "marker") === "marker" ? `X from ArUco (${(step.translation_frame ?? "marker") === "base" ? "link_base" : "ArUco"} axes, m)` : (step.reference ?? "marker") === "base" ? "X in link_base (m)" : "ΔX m"), ((step.reference ?? "marker") === "marker" ? `Y from ArUco (${(step.translation_frame ?? "marker") === "base" ? "link_base" : "ArUco"} axes, m)` : (step.reference ?? "marker") === "base" ? "Y in link_base (m)" : "ΔY m"), ((step.reference ?? "marker") === "marker" ? `Z from ArUco (${(step.translation_frame ?? "marker") === "base" ? "link_base" : "ArUco"} axes, m)` : (step.reference ?? "marker") === "base" ? "Z in link_base (m)" : "ΔZ m"), (step.reference ?? "marker") === "base" ? "Roll in link_base (rad)" : "ΔRoll rad", (step.reference ?? "marker") === "base" ? "Pitch in link_base (rad)" : "ΔPitch rad", (step.reference ?? "marker") === "base" ? "Yaw in link_base (rad)" : "ΔYaw rad"].map((label, axis) => <Field key={label} label={label} type="number" step="0.001" value={step.offset[axis]} onChange={(e) => updateStep(index, { offset: step.offset.map((value, i) => i === axis ? number(e.target.value) : value) })} />)}</div>
        <div className="robot-goal-fields"><Field label="Speed scale" type="number" min="0.01" max="1" step="0.01" value={step.speed_scale} onChange={(e) => updateStep(index, { speed_scale: number(e.target.value) })} /><Field label="Acceleration scale" type="number" min="0.01" max="1" step="0.01" value={step.acceleration_scale} onChange={(e) => updateStep(index, { acceleration_scale: number(e.target.value) })} /></div>
      </>}
      {step.kind === "scan" && <><p className="robot-help">Profile scan verifies that the reference ArUco is visible from the current pose. It does not move the robot. Put a marker-relative <code>pose</code> before it to define the viewing location. The standoff is defined by that pose and must be physically measured for this fixture; it is not constrained by a universal ArUco-distance rule.</p><Field label="Detection timeout (s)" type="number" min="0.5" max="30" step="0.5" value={step.timeout_s} onChange={(e) => updateStep(index, { timeout_s: number(e.target.value) })} /></>}
      {step.kind === "gripper" && <div className="robot-goal-fields"><Field label="CO0 action"><select value={step.action} onChange={(e) => updateStep(index, { action: e.target.value })}><option value="open">Open — LOW</option><option value="close">Close — HIGH</option></select></Field><Field label="Settle time (s)" type="number" min="0" max="10" step="0.1" value={step.settle_s} onChange={(e) => updateStep(index, { settle_s: number(e.target.value) })} /></div>}
      {step.kind === "hold" && <Field label="Duration (s)" type="number" min="0" max="60" step="0.1" value={step.duration_s} onChange={(e) => updateStep(index, { duration_s: number(e.target.value) })} />}
      {step.kind === "contact" && <><div className="robot-goal-fields robot-goal-fields--three"><Field label="Maximum travel (m)" type="number" min="0.001" max="0.01" step="0.001" value={step.travel_m} onChange={(e) => updateStep(index, { travel_m: number(e.target.value), physically_validated: false })} /><Field label="Maximum speed (mm/s)" type="number" min="1" max="5" step="1" value={step.speed_mm_s} onChange={(e) => updateStep(index, { speed_mm_s: number(e.target.value), physically_validated: false })} /><Field label="Hold after approach (s)" type="number" min="0" max="60" step="0.1" value={step.duration_s} onChange={(e) => updateStep(index, { duration_s: number(e.target.value), physically_validated: false })} /></div><div className="robot-goal-fields"><Field label="Direction frame"><select value={step.frame ?? "base"} onChange={(e) => updateStep(index, { frame: e.target.value, physically_validated: false })}><option value="base">link_base — recommended</option><option value="tool">TCP/tool axes</option></select></Field><Field label="Approach direction"><select value={step.direction} onChange={(e) => updateStep(index, { direction: e.target.value, physically_validated: false })}>{["+x", "-x", "+y", "-y", "+z", "-z"].map((axis) => <option key={axis} value={axis}>{axis}</option>)}</select></Field></div><label className="robot-enable"><input type="checkbox" checked={Boolean(step.physically_validated)} onChange={(e) => updateStep(index, { physically_validated: e.target.checked })} /> I physically validated this exact fixture, direction, travel, speed, and plate configuration.</label><div className="state-warn">Physical run limit: 10 mm and 5 mm/s. This is a bounded MoveIt approach only; there is no force, contact, or grasp feedback.</div></>}
    </div>)}
    <div className="robot-form__actions"><select value={newKind} onChange={(e) => setNewKind(e.target.value)}><option value="pose">Pose</option><option value="scan">Scan</option><option value="gripper">Gripper</option><option value="hold">Hold</option><option value="contact">Bounded contact approach</option></select><Button onClick={() => setProfile((p) => ({ ...p, steps: [...p.steps, blankStep(newKind)] }))}>Add step</Button><Button variant="primary" busy={saving} onClick={save}>Save draft</Button><Button onClick={() => { setProfile(emptyProfile()); setNotice("New draft"); }}>New profile</Button></div>
    {notice && <div className="state-ok robot-notice">{notice}</div>}{error && <div className="state-error robot-notice">{error}</div>}
    {profiles.length > 0 && <><label className="robot-enable"><input type="checkbox" checked={dryRunArmed} onChange={(e) => setDryRunArmed(e.target.checked)} /> I have verified the workspace is clear; run profiles in dry-run mode only.</label><p className="robot-help">Dry run validates all configured pose targets before moving. It executes scan, pose, and hold only. Gripper output and contact are skipped.</p><label className="robot-enable"><input type="checkbox" checked={physicalRunArmed} onChange={(e) => setPhysicalRunArmed(e.target.checked)} /> I verified the workspace is clear, fixture is secured, and I will supervise the full physical run with emergency stop accessible.</label><div className="robot-readout">{profiles.map((item) => <div key={item.id}><span>{item.role} · marker {item.marker_id}</span><strong>{item.id} · v{item.version}</strong><small>{item.risk_level} risk · {item.steps.length} steps</small><div className="robot-form__actions"><Button size="sm" onClick={() => load(item)}>Edit</Button><Button size="sm" onClick={() => setProfile({ ...item, id: `${item.id}_copy`, marker_id: String(item.marker_id) })}>Duplicate</Button><Button size="sm" variant="primary" busy={runningProfile === item.id} disabled={!dryRunArmed} onClick={() => dryRun(item.id)}>Validate & dry run</Button><Button size="sm" variant="danger" busy={runningProfile === item.id} disabled={!physicalRunArmed} onClick={() => runPhysical(item)}>Run physical profile</Button><Button size="sm" variant="danger" onClick={() => removeProfile(item.id)}>Delete</Button></div></div>)}</div></>}
  </div>;
}
