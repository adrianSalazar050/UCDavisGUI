import { Euler, Matrix4, Mesh, MeshStandardMaterial } from "three";
import { STLLoader } from "three/examples/jsm/loaders/STLLoader.js";
import { STLExporter } from "three/examples/jsm/exporters/STLExporter.js";
import { degToRad } from "./stlGeometry.js";

// Parse an STL ArrayBuffer into a three.js BufferGeometry. Throws on a bad STL.
export function parseStl(arrayBuffer) {
  return new STLLoader().parse(arrayBuffer);
}

// The rotation matrix for a {x,y,z}-degrees orientation, Euler order XYZ. The
// SAME matrix the viewer applies, so what you see is what gets sliced.
export function rotationMatrix(rot) {
  return new Matrix4().makeRotationFromEuler(
    new Euler(degToRad(rot.x), degToRad(rot.y), degToRad(rot.z), "XYZ"));
}

// Apply a rotation to a geometry (baking it into the vertices) and export a
// BINARY STL as a Blob, ready to upload -- so the slicer receives an already-
// oriented mesh and the backend never changes.
export function bakeRotatedStl(arrayBuffer, rot) {
  const geom = parseStl(arrayBuffer);
  geom.applyMatrix4(rotationMatrix(rot));
  geom.computeVertexNormals();
  const mesh = new Mesh(geom, new MeshStandardMaterial());
  const data = new STLExporter().parse(mesh, { binary: true });
  return new Blob([data], { type: "model/stl" });
}
