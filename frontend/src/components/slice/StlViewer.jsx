import { useEffect, useRef } from "react";
import {
  Scene, PerspectiveCamera, WebGLRenderer, GridHelper, Mesh,
  MeshStandardMaterial, DirectionalLight, AmbientLight, Color,
} from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import { parseStl, rotationMatrix } from "./stlBake.js";
import { dropTranslation, exceedsPlate } from "./stlGeometry.js";

const DEFAULT_BED = { x: 256, y: 256 };

// A self-contained three.js viewport. Props:
//   arrayBuffer: the STL bytes (or null -> empty plate)
//   rotation: {x,y,z} degrees
//   bed: {x,y} mm or null (-> DEFAULT_BED, the caller shows a note)
//   onFit(fits:boolean): whether the oriented model fits the plate
// Z is treated as up (printer-bed convention): the mesh is dropped on Z and the
// grid is rotated into the XY plane. Rebuilds the mesh on arrayBuffer/rotation
// change; the renderer/camera persist across renders.
export default function StlViewer({ arrayBuffer, rotation, bed, onFit }) {
  const mountRef = useRef(null);
  const stateRef = useRef(null);

  // one-time scene + renderer + camera + controls
  useEffect(() => {
    const mount = mountRef.current;
    const w = mount.clientWidth || 480;
    const h = mount.clientHeight || 360;
    const scene = new Scene();
    scene.background = new Color(0x0e1116);
    const camera = new PerspectiveCamera(45, w / h, 1, 5000);
    camera.up.set(0, 0, 1); // Z-up
    camera.position.set(220, -260, 220);
    const renderer = new WebGLRenderer({ antialias: true });
    renderer.setSize(w, h);
    renderer.setPixelRatio(window.devicePixelRatio || 1);
    mount.appendChild(renderer.domElement);
    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    scene.add(new AmbientLight(0xffffff, 0.65));
    const dir = new DirectionalLight(0xffffff, 0.85);
    dir.position.set(1, -1, 2);
    scene.add(dir);
    const st = { renderer, scene, camera, controls, mesh: null, grid: null, raf: 0 };
    stateRef.current = st;
    const loop = () => {
      controls.update();
      renderer.render(scene, camera);
      st.raf = requestAnimationFrame(loop);
    };
    loop();
    const onResize = () => {
      const ww = mount.clientWidth || 480;
      const hh = mount.clientHeight || 360;
      camera.aspect = ww / hh;
      camera.updateProjectionMatrix();
      renderer.setSize(ww, hh);
    };
    window.addEventListener("resize", onResize);
    return () => {
      window.removeEventListener("resize", onResize);
      cancelAnimationFrame(st.raf);
      controls.dispose();
      renderer.dispose();
      if (renderer.domElement.parentNode) {
        renderer.domElement.parentNode.removeChild(renderer.domElement);
      }
    };
  }, []);

  // plate grid sized to the bed
  useEffect(() => {
    const st = stateRef.current;
    if (!st) return;
    if (st.grid) {
      st.scene.remove(st.grid);
      st.grid.geometry.dispose();
    }
    const b = bed || DEFAULT_BED;
    const size = Math.max(b.x, b.y);
    const divisions = Math.max(4, Math.round(size / 20));
    const grid = new GridHelper(size, divisions, 0x3a4150, 0x22262e);
    grid.rotation.x = Math.PI / 2; // GridHelper is XZ by default; rotate into XY
    st.grid = grid;
    st.scene.add(grid);
  }, [bed]);

  // (re)build the mesh on new bytes or rotation
  useEffect(() => {
    const st = stateRef.current;
    if (!st) return;
    if (st.mesh) {
      st.scene.remove(st.mesh);
      st.mesh.geometry.dispose();
      st.mesh.material.dispose();
      st.mesh = null;
    }
    if (!arrayBuffer) {
      onFit?.(true);
      return;
    }
    let geom;
    try {
      geom = parseStl(arrayBuffer);
    } catch {
      onFit?.(true);
      return;
    }
    geom.applyMatrix4(rotationMatrix(rotation));
    geom.computeVertexNormals();
    geom.computeBoundingBox();
    const bb = geom.boundingBox;
    const drop = dropTranslation({ min: bb.min, max: bb.max });
    geom.translate(drop.x, drop.y, drop.z);
    geom.computeBoundingBox();
    const mesh = new Mesh(
      geom,
      new MeshStandardMaterial({ color: 0x4c8bf5, metalness: 0.1, roughness: 0.7 }),
    );
    st.mesh = mesh;
    st.scene.add(mesh);
    onFit?.(!exceedsPlate({ min: geom.boundingBox.min, max: geom.boundingBox.max }, bed));
  }, [arrayBuffer, rotation, bed, onFit]);

  return <div ref={mountRef} className="stl-viewer" style={{ width: "100%", height: 360 }} />;
}
