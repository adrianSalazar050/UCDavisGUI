import { useEffect, useMemo, useState } from "react";
import { fetchCellLayout, saveCellLayout } from "../api/cell.js";
import Button from "../components/ui/Button.jsx";
import Field from "../components/ui/Field.jsx";
import PageFrame from "../components/ui/PageFrame.jsx";
import Section from "../components/ui/Section.jsx";

const COLORS = {
  allowed: "#15803d", keepout: "#dc2626", restricted: "#ca8a04",
  fixture: "#2563eb", observation: "#7c3aed",
};
const emptyZone = (index) => ({
  id: `zone-${index + 1}`, name: "New zone", kind: "keepout",
  points: [[-0.05, -0.05], [-0.05, 0.05], [0.05, 0.05], [0.05, -0.05]],
  z_min: 0.025, z_max: 0.20, notes: "",
});
const numeric = (value) => value === "" ? "" : Number(value);

function rectangleBetween(first, second) {
  const xMin = Math.min(first[0], second[0]); const xMax = Math.max(first[0], second[0]);
  const yMin = Math.min(first[1], second[1]); const yMax = Math.max(first[1], second[1]);
  return [[xMin, yMin], [xMin, yMax], [xMax, yMax], [xMax, yMin]];
}

function boundsFor(points) {
  return { x_min: Math.min(...points.map((point) => point[0])), x_max: Math.max(...points.map((point) => point[0])),
    y_min: Math.min(...points.map((point) => point[1])), y_max: Math.max(...points.map((point) => point[1])) };
}

function TopView({ layout, selected, zSlice, onDrawRectangle }) {
  const [start, setStart] = useState(null);
  const [preview, setPreview] = useState(null);
  const { workspace, zones } = layout;
  const xRange = workspace.x_max - workspace.x_min;
  const yRange = workspace.y_max - workspace.y_min;
  const project = ([x, y]) => [
    ((y - workspace.y_min) / yRange) * 600,
    ((x - workspace.x_min) / xRange) * 440,
  ];
  const pointAt = (event) => {
    const box = event.currentTarget.getBoundingClientRect();
    const x = workspace.x_min + ((event.clientY - box.top) / box.height) * xRange;
    const y = workspace.y_min + ((event.clientX - box.left) / box.width) * yRange;
    return [Math.round(x * 1000) / 1000, Math.round(y * 1000) / 1000];
  };
  const pointerDown = (event) => {
    if (selected < 0) return;
    event.currentTarget.setPointerCapture(event.pointerId);
    const point = pointAt(event);
    setStart(point); setPreview(point);
  };
  const pointerMove = (event) => {
    if (start) setPreview(pointAt(event));
  };
  const pointerUp = (event) => {
    if (!start) return;
    onDrawRectangle(rectangleBetween(start, pointAt(event)));
    setStart(null); setPreview(null);
  };
  return <svg className="cell-layout__map" viewBox="0 0 600 440" role="img"
    aria-label="Top view of cell layout, X points down and Y points right"
    onPointerDown={pointerDown} onPointerMove={pointerMove} onPointerUp={pointerUp}>
    <rect x="0" y="0" width="600" height="440" className="cell-layout__floor" />
    {zones.map((zone, i) => {
      const points = zone.points.map(project).map((p) => p.join(",")).join(" ");
      const first = project(zone.points[0]);
      const atSlice = zone.z_min <= zSlice && zSlice <= zone.z_max;
      return <g key={`${zone.id}-${i}`}>
        <polygon points={points} fill={COLORS[zone.kind]} fillOpacity={atSlice || i === selected ? "0.20" : "0.035"}
          stroke={COLORS[zone.kind]} strokeOpacity={atSlice || i === selected ? "1" : "0.18"} strokeWidth={i === selected ? 4 : 2} />
        <text x={first[0] + 6} y={first[1] + 17} className="cell-layout__label">{zone.name}</text>
      </g>;
    })}
    {start && preview && (() => {
      const points = rectangleBetween(start, preview).map(project).map((p) => p.join(",")).join(" ");
      return <polygon points={points} className="cell-layout__preview" />;
    })()}
    {workspace.x_min <= 0 && workspace.x_max >= 0 && workspace.y_min <= 0 && workspace.y_max >= 0 && (() => {
      const [x, y] = project([0, 0]);
      return <g><circle cx={x} cy={y} r="5" className="cell-layout__origin" /><text x={x + 8} y={y - 8} className="cell-layout__label">link_base</text></g>;
    })()}
    <text x="12" y="24" className="cell-layout__axis">−X ↑ toward robot</text>
    <text x="12" y="426" className="cell-layout__axis">+X ↓ toward operator</text>
    <text x="470" y="24" className="cell-layout__axis">+Y → printer</text>
    <text x="390" y="426" className="cell-layout__axis">Z slice: {Number(zSlice).toFixed(3)} m</text>
  </svg>;
}

