import { describe, expect, it } from "vitest";
import { formatDuration, pieceRollup, runOutcome } from "./runFormat.js";

describe("pieceRollup", () => {
  it("counts each status", () => {
    const pieces = [
      { status: "good" }, { status: "good" },
      { status: "scrap" }, { status: "pending_inspection" },
    ];
    expect(pieceRollup(pieces)).toEqual({
      total: 4, good: 2, scrap: 1, rework: 0, pending: 1,
    });
  });

  it("handles an empty plate", () => {
    expect(pieceRollup([])).toEqual({
      total: 0, good: 0, scrap: 0, rework: 0, pending: 0,
    });
  });

  it("tolerates a missing list", () => {
    expect(pieceRollup(undefined).total).toBe(0);
  });
});

describe("runOutcome", () => {
  it("labels an open run as running", () => {
    expect(runOutcome({ end_state: null })).toEqual({
      label: "Running", tone: "ok",
    });
  });

  it("distinguishes a monitor stop from a plain failure", () => {
    expect(runOutcome({ end_state: "STOPPED_BY_MONITOR" }).label)
      .toBe("Stopped by monitor");
    expect(runOutcome({ end_state: "FAILED" }).label).toBe("Failed");
  });

  it("falls back to the raw value for an unknown state", () => {
    expect(runOutcome({ end_state: "WEIRD" }).label).toBe("WEIRD");
  });
});

describe("formatDuration", () => {
  it("renders hours and minutes", () => {
    expect(formatDuration(3720)).toBe("1h 2m");
  });

  it("renders minutes alone under an hour", () => {
    expect(formatDuration(150)).toBe("2m");
  });

  it("returns a dash for nothing", () => {
    expect(formatDuration(null)).toBe("—");
    expect(formatDuration(0)).toBe("—");
  });
});
