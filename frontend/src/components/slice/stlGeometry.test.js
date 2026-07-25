import { describe, expect, it } from "vitest";
import { addRotation, dropTranslation, exceedsPlate, IDENTITY, isIdentity }
  from "./stlGeometry.js";

describe("addRotation", () => {
  it("adds degrees on an axis and wraps at 360", () => {
    expect(addRotation(IDENTITY, "x", 90)).toEqual({ x: 90, y: 0, z: 0 });
    expect(addRotation({ x: 300, y: 0, z: 0 }, "x", 90)).toEqual({ x: 30, y: 0, z: 0 });
  });
  it("handles negative wrap", () => {
    expect(addRotation(IDENTITY, "y", -90)).toEqual({ x: 0, y: 270, z: 0 });
  });
});

describe("isIdentity", () => {
  it("is true only for all-zero rotation", () => {
    expect(isIdentity(IDENTITY)).toBe(true);
    expect(isIdentity({ x: 0, y: 0, z: 0 })).toBe(true);
    expect(isIdentity({ x: 0, y: 90, z: 0 })).toBe(false);
  });
});

describe("dropTranslation", () => {
  it("centers X/Y and puts min-Z on the plate", () => {
    const bbox = { min: { x: 10, y: 20, z: 5 }, max: { x: 30, y: 60, z: 25 } };
    expect(dropTranslation(bbox)).toEqual({ x: -20, y: -40, z: -5 });
  });
});

describe("exceedsPlate", () => {
  const bed = { x: 256, y: 256 };
  it("false when the footprint fits", () => {
    const bbox = { min: { x: -50, y: -50, z: 0 }, max: { x: 50, y: 50, z: 40 } };
    expect(exceedsPlate(bbox, bed)).toBe(false);
  });
  it("true when the footprint is larger than the bed", () => {
    const bbox = { min: { x: -200, y: -10, z: 0 }, max: { x: 200, y: 10, z: 5 } };
    expect(exceedsPlate(bbox, bed)).toBe(true);
  });
  it("false when the bed is unknown (null) -- never hard-block", () => {
    const bbox = { min: { x: -999, y: -999, z: 0 }, max: { x: 999, y: 999, z: 1 } };
    expect(exceedsPlate(bbox, null)).toBe(false);
  });
});