function SideView({ workspace, zone }) {
  const yRange = workspace.y_max - workspace.y_min;
  const zRange = workspace.z_max - workspace.z_min;
  const projectY = (y) => ((y - workspace.y_min) / yRange) * 600;
  const projectZ = (z) => 260 - ((z - workspace.z_min) / zRange) * 240;
  if (!zone) return <div className="cell-layout__side-empty">Select a zone to inspect its Y–Z envelope.</div>;
  const ys = zone.points.map((point) => point[1]);
  const yMin = Math.min(...ys); const yMax = Math.max(...ys);
  return <svg className="cell-layout__side" viewBox="0 0 600 280" role="img" aria-label="Side view showing Y and Z extent">
    <rect x="0" y="20" width="600" height="240" className="cell-layout__floor" />
    <rect x={projectY(yMin)} y={projectZ(zone.z_max)} width={Math.max(3, projectY(yMax) - projectY(yMin))}
      height={Math.max(3, projectZ(zone.z_min) - projectZ(zone.z_max))} fill={COLORS[zone.kind]} fillOpacity="0.25" stroke={COLORS[zone.kind]} strokeWidth="2" />
    <text x="10" y="18" className="cell-layout__axis">+Z ↑</text><text x="488" y="274" className="cell-layout__axis">+Y →</text>
  </svg>;
}

