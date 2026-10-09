const { app, BrowserWindow, dialog, ipcMain } = require("electron");
const { spawn } = require("node:child_process");
const path = require("node:path");
const fs = require("node:fs");

let backend = null;
let mainWindow = null;
const root = path.resolve(__dirname, "..");
const python = path.join(root, ".venv", "Scripts", "python.exe");
const appScript = path.join(root, "app.py");
const baseUrl = "http://127.0.0.1:8765/manager";

function startBackend() {
  if (!fs.existsSync(python)) {
    dialog.showErrorBox("Hermes setup required", "Run launch-desktop.bat first so Hermes can create its Python environment.");
    app.quit();
    return;
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
  backend.on("error", error => dialog.showErrorBox("Hermes backend failed", error.message));
  backend.on("exit", () => {
    try { fs.closeSync(logFd); } catch (_) {}
  });
}

async function waitForServer() {
  const deadline = Date.now() + 30000;
  while (Date.now() < deadline) {
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
  startBackend();
  if (!backend) return;
  const ready = await waitForServer();
  if (!ready) {
    dialog.showErrorBox("Hermes did not start", "The local API did not respond on port 8765. Check whether another Hermes instance is running or inspect data/backend.log.");
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
  if (backend && !backend.killed) backend.kill();
});
app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});
