// The Gripper card's controller-output picker, as pure rules.
//
// The xArm control box exposes CO0..CO7 and the gripper is wired into one of
// them (master.md section 16.6). Which one is a wiring decision, so the page
// lets the operator choose it. This module decides WHEN the picker shows and
// WHETHER "Apply" is allowed, so those rules can be tested without a DOM --
// the same extract-the-rules-for-testability pattern printerStatus.js follows.
//
// The rule for visibility mirrors the one the open/close buttons already use:
// hide only on a POSITIVE "not applicable". A server with no arm configured,
// or an older backend whose telemetry carries no gripper block, still shows
// the picker -- otherwise the setting is undiscoverable until the exact
// moment it is already right. Only a gripper that positively has no output
// to point anywhere (the Lite 6's built-in services, the AR4's MoveIt action)
// hides it.

export const DEFAULT_CO_COUNT = 8;

// Kinds the automation package reports whose gripper is not driven from a
// controller output. Anything else -- "cgpio", null (uncommissioned), or a
// missing block -- keeps the picker.
const NO_OUTPUT_KINDS = new Set(["lite6_service", "moveit_action"]);


// -> null when the picker should not render at all; otherwise
//    { outputs, value, current, blocker }:
//      outputs  the CO indices to offer
//      value    what the <select> shows: the operator's pick, else the live
//               setting, else CO0
//      current  the live setting from telemetry, or null when unknown
//      blocker  null when Apply is allowed, else why it is not:
//               "offline" | "unavailable" | "closed" | "busy" | "unchanged"
export function gripperOutputView({ robot, gripper, selection, busy }) {
  const kind = gripper?.kind;
  if (kind && NO_OUTPUT_KINDS.has(kind)) return null;

  const reported = Number(gripper?.output_count);
  const count = Number.isInteger(reported) && reported > 0
    ? reported : DEFAULT_CO_COUNT;
  const outputs = Array.from({ length: count }, (_, index) => index);

  const current = Number.isInteger(gripper?.ionum) ? gripper.ionum : null;
  const picked = selection == null ? "" : String(selection);
  const value = picked !== "" ? picked : String(current ?? 0);

  let blocker = null;
  if (!robot) blocker = "offline";
  else if (!robot.available) blocker = "unavailable";
  // Checked before busy: a closed gripper is the reason that outlives the
  // current command, and the one the operator has to act on.
  else if (gripper?.command === "close") blocker = "closed";
  else if (busy) blocker = "busy";
  // With no live setting to compare against, let the server decide -- it
  // answers 400/503 with a readable reason if this backend truly has none.
  else if (current !== null && Number(value) === current) blocker = "unchanged";

  return { outputs, value, current, blocker };
}


// The help line under the picker for a given blocker; null when there is
// nothing to explain (Apply is allowed, or the selection simply matches).
export function blockerText(blocker) {
  switch (blocker) {
    case "offline":
      return "No arm is configured on this server, so the choice cannot be " +
        "applied yet. Start the server with --robot-mode mock or ros and the " +
        "picker seeds itself from the live setting.";
    case "unavailable":
      return "The arm is unavailable right now. The output can be changed " +
        "once it reports in.";
    case "closed":
      return "The output cannot be changed while the gripper is commanded " +
        "closed — the current pin stays latched, and switching would leave " +
        "it holding with nothing tracking it. Open first.";
    case "busy":
      return "Wait for the running command to finish before changing the " +
        "output.";
    default:
      return null;
  }
}


// Human name for the gripper telemetry reports, for the Hardware readout.
export function gripperLabel(gripper) {
  if (!gripper?.kind) return "Not configured";
  if (gripper.kind === "cgpio") {
    return `Controller output ${gripper.output ?? "CO0"}`;
  }
  if (gripper.kind === "lite6_service") return "Lite 6 built-in";
  if (gripper.kind === "moveit_action") return "MoveIt gripper action";
  return gripper.kind;
}
