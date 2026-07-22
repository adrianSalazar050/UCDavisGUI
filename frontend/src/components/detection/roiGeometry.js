// Pure ROI geometry -- no DOM, no React, so it is the part that can be
// tested. Everything is FRACTIONS of the frame ([0,1]), never pixels: the
// camera is 1536x1080 on the A1 and 1680x1080 on the A1 mini, and the <img>
// is displayed at whatever width the layout gives it, so fractions are the
// only representation that survives all three.

// A box smaller than this is almost certainly an accident (a click that
// dragged two pixels), and a tiny ROI crops the failure out of view -- a
// silent false negative, which master.md section 4.1 calls out as the worst
// outcome here. So drags clamp against it rather than being allowed through.
export const MIN_SIZE = 0.05;

const clamp01 = (n) => Math.min(1, Math.max(0, n));

// Binary floating point can't hold 0.32 or 0.68 exactly, so a box that ends
// flush with the frame edge computes as 1 + 5e-17 and an unconditional
// `min(y, 1 - h)` would "correct" a perfectly valid box to 0.31999999999999995
// on every single call. Only move the box when it is out of bounds by more
// than representation noise.
const EPS = 1e-9;

/** Force [x, y, w, h] inside the frame, keeping w/h >= MIN_SIZE. */
export function clampRoi([x, y, w, h]) {
  // Size first, then position: a box wider than the frame must shrink, and
  // only then can it be slid back inside. Doing it the other way lets a
  // too-wide box pin to x=0 and still overhang the right edge.
  const cw = Math.min(1, Math.max(MIN_SIZE, w));
  const ch = Math.min(1, Math.max(MIN_SIZE, h));
  let cx = clamp01(x);
  let cy = clamp01(y);
  if (cx + cw > 1 + EPS) cx = clamp01(1 - cw);
  if (cy + ch > 1 + EPS) cy = clamp01(1 - ch);
  return [cx, cy, cw, ch];
}

// Which edges each handle moves. "nw" drags the north and west edges, "n"
// only the north, and "move" translates without resizing.
const HANDLE_EDGES = {
  nw: ["n", "w"], n: ["n"], ne: ["n", "e"],
  w: ["w"], e: ["e"],
  sw: ["s", "w"], s: ["s"], se: ["s", "e"],
};

export const HANDLES = Object.keys(HANDLE_EDGES);

/**
 * Apply a drag to one handle. `roi` is the box at drag START, (dx, dy) the
 * total movement since then as fractions. Returns a new clamped roi.
 *
 * Deliberately computed from the START box plus a total delta rather than
 * accumulated per-mousemove: accumulating drifts once clamping kicks in, so
 * the box would fail to follow the cursor back after you drag past an edge.
 */
export function applyHandleDrag(roi, handle, dx, dy) {
  const [x, y, w, h] = roi;
  if (handle === "move") return clampRoi([x + dx, y + dy, w, h]);

  const edges = HANDLE_EDGES[handle];
  if (!edges) return clampRoi(roi);

  // Work in edge coordinates -- moving the west edge changes both x and w,
  // and expressing that as left/right is what keeps the opposite edge fixed.
  let left = x, top = y, right = x + w, bottom = y + h;
  if (edges.includes("w")) left = Math.min(x + dx, right - MIN_SIZE);
  if (edges.includes("e")) right = Math.max(x + w + dx, left + MIN_SIZE);
  if (edges.includes("n")) top = Math.min(y + dy, bottom - MIN_SIZE);
  if (edges.includes("s")) bottom = Math.max(y + h + dy, top + MIN_SIZE);

  return clampRoi([left, top, right - left, bottom - top]);
}

/** [0.08, 0.32, ...] -> ["8", "32", ...] for the numeric inputs. */
export const roiToPct = (roi, fallback) =>
  roi ? roi.map((v) => String(Math.round(v * 100))) : fallback;

/** ["8","32","88","68"] -> [0.08, ...], or null if any entry isn't a number. */
export function pctToRoi(pct) {
  const nums = pct.map(Number);
  if (nums.some((n) => !Number.isFinite(n))) return null;
  return nums.map((n) => n / 100);
}