export default function CellLayoutSafety() {
  const [layout, setLayout] = useState(null);
  const [selected, setSelected] = useState(-1);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const [newKind, setNewKind] = useState("keepout");
  const [zSlice, setZSlice] = useState(0.20);
  useEffect(() => { fetchCellLayout().then((data) => { setLayout(data); setZSlice((data.workspace.z_min + data.workspace.z_max) / 2); }).catch((err) => setError(err.message)); }, []);
  const zone = selected >= 0 ? layout?.zones[selected] : null;
  const updateWorkspace = (key, value) => setLayout((current) => ({ ...current,
    workspace: { ...current.workspace, [key]: numeric(value) } }));
  const updateZone = (patch) => setLayout((current) => ({ ...current,
    zones: current.zones.map((item, index) => index === selected ? { ...item, ...patch } : item) }));
  const updateBounds = (key, value) => {
    if (!zone) return;
    const bounds = { ...boundsFor(zone.points), [key]: numeric(value) };
    updateZone({ points: rectangleBetween([bounds.x_min, bounds.y_min], [bounds.x_max, bounds.y_max]) });
  };
  const save = async () => {
    setSaving(true); setError(""); setNotice("");
    try { const saved = await saveCellLayout(layout); setLayout(saved); setNotice("Cell layout saved. It is configuration only until trajectory validation is enabled."); }
    catch (err) { setError(err.message); } finally { setSaving(false); }
  };
  const allZones = useMemo(() => layout?.zones ?? [], [layout]);
  if (!layout) return <PageFrame><div className="state-warn">Loading Cell Layout &amp; Safety… {error}</div></PageFrame>;
  return <PageFrame>
    <Section title="Coordinate frame">
      <p className="robot-help">This layout is expressed in <code>link_base</code>. Confirmed physical convention: +X points toward the operator, +Y points toward the printer, and +Z points up.</p>
      <label className="robot-enable"><input type="checkbox" checked={layout.frame.axes_confirmed} onChange={(e) => setLayout((current) => ({ ...current, frame: { ...current.frame, axes_confirmed: e.target.checked } }))} /> I verified the physical X, Y and Z directions before using this layout.</label>
      <Field label="Frame notes"><textarea rows="2" value={layout.frame.notes} onChange={(e) => setLayout((current) => ({ ...current, frame: { ...current.frame, notes: e.target.value } }))} /></Field>
    </Section>
    <Section title="Global workspace envelope">
      <p className="robot-help">These are editable planning limits in metres, not robot hardware limits. Keep them conservative. Changing them does not yet authorize a trajectory.</p>
      <div className="robot-goal-fields robot-goal-fields--three">{["x_min", "x_max", "y_min", "y_max", "z_min", "z_max"].map((key) => <Field key={key} label={`${key.replace("_", " ")} (m)`} type="number" step="0.001" value={layout.workspace[key]} onChange={(e) => updateWorkspace(key, e.target.value)} />)}</div>
    </Section>
    <Section title="Top view — X/Y">
      <div className="robot-goal-fields"><Field label="View height / Z slice (m)" type="number" min={layout.workspace.z_min} max={layout.workspace.z_max} step="0.001" value={zSlice} onChange={(e) => setZSlice(numeric(e.target.value))} /><Button size="sm" onClick={() => zone && setZSlice((Number(zone.z_min) + Number(zone.z_max)) / 2)} disabled={!zone}>View selected zone height</Button></div>
      <p className="robot-help">Zones may overlap in X/Y when they use separate heights. At the selected Z slice, relevant zones are solid and the other layers are faded.</p>
      <p className="robot-help">Zones are rectangles only. Select a zone, then drag from corner P1 to opposite corner P2. The map follows the physical directions you verified: down is +X and right is +Y.</p>
      <div className="cell-layout__views"><TopView layout={layout} selected={selected} zSlice={zSlice} onDrawRectangle={(points) => { updateZone({ points }); setError(""); }} /><div className="cell-layout__legend">{Object.entries(COLORS).map(([kind, color]) => <span key={kind}><i style={{ background: color }} />{kind}</span>)}</div></div>
    </Section>
    <Section title="Zones and lateral height">
      <div className="cell-layout__workspace">
        <div className="cell-layout__zones"><Field label="New zone category"><select value={newKind} onChange={(e) => setNewKind(e.target.value)}>{Object.keys(COLORS).map((kind) => <option key={kind} value={kind}>{kind}</option>)}</select></Field><div className="robot-form__actions"><Button onClick={() => { const next = { ...emptyZone(allZones.length), kind: newKind, name: `New ${newKind} zone` }; setLayout((current) => ({ ...current, zones: [...current.zones, next] })); setSelected(allZones.length); }}>Add zone</Button></div>{allZones.map((item, index) => <button type="button" key={`${item.id}-${index}`} className={`cell-layout__zone${index === selected ? " cell-layout__zone--selected" : ""}`} onClick={() => setSelected(index)}><i style={{ background: COLORS[item.kind] }} />{item.name}<small>{item.kind} · Z {item.z_min}–{item.z_max} m</small></button>)}</div>
        <div>{zone ? <div className="robot-form"><div className="robot-goal-fields"><Field label="Zone ID" value={zone.id} onChange={(e) => updateZone({ id: e.target.value })} /><Field label="Name" value={zone.name} onChange={(e) => updateZone({ name: e.target.value })} /><Field label="Category / color"><select value={zone.kind} onChange={(e) => updateZone({ kind: e.target.value })}>{Object.keys(COLORS).map((kind) => <option key={kind} value={kind}>{kind}</option>)}</select></Field><Field label="X minimum (m)" type="number" step="0.001" value={boundsFor(zone.points).x_min} onChange={(e) => updateBounds("x_min", e.target.value)} /><Field label="X maximum (m)" type="number" step="0.001" value={boundsFor(zone.points).x_max} onChange={(e) => updateBounds("x_max", e.target.value)} /><Field label="Y minimum (m)" type="number" step="0.001" value={boundsFor(zone.points).y_min} onChange={(e) => updateBounds("y_min", e.target.value)} /><Field label="Y maximum (m)" type="number" step="0.001" value={boundsFor(zone.points).y_max} onChange={(e) => updateBounds("y_max", e.target.value)} /><Field label="Z minimum (m)" type="number" step="0.001" value={zone.z_min} onChange={(e) => updateZone({ z_min: numeric(e.target.value) })} /><Field label="Z maximum (m)" type="number" step="0.001" value={zone.z_max} onChange={(e) => updateZone({ z_max: numeric(e.target.value) })} /></div><p className="robot-help">Category controls the map color: allowed = green, keepout = red, restricted = amber, fixture = blue, observation = purple.</p><Field label="Zone notes"><textarea rows="2" value={zone.notes} onChange={(e) => updateZone({ notes: e.target.value })} /></Field><div className="robot-form__actions"><Button size="sm" variant="danger" onClick={() => { setLayout((current) => ({ ...current, zones: current.zones.filter((_, i) => i !== selected) })); setSelected(-1); }}>Delete zone</Button></div><p className="robot-help">Side view below is the selected rectangle’s Y–Z envelope. Use it to give a top-view area physical height.</p><SideView workspace={layout.workspace} zone={zone} /></div> : <div className="state-warn">Choose a category, add a zone, then drag a rectangle on the top view.</div>}</div>
      </div>
    </Section>
    {notice && <div className="state-ok robot-notice">{notice}</div>}{error && <div className="state-error robot-notice">{error}</div>}
    <div className="robot-form__actions"><Button variant="primary" busy={saving} onClick={save}>Save cell layout</Button></div>
  </PageFrame>;
}
