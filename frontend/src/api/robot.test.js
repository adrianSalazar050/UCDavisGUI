import { afterEach, describe, expect, it, vi } from "vitest";

import {
  cancelRobotCommand,
  fetchRobotStatus,
  sendRobotCommand,
} from "./robot.js";


afterEach(() => {
  vi.unstubAllGlobals();
});


describe("robot API", () => {
  it("submits a typed movement command", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ id: "cmd-1", state: "queued" }),
    });
    vi.stubGlobal("fetch", fetchMock);

    const result = await sendRobotCommand(
      "move_joints", { joints: [0, 1, 2, 3, 4, 5] });

    expect(result.id).toBe("cmd-1");
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/robot/commands",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          action: "move_joints",
          parameters: { joints: [0, 1, 2, 3, 4, 5] },
        }),
      }),
    );
  });

  it("fetches status without caching", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ available: true, state: "idle" }),
    });
    vi.stubGlobal("fetch", fetchMock);

    expect((await fetchRobotStatus()).state).toBe("idle");
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/robot/status", { cache: "no-store" });
  });

  it("encodes command ids when cancelling", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ state: "cancelling" }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await cancelRobotCommand("id/with space");
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/robot/commands/id%2Fwith%20space/cancel",
      { method: "POST" });
  });

  it("surfaces FastAPI errors", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: false,
      status: 409,
      json: async () => ({ detail: "robot is already moving" }),
    }));
    await expect(sendRobotCommand("home")).rejects.toThrow(
      "robot is already moving");
  });
});
