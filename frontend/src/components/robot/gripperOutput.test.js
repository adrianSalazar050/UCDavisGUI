import { describe, expect, it } from "vitest";
import { blockerText, gripperLabel, gripperOutputView }
  from "./gripperOutput.js";


const idle = { available: true, state: "idle" };
const cgpio = (extra = {}) => ({
  kind: "cgpio", ionum: 3, output_count: 8, command: null, sensed: false,
  disabled: false, ...extra,
});


describe("gripperOutputView", () => {
  it("shows the picker with no arm configured, but cannot apply", () => {
    // The user's own case: `python -m server --lan` with no --robot-mode.
    // The setting must still be discoverable on the page.
    const view = gripperOutputView(
      { robot: null, gripper: undefined, selection: "", busy: false });
    expect(view).not.toBeNull();
    expect(view.outputs).toEqual([0, 1, 2, 3, 4, 5, 6, 7]);
    expect(view.value).toBe("0");
    expect(view.current).toBeNull();
    expect(view.blocker).toBe("offline");
  });

  it("lets the server decide when telemetry carries no gripper block", () => {
    // An older backend sends no gripper key at all. Hiding the picker on a
    // missing key would hide a working output behind a telemetry gap; the
    // route answers 400/503 with a readable reason if it truly has none.
    const view = gripperOutputView(
      { robot: idle, gripper: undefined, selection: "", busy: false });
    expect(view.current).toBeNull();
    expect(view.blocker).toBeNull();
  });

  it("follows the live output until the operator picks another", () => {
    const untouched = gripperOutputView(
      { robot: idle, gripper: cgpio(), selection: "", busy: false });
    expect(untouched.value).toBe("3");
    expect(untouched.current).toBe(3);
    // Nothing to apply while the selection matches what is already set.
    expect(untouched.blocker).toBe("unchanged");

    const changed = gripperOutputView(
      { robot: idle, gripper: cgpio(), selection: "5", busy: false });
    expect(changed.value).toBe("5");
    expect(changed.blocker).toBeNull();
  });

  it("bounds the options by the controller's reported CO count", () => {
    const view = gripperOutputView({
      robot: idle, gripper: cgpio({ output_count: 4 }), selection: "",
      busy: false,
    });
    expect(view.outputs).toEqual([0, 1, 2, 3]);
  });

  it("hides the picker only for a gripper that positively has no output", () => {
    for (const kind of ["lite6_service", "moveit_action"]) {
      expect(gripperOutputView({
        robot: idle, gripper: { kind, command: null }, selection: "",
        busy: false,
      })).toBeNull();
    }
    // An uncommissioned xArm 6 reports kind null. That is "not set up yet",
    // not "has no pin" -- the picker stays, and the server explains if the
    // automation config still lacks a cgpio gripper.
    expect(gripperOutputView({
      robot: idle, gripper: { kind: null, disabled: true }, selection: "",
      busy: false,
    })).not.toBeNull();
  });

  it("refuses while the gripper is commanded closed", () => {
    // The old pin stays latched high after a switch; re-pointing mid-grip
    // would leave a live pin holding a plate nothing tracks any more.
    const view = gripperOutputView({
      robot: idle, gripper: cgpio({ command: "close" }), selection: "6",
      busy: false,
    });
    expect(view.blocker).toBe("closed");
  });

  it("waits for a running command and an unavailable arm", () => {
    expect(gripperOutputView({
      robot: { ...idle, state: "executing" }, gripper: cgpio(),
      selection: "6", busy: true,
    }).blocker).toBe("busy");
    expect(gripperOutputView({
      robot: { available: false, state: "error" }, gripper: cgpio(),
      selection: "6", busy: false,
    }).blocker).toBe("unavailable");
  });
});


describe("blockerText", () => {
  it("explains every blocker except an unchanged selection", () => {
    for (const blocker of ["offline", "unavailable", "closed", "busy"]) {
      expect(typeof blockerText(blocker)).toBe("string");
      expect(blockerText(blocker).length).toBeGreaterThan(20);
    }
    expect(blockerText("unchanged")).toBeNull();
    expect(blockerText(null)).toBeNull();
  });

  it("names the pin problem in the mid-grip refusal", () => {
    expect(blockerText("closed")).toMatch(/Open first/);
  });
});


describe("gripperLabel", () => {
  it("names the selected output for a controller-output gripper", () => {
    expect(gripperLabel(cgpio({ output: "CO3" })))
      .toBe("Controller output CO3");
  });

  it("names the other supported grippers and the unconfigured case", () => {
    expect(gripperLabel({ kind: "lite6_service" })).toBe("Lite 6 built-in");
    expect(gripperLabel({ kind: "moveit_action" }))
      .toBe("MoveIt gripper action");
    expect(gripperLabel({ kind: null })).toBe("Not configured");
    expect(gripperLabel(undefined)).toBe("Not configured");
  });
});
