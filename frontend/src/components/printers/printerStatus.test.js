import { describe, expect, it } from "vitest";
import { fleetSummary, moveFocus, printerPercent, printerProgress, printerTone }
  from "./printerStatus.js";

describe("printerTone", () => {
  it("shows what a connected printer is doing", () => {
    expect(printerTone({ connection: "ok", gcode_state: "RUNNING" }))
      .toEqual({ tone: "ok", label: "RUNNING" });
  });

  it("says Connected when a live printer hasn't reported a state yet", () => {
    // "" is what arrives before the push_all round trip completes; an empty
    // pill would read as a broken UI.
    expect(printerTone({ connection: "ok", gcode_state: "" }).label)
      .toBe("Connected");
    expect(printerTone({ connection: "ok" }).label).toBe("Connected");
  });

  it("lets connection outrank a stale gcode_state", () => {
    // The whole point: a dropped printer's last RUNNING must not read as
    // "printing" just because that was the final report.
    expect(printerTone({ connection: "disconnected", gcode_state: "RUNNING" }))
      .toEqual({ tone: "danger", label: "Offline" });
    expect(printerTone({ connection: "stale", gcode_state: "RUNNING" }))
      .toEqual({ tone: "warn", label: "Stale" });
  });

  it("handles no printer at all", () => {
    expect(printerTone(null).tone).toBe("warn");
  });
});

describe("printerProgress", () => {
  it("combines layers and percent", () => {
    expect(printerProgress({ layer_num: 12, total_layer_num: 100, mc_percent: 34 }))
      .toBe("layer 12 / 100 · 34%");
  });

  it("copes with an unknown total", () => {
    expect(printerProgress({ layer_num: 3 })).toBe("layer 3 / ?");
  });

  it("is null when nothing is printing", () => {
    expect(printerProgress({})).toBe(null);
    expect(printerProgress(null)).toBe(null);
  });

  it("keeps layer 0", () => {
    // A falsy-but-real value: layer_num 0 is the first layer, not "no data".
    expect(printerProgress({ layer_num: 0, total_layer_num: 50 }))
      .toBe("layer 0 / 50");
  });
});

describe("printerPercent", () => {
  it("clamps a value that came off the wire wrong", () => {
    expect(printerPercent({ mc_percent: 140 })).toBe(100);
    expect(printerPercent({ mc_percent: -5 })).toBe(0);
  });

  it("is null when there is no percent", () => {
    expect(printerPercent({})).toBe(null);
    expect(printerPercent({ mc_percent: null })).toBe(null);
    expect(printerPercent(null)).toBe(null);
  });

  it("keeps 0 distinct from null", () => {
    expect(printerPercent({ mc_percent: 0 })).toBe(0);
  });
});

describe("fleetSummary", () => {
  it("counts printers and how many are online", () => {
    expect(fleetSummary([
      { connection: "ok" }, { connection: "stale" }, { connection: "ok" },
    ])).toBe("3 printers · 2 online");
  });

  it("stays singular for one", () => {
    expect(fleetSummary([{ connection: "ok" }])).toBe("1 printer · 1 online");
  });

  it("handles an empty fleet", () => {
    expect(fleetSummary([])).toBe("No printers");
    expect(fleetSummary(undefined)).toBe("No printers");
  });
});

describe("moveFocus", () => {
  it("walks down and wraps", () => {
    expect(moveFocus(0, "ArrowDown", 3)).toBe(1);
    expect(moveFocus(2, "ArrowDown", 3)).toBe(0);
  });

  it("walks up and wraps", () => {
    expect(moveFocus(1, "ArrowUp", 3)).toBe(0);
    expect(moveFocus(0, "ArrowUp", 3)).toBe(2);
  });

  it("jumps to the ends", () => {
    expect(moveFocus(1, "Home", 3)).toBe(0);
    expect(moveFocus(1, "End", 3)).toBe(2);
  });

  it("enters the list from nowhere", () => {
    expect(moveFocus(null, "ArrowDown", 3)).toBe(0);
    expect(moveFocus(null, "ArrowUp", 3)).toBe(2);
  });

  it("returns null for keys it does not handle, so they stay unswallowed", () => {
    expect(moveFocus(0, "a", 3)).toBe(null);
    expect(moveFocus(0, "Enter", 3)).toBe(null);
    expect(moveFocus(0, "Tab", 3)).toBe(null);
  });

  it("returns null for an empty list", () => {
    expect(moveFocus(null, "ArrowDown", 0)).toBe(null);
  });
});
