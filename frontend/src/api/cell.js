async function detail(res) {
  try {
    const body = await res.json();
    if (typeof body.detail === "string") return body.detail;
  } catch {
    // Use the HTTP status below when an intermediary returned non-JSON.
  }
  return `HTTP ${res.status}`;
}

export async function acknowledgeCellIncident(serial, operatorNote = "") {
  const res = await fetch(
    `/api/cell/printers/${encodeURIComponent(serial)}/acknowledge-incident`,
    { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ operator_note: operatorNote }) },
  );
  if (!res.ok) throw new Error(await detail(res));
  return res.json();
}

export async function fetchCellProfiles() {
  const res = await fetch("/api/cell/profiles", { cache: "no-store" });
  if (!res.ok) throw new Error(await detail(res));
  return res.json();
}

export async function fetchCellLayout() {
  const res = await fetch("/api/cell/layout", { cache: "no-store" });
  if (!res.ok) throw new Error(await detail(res));
  return res.json();
}

export async function saveCellLayout(layout) {
  const res = await fetch("/api/cell/layout", {
    method: "PUT", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(layout),
  });
  if (!res.ok) throw new Error(await detail(res));
  return res.json();
}

export async function saveCellProfile(profile) {
  const res = await fetch("/api/cell/profiles", {
    method: "PUT", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ profile }),
  });
  if (!res.ok) throw new Error(await detail(res));
  return res.json();
}

export async function deleteCellProfile(profileId) {
  const res = await fetch(`/api/cell/profiles/${encodeURIComponent(profileId)}`, {
    method: "DELETE",
  });
  if (!res.ok) throw new Error(await detail(res));
}

export async function captureProfilePose(markerId, targetFrame = "tcp", translationFrame = "marker", orientationFrame = "marker") {
  const res = await fetch("/api/robot/profile-pose", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ marker_id: Number(markerId), target_frame: targetFrame,
      translation_frame: translationFrame, orientation_frame: orientationFrame }),
  });
  if (!res.ok) throw new Error(await detail(res));
  return res.json();
}

export async function capturePreviousTcpOffset(profile, stepIndex) {
  const res = await fetch("/api/robot/profile-previous-tcp-offset", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ profile, step_index: stepIndex }),
  });
  if (!res.ok) throw new Error(await detail(res));
  return res.json();
}

export async function captureAbsoluteTcpPose() {
  const res = await fetch("/api/robot/profile-absolute-tcp", { method: "POST" });
  if (!res.ok) throw new Error(await detail(res));
  return res.json();
}

export async function runCellProfileDryRun(profileId) {
  const res = await fetch(`/api/cell/profiles/${encodeURIComponent(profileId)}/dry-run`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ area_clear: true }),
  });
  if (!res.ok) throw new Error(await detail(res));
  return res.json();
}

export async function runCellProfile(profileId) {
  const res = await fetch(`/api/cell/profiles/${encodeURIComponent(profileId)}/execute`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ area_clear: true, supervised: true, contact_fixture_confirmed: true }),
  });
  if (!res.ok) throw new Error(await detail(res));
  return res.json();
}
