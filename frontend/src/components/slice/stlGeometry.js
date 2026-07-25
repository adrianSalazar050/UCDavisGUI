// Pure reorientation math for the STL viewer. No three.js import -- it takes
// plain numbers so it unit-tests with vitest, the same discipline that makes
// detection/roiGeometry.js the one tested frontend module. three.js does the
// rendering and the actual mesh transform (stlBake.js / StlViewer.jsx); the
// decisions -- which rotation, where to drop, does it fit -- live here.

export const IDENTITY = { x: 0, y: 0, z: 0 }; // degrees per axis

// Add `degrees` about `axis` to a rotation, normalized to [0, 360).
export function addRotation(rot, axis, degrees) {
  const next = { ...rot };
  next[axis] = (((rot[axis] + degrees) % 360) + 360) % 360;
  return next;
}

export function isIdentity(rot) {
  return rot.x === 0 && rot.y === 0 && rot.z === 0;
}

// Translation that centers a bounding box in X/Y and rests its lowest point on
// the plate (z = 0). bbox = {min:{x,y,z}, max:{x,y,z}} in world space AFTER the
// rotation is applied (three.js recomputes it; this just reads it).
export function dropTranslation(bbox) {
  const cx = (bbox.min.x + bbox.max.x) / 2;
  const cy = (bbox.min.y + bbox.max.y) / 2;
  return { x: -cx, y: -cy, z: -bbox.min.z };
}

// Does the oriented footprint exceed the bed? bed may be null (unknown) -- then
// never block, because an unknown bed must not stop a slice (spec 3.8).
export function exceedsPlate(bbox, bed) {
  if (!bed) return false;
  const w = bbox.max.x - bbox.min.x;
  const d = bbox.max.y - bbox.min.y;
  return w > bed.x || d > bed.y;
}

export function degToRad(d) {
  return (d * Math.PI) / 180;
}
