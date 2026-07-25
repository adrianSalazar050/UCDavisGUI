async function detail(res) {
  try {
    const body = await res.json();
    if (typeof body.detail === "string") return body.detail;
    if (Array.isArray(body.detail)) {
      return body.detail.map((item) => item.msg ?? JSON.stringify(item)).join("; ");
    }
  } catch {
    // Fall through to the status-based message.
  }
  return `HTTP ${res.status}`;
}

export async function fetchRobotStatus() {
  const res = await fetch("/api/robot/status", { cache: "no-store" });
  if (!res.ok) throw new Error(await detail(res));
  return res.json();
}

export async function sendRobotCommand(action, parameters = {}) {
  const res = await fetch("/api/robot/commands", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action, parameters }),
  });
  if (!res.ok) throw new Error(await detail(res));
  return res.json();
}

export async function cancelRobotCommand(commandId) {
  const res = await fetch(
    `/api/robot/commands/${encodeURIComponent(commandId)}/cancel`,
    { method: "POST" },
  );
  if (!res.ok) throw new Error(await detail(res));
  return res.json();
}
