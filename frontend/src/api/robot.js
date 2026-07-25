// Fetch wrappers for the robot arm. The backend proxies these to
// robot_agent.py on the robot computer.

async function detail(res) {
  const { detail } = await res.json();
  // FastAPI sends a string for our own raises, a list for validation errors
  return Array.isArray(detail)
    ? detail.map((d) => d.msg ?? JSON.stringify(d)).join("; ")
    : detail || `HTTP ${res.status}`;
}

// { reachable, dry_run, robot, busy, current, history, commands: [...] }
// reachable:false comes back 200 with an `error` string; it isn't a failure.
export async function fetchRobot() {
  const res = await fetch("/api/robot");
  if (!res.ok) throw new Error(await detail(res));
  return res.json();
}

// -> { received: true, id, name, params }, the agent's own receipt.
export async function runRobotCommand(name, params) {
  const res = await fetch("/api/robot/command", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, params }),
  });
  if (!res.ok) throw new Error(await detail(res));
  return res.json();
}

// -> { id, name, state: "running"|"done"|"failed", result, error, duration_s }
export async function fetchRobotCommand(id) {
  const res = await fetch(`/api/robot/command/${id}`);
  if (!res.ok) throw new Error(await detail(res));
  return res.json();
}
