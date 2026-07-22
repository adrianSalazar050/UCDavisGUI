import { describe, expect, it } from "vitest";

import {
  HANDLES, MIN_SIZE, applyHandleDrag, clampRoi, pctToRoi, roiToPct,
} from "./roiGeometry.js";

const A1 = [0.08, 0.32, 0.88, 0.68];   // the measured A1 bed box

describe("clampRoi", () => {
  it("leaves a valid box alone", () => {
    expect(clampRoi(A1)).toEqual(A1);
  });

  it("slides a box that overhangs the right edge back inside", () => {
    const [x, , w] = clampRoi([0.9, 0.1, 0.5, 0.2]);
    expect(x + w).toBeCloseTo(1);
  });

  it("shrinks a box wider than the frame instead of letting it overhang", () => {
    // Position-then-size would pin this at x=0 and leave w=1.5 hanging out.
    expect(clampRoi([-0.5, 0, 1.5, 0.5])).toEqual([0, 0, 1, 0.5]);
  });

  it("enforces the minimum size", () => {
    const [, , w, h] = clampRoi([0.5, 0.5, 0.001, 0]);
    expect(w).toBe(MIN_SIZE);
    expect(h).toBe(MIN_SIZE);
  });

  it("never returns a box outside the frame", () => {
    for (const roi of [[-1, -1, 0.2, 0.2], [2, 2, 0.2, 0.2], [0, 0, 9, 9]]) {
      const [x, y, w, h] = clampRoi(roi);
      expect(x).toBeGreaterThanOrEqual(0);
      expect(y).toBeGreaterThanOrEqual(0);
      expect(x + w).toBeLessThanOrEqual(1 + 1e-9);
      expect(y + h).toBeLessThanOrEqual(1 + 1e-9);
    }
  });
});

describe("applyHandleDrag", () => {
  it("move translates without resizing", () => {
    const [x, y, w, h] = applyHandleDrag(A1, "move", 0.02, -0.05);
    expect(w).toBeCloseTo(A1[2]);
    expect(h).toBeCloseTo(A1[3]);
    expect(x).toBeCloseTo(0.10);
    expect(y).toBeCloseTo(0.27);
  });

  it("move clamps at the frame edge instead of leaving it", () => {
    const [x, , w] = applyHandleDrag(A1, "move", 5, 0);
    expect(x + w).toBeCloseTo(1);
  });

  it("dragging the west handle keeps the east edge fixed", () => {
    const right = A1[0] + A1[2];
    const [x, , w] = applyHandleDrag(A1, "w", 0.05, 0);
    expect(x).toBeCloseTo(0.13);
    expect(x + w).toBeCloseTo(right);
  });

  it("dragging the south handle keeps the north edge fixed", () => {
    const [, y, , h] = applyHandleDrag(A1, "s", 0, -0.10);
    expect(y).toBeCloseTo(A1[1]);
    expect(h).toBeCloseTo(0.58);
  });

  it("a corner handle moves exactly its two edges", () => {
    const [x, y, w, h] = applyHandleDrag([0.2, 0.2, 0.5, 0.5], "ne", 0.1, 0.1);
    expect(x).toBeCloseTo(0.2);          // west edge untouched
    expect(y).toBeCloseTo(0.3);          // north dragged down
    expect(x + w).toBeCloseTo(0.8);      // east dragged right
    expect(y + h).toBeCloseTo(0.7);      // south untouched
  });

  it("collapses to MIN_SIZE instead of inverting when dragged past the far edge", () => {
    // The bug this guards: without the clamp, w goes negative and the box
    // renders inside-out.
    const [x, , w] = applyHandleDrag([0.2, 0.2, 0.5, 0.5], "w", 10, 0);
    expect(w).toBeCloseTo(MIN_SIZE);
    expect(x).toBeCloseTo(0.7 - MIN_SIZE);
    expect(w).toBeGreaterThan(0);
  });

  it("is computed from the start box, so a drag out and back returns home", () => {
    // Accumulating deltas per-mousemove would drift once clamping engaged.
    expect(applyHandleDrag(A1, "move", 5, 5)).not.toEqual(A1);
    expect(applyHandleDrag(A1, "move", 0, 0)).toEqual(A1);
  });

  it("ignores an unknown handle rather than corrupting the box", () => {
    expect(applyHandleDrag(A1, "nope", 0.5, 0.5)).toEqual(A1);
  });

  it("every handle keeps the box inside the frame for a large drag", () => {
    for (const handle of [...HANDLES, "move"]) {
      for (const [dx, dy] of [[9, 9], [-9, -9], [9, -9], [-9, 9]]) {
        const [x, y, w, h] = applyHandleDrag(A1, handle, dx, dy);
        expect(x).toBeGreaterThanOrEqual(-1e-9);
        expect(y).toBeGreaterThanOrEqual(-1e-9);
        expect(x + w).toBeLessThanOrEqual(1 + 1e-9);
        expect(y + h).toBeLessThanOrEqual(1 + 1e-9);
        expect(w).toBeGreaterThanOrEqual(MIN_SIZE - 1e-9);
        expect(h).toBeGreaterThanOrEqual(MIN_SIZE - 1e-9);
      }
    }
  });
});

describe("percent conversion", () => {
  it("round-trips", () => {
    expect(pctToRoi(roiToPct(A1, []))).toEqual(A1);
  });

  it("falls back when there is no roi", () => {
    expect(roiToPct(null, ["8", "32", "88", "68"]))
      .toEqual(["8", "32", "88", "68"]);
  });

  it("returns null for non-numeric input rather than NaN", () => {
    expect(pctToRoi(["8", "abc", "88", "68"])).toBeNull();
  });
});
