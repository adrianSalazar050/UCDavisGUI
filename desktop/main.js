// Electron main process for Bambu Monitor.
//
// Responsibilities, in order:
//   1. Spawn the frozen FastAPI backend (PyInstaller onedir output), telling it
//      where to keep writable state via BAMBU_DATA_DIR.
//   2. Poll 127.0.0.1:PORT until it answers (or time out with an error dialog).
//   3. Open a window on the local server -- the backend serves the React app,
//      so relative /api and /ws calls resolve against this origin.
//   4. Tear the backend down cleanly on quit (no orphaned process).

const { app, BrowserWindow, dialog, shell, Menu } = require("electron");
const { spawn } = require("child_process");
const http = require("http");
const net = require("net");
const path = require("path");
const fs = require("fs");

const HOST = "127.0.0.1";
// Chosen at startup via getFreePort(). Deliberately NOT a fixed 8000: the dev
// server (python -m server) runs on 8000, and any fixed port can collide with
// something already listening -- which would make the health check silently
// talk to the wrong server. A per-launch free port removes that whole class of
// bug, so the packaged app never fights for a port.
let PORT = 0;
const READY_TIMEOUT_MS = 30000;
const READY_POLL_MS = 300;

// Ask the OS for an unused loopback port by binding :0, then release it and hand
// the number to the backend. The tiny window between release and the backend's
// bind is acceptable for a localhost desktop app and is the standard sidecar
// pattern; a fixed port's guaranteed collisions are the worse trade.
function getFreePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.on("error", reject);
    srv.listen(0, HOST, () => {
      const { port } = srv.address();
      srv.close(() => resolve(port));
    });
  });
}

let backend = null;
let backendStderr = ""; // tail kept for the failure dialog
let mainWindow = null;

// Locate the backend executable. Packaged: extraResources puts the onedir under
// <resources>/backend/. Dev: the PyInstaller output at repo/dist/bambu-backend/.
function backendPath() {
  const exeName = process.platform === "win32" ? "bambu-backend.exe" : "bambu-backend";
  const candidates = app.isPackaged
    ? [path.join(process.resourcesPath, "backend", exeName)]
    : [path.join(__dirname, "..", "dist", "bambu-backend", exeName)];
  for (const c of candidates) {
    if (fs.existsSync(c)) return c;
  }
  return candidates[0]; // report the primary path in the error if missing
}

function startBackend() {
  const exe = backendPath();
  if (!fs.existsSync(exe)) {
    dialog.showErrorBox(
      "Backend missing",
      `Could not find the bundled backend at:\n${exe}\n\n` +
        "The installation may be corrupt. Please reinstall Bambu Monitor."
    );
    app.quit();
    return;
  }

  const env = Object.assign({}, process.env, {
    BAMBU_DATA_DIR: app.getPath("userData"),
    BAMBU_HOST: HOST,
    BAMBU_PORT: String(PORT),
  });

  backend = spawn(exe, [], { env, windowsHide: true });
  backend.stdout.on("data", (d) => process.stdout.write(`[backend] ${d}`));
  backend.stderr.on("data", (d) => {
    process.stderr.write(`[backend] ${d}`);
    backendStderr = (backendStderr + d.toString()).slice(-4000);
  });
  backend.on("exit", (code, signal) => {
    backend = null;
    // If it dies before the window is up, that's fatal (e.g. port in use).
    if (!mainWindow) {
      dialog.showErrorBox(
        "Backend failed to start",
        `The Bambu Monitor backend exited (code ${code}, signal ${signal}).\n\n` +
          (backendStderr ? `Last output:\n${backendStderr}` : "No output was captured.")
      );
      app.quit();
    }
  });
}

function pingOnce() {
  return new Promise((resolve) => {
    const req = http.get(
      { host: HOST, port: PORT, path: "/api/printers", timeout: 1000 },
      (res) => {
        res.resume();
        resolve(res.statusCode > 0);
      }
    );
    req.on("error", () => resolve(false));
    req.on("timeout", () => {
      req.destroy();
      resolve(false);
    });
  });
}

async function waitForBackend() {
  const deadline = Date.now() + READY_TIMEOUT_MS;
  while (Date.now() < deadline) {
    if (backend === null && mainWindow === null) return false; // backend died
    if (await pingOnce()) return true;
    await new Promise((r) => setTimeout(r, READY_POLL_MS));
  }
  return false;
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1280,
    height: 860,
    minWidth: 900,
    minHeight: 600,
    title: "Bambu Monitor",
    backgroundColor: "#0b0f14",
    icon: path.join(__dirname, "icons", process.platform === "win32" ? "icon.ico" : "icon.png"),
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      preload: path.join(__dirname, "preload.js"),
    },
  });

  // Open external links (if any) in the system browser, not inside the app.
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: "deny" };
  });

  mainWindow.loadURL(`http://${HOST}:${PORT}/`);
  mainWindow.on("closed", () => {
    mainWindow = null;
  });
}

function stopBackend() {
  if (!backend) return;
  const proc = backend;
  backend = null;
  try {
    if (process.platform === "win32") {
      // Kill the whole tree; the backend may have spawned children.
      spawn("taskkill", ["/pid", String(proc.pid), "/T", "/F"]);
    } else {
      proc.kill("SIGTERM");
      setTimeout(() => {
        try {
          proc.kill("SIGKILL");
        } catch (_) {}
      }, 3000);
    }
  } catch (_) {}
}

app.whenReady().then(async () => {
  Menu.setApplicationMenu(null); // trim the default menu; app has its own UI
  try {
    PORT = await getFreePort();
  } catch (e) {
    dialog.showErrorBox("Startup error", `Could not allocate a local port:\n${e}`);
    app.quit();
    return;
  }
  startBackend();
  const ok = await waitForBackend();
  if (!ok) {
    if (mainWindow === null && backend !== null) {
      dialog.showErrorBox(
        "Startup timed out",
        `The backend did not respond on http://${HOST}:${PORT} within ` +
          `${READY_TIMEOUT_MS / 1000}s.\n\n` +
          (backendStderr ? `Last output:\n${backendStderr}` : "")
      );
    }
    stopBackend();
    app.quit();
    return;
  }
  createWindow();
});

app.on("activate", () => {
  // macOS: re-open a window when the dock icon is clicked and none are open.
  if (BrowserWindow.getAllWindows().length === 0 && backend) createWindow();
});

app.on("window-all-closed", () => {
  stopBackend();
  app.quit();
});

app.on("before-quit", stopBackend);
app.on("will-quit", stopBackend);
