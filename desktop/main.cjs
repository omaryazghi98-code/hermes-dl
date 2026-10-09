const { app, BrowserWindow, dialog, ipcMain } = require("electron");
const { spawn } = require("node:child_process");
const path = require("node:path");
const fs = require("node:fs");
const net = require("node:net");

let backend = null;
let backendExited = false;
let shuttingDown = false;
let backendExitInfo = null;
let mainWindow = null;
const root = path.resolve(__dirname, "..");
const python = path.join(root, ".venv", "Scripts", "python.exe");
const appScript = path.join(root, "app.py");
const baseUrl = "http://127.0.0.1:8765/manager";

function portIsAvailable(port) {
  return new Promise(resolve => {
    const probe = net.createServer();
    probe.once("error", () => resolve(false));
    probe.once("listening", () => probe.close(() => resolve(true)));
    probe.listen(port, "127.0.0.1");
  });
}

function startBackend() {
  if (!fs.existsSync(python)) {
    dialog.showErrorBox("Hermes setup required", "Run launch-desktop.bat first so Hermes can create its Python environment.");
    app.quit();
    return false;
  }
  const dataDir = path.join(root, "data");
  fs.mkdirSync(dataDir, { recursive: true });
  const logPath = path.join(dataDir, "backend.log");
  const logFd = fs.openSync(logPath, "a");
  fs.writeSync(logFd, String.fromCharCode(10, 10) + "=== Hermes Manager start " + new Date().toISOString() + " ===" + String.fromCharCode(10));
  backend = spawn(python, [appScript], {
    cwd: root,
    env: { ...process.env, HERMES_DESKTOP: "1" },
    windowsHide: true,
    stdio: ["ignore", logFd, logFd]
  });
  backend.on("error", error => {
    backendExited = true;
    backendExitInfo = { error: error.message };
    if (!shuttingDown) dialog.showErrorBox("Hermes backend failed", error.message);
  });
  backend.on("exit", (code, signal) => {
    backendExited = true;
    backendExitInfo = { code, signal };
    try { fs.closeSync(logFd); } catch (_) {}
    if (mainWindow && !shuttingDown) {
      dialog.showErrorBox("Hermes backend stopped", "The Python service exited (" + (signal || code) + "). Check data/backend.log.");
    }
  });
  return true;
}

async function waitForServer() {
  const deadline = Date.now() + 30000;
  while (Date.now() < deadline) {
    if (backendExited) return false;
    try {
      const response = await fetch("http://127.0.0.1:8765/api/status");
      if (response.ok) return true;
    } catch (_) {}
    await new Promise(resolve => setTimeout(resolve, 400));
  }
  return false;
}

ipcMain.handle("hermes:choose-file", async (_event, kind) => {
  const filters = kind === "executable"
    ? [{ name: "Windows applications", extensions: ["exe"] }]
    : kind === "pkg"
      ? [{ name: "PlayStation package", extensions: ["pkg"] }]
      : [{ name: "All files", extensions: ["*"] }];
  const result = await dialog.showOpenDialog(mainWindow || undefined, {
    title: kind === "executable" ? "Choose application executable" : kind === "pkg" ? "Choose a PKG file" : "Choose local transfer file",
    properties: ["openFile"],
    filters
  });
  return result.canceled || !result.filePaths.length ? null : result.filePaths[0];
});

async function createWindow() {
  if (!await portIsAvailable(8765)) {
    dialog.showErrorBox(
      "Hermes is already running",
      "Something is already listening on 127.0.0.1:8765. Close the manually launched Python/Flask window or stop the other Hermes instance, then run launch-desktop.bat again. Hermes stopped here rather than connecting to a possibly stale backend."
    );
    app.quit();
    return;
  }
  if (!startBackend()) return;
  const ready = await waitForServer();
  if (!ready) {
    const detail = backendExitInfo
      ? "The Python process exited (" + (backendExitInfo.signal || backendExitInfo.code || backendExitInfo.error || "unknown") + ")."
      : "The local API did not respond within 30 seconds.";
    dialog.showErrorBox("Hermes did not start", detail + " Inspect data/backend.log for the startup traceback.");
    app.quit();
    return;
  }
  mainWindow = new BrowserWindow({
    width: 1480,
    height: 960,
    minWidth: 1050,
    minHeight: 700,
    backgroundColor: "#090c12",
    title: "Hermes Manager",
    webPreferences: {
      webviewTag: true,
      preload: path.join(__dirname, "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true
    }
  });
  mainWindow.loadURL(baseUrl);
  mainWindow.on("closed", () => { mainWindow = null; });
}

app.whenReady().then(createWindow);
app.on("before-quit", () => {
  shuttingDown = true;
  if (backend && !backend.killed) backend.kill();
});
app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});
