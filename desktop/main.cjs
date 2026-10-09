const { app, BrowserWindow, dialog, ipcMain, session } = require("electron");
const { ElectronBlocker } = require("@ghostery/adblocker-electron");
const fetch = require("cross-fetch");
const { spawn } = require("node:child_process");
const path = require("node:path");
const fs = require("node:fs");
const net = require("node:net");

let backend = null;
let backendExited = false;
let shuttingDown = false;
let backendExitInfo = null;
let mainWindow = null;
let browserSession = null;
let browserAdBlocker = null;
let browserFallbackActive = false;
let browserAdBlockEnabled = true;
let browserAdBlockState = {
  ready: false,
  enabled: true,
  mode: "loading",
  message: "Preparing ad filters…"
};
const BROWSER_PARTITION = "persist:hermes-browser";
const FALLBACK_AD_DOMAINS = [
  "doubleclick.net",
  "googlesyndication.com",
  "googleadservices.com",
  "adservice.google.com",
  "adnxs.com",
  "adsrvr.org",
  "adform.net",
  "taboola.com",
  "outbrain.com",
  "criteo.com",
  "pubmatic.com",
  "openx.net",
  "rubiconproject.com",
  "amazon-adsystem.com",
  "zedo.com",
  "adroll.com",
  "smartadserver.com"
];
const root = path.resolve(__dirname, "..");
const python = path.join(root, ".venv", "Scripts", "python.exe");
const appScript = path.join(root, "app.py");
const baseUrl = "http://127.0.0.1:8765/manager";

function browserAdSettingsPath() {
  return path.join(app.getPath("userData"), "hermes-browser-settings.json");
}

function loadBrowserAdBlockPreference() {
  try {
    const settings = JSON.parse(fs.readFileSync(browserAdSettingsPath(), "utf8"));
    return settings.adBlockingEnabled !== false;
  } catch (_) {
    return true;
  }
}

function saveBrowserAdBlockPreference(enabled) {
  try {
    fs.mkdirSync(path.dirname(browserAdSettingsPath()), { recursive: true });
    fs.writeFileSync(browserAdSettingsPath(), JSON.stringify({ adBlockingEnabled: enabled }, null, 2));
  } catch (error) {
    console.warn("[Hermes] Could not persist ad-block preference:", error.message);
  }
}

function hostnameIsAdDomain(hostname) {
  const host = String(hostname || "").toLowerCase().replace(/\.$/, "");
  return FALLBACK_AD_DOMAINS.some(domain => host === domain || host.endsWith("." + domain));
}

function installFallbackAdFilter() {
  if (!browserSession) return;
  browserSession.webRequest.onBeforeRequest({ urls: ["<all_urls>"] }, (details, callback) => {
    let cancel = false;
    try {
      const url = new URL(details.url);
      cancel = url.protocol !== "file:" && hostnameIsAdDomain(url.hostname);
    } catch (_) {}
    callback({ cancel });
  });
  browserFallbackActive = true;
}

function disableFallbackAdFilter() {
  if (!browserSession || !browserFallbackActive) return;
  browserSession.webRequest.onBeforeRequest(null);
  browserFallbackActive = false;
}

async function fetchFilterResource(url, options = {}) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 12000);
  try {
    return await fetch(url, { ...options, signal: controller.signal });
  } finally {
    clearTimeout(timeout);
  }
}

async function initializeBrowserAdBlocking() {
  browserSession = session.fromPartition(BROWSER_PARTITION);
  browserAdBlockEnabled = loadBrowserAdBlockPreference();
  browserAdBlockState = {
    ready: false,
    enabled: browserAdBlockEnabled,
    mode: "loading",
    message: "Loading the EasyList-compatible ad and tracker filters…"
  };

  const cacheDir = path.join(app.getPath("userData"), "hermes-browser");
  const cachePath = path.join(cacheDir, "adblocker-engine.bin");
  try {
    fs.mkdirSync(cacheDir, { recursive: true });
    try {
      browserAdBlocker = await ElectronBlocker.fromPrebuiltAdsAndTracking(fetchFilterResource, {
        path: cachePath,
        read: filePath => fs.promises.readFile(filePath),
        write: (filePath, contents) => fs.promises.writeFile(filePath, contents)
      });
    } catch (cacheOrNetworkError) {
      // A stale/corrupt cache can fail to deserialize. Try once without cached state.
      try { fs.unlinkSync(cachePath); } catch (_) {}
      browserAdBlocker = await ElectronBlocker.fromPrebuiltAdsAndTracking(fetchFilterResource);
      try { await fs.promises.writeFile(cachePath, browserAdBlocker.serialize()); } catch (_) {}
    }

    if (browserAdBlockEnabled) {
      browserAdBlocker.enableBlockingInSession(browserSession);
    } else {
      browserAdBlocker.disableBlockingInSession(browserSession);
    }
    browserAdBlockState = {
      ready: true,
      enabled: browserAdBlockEnabled,
      mode: "EasyList ads + tracking",
      message: browserAdBlockEnabled
        ? "Ad and tracker filter lists are active in the integrated browser."
        : "Ad blocking is off. Enable it here to filter ads and trackers."
    };
  } catch (error) {
    browserAdBlocker = null;
    if (browserAdBlockEnabled) installFallbackAdFilter();
    browserAdBlockState = {
      ready: true,
      enabled: browserAdBlockEnabled,
      mode: "basic fallback",
      message: browserAdBlockEnabled
        ? "Full filter lists could not load. Basic ad-domain blocking is active instead (" + error.message + ")."
        : "Full filter lists could not load. Basic ad-domain blocking is off."
    };
    console.warn("[Hermes] Advanced ad filtering unavailable:", error.message);
  }
  return { ...browserAdBlockState };
}

function setBrowserAdBlockEnabled(enabled) {
  browserAdBlockEnabled = enabled === true;
  if (browserAdBlocker) {
    if (browserAdBlockEnabled) {
      browserAdBlocker.enableBlockingInSession(browserSession);
    } else {
      browserAdBlocker.disableBlockingInSession(browserSession);
    }
    browserFallbackActive = false;
  } else if (browserAdBlockEnabled) {
    installFallbackAdFilter();
  } else {
    disableFallbackAdFilter();
  }
  browserAdBlockState = {
    ...browserAdBlockState,
    ready: true,
    enabled: browserAdBlockEnabled,
    message: browserAdBlockEnabled
      ? (browserAdBlockState.mode === "basic fallback"
        ? "Basic ad-domain blocking is active; the full filter list could not be loaded."
        : "Ad and tracker filtering is active in the integrated browser.")
      : "Ad blocking is off."
  };
  saveBrowserAdBlockPreference(browserAdBlockEnabled);
  return { ...browserAdBlockState };
}

ipcMain.handle("hermes:adblock:get-status", () => ({ ...browserAdBlockState }));
ipcMain.handle("hermes:adblock:set-enabled", (_event, enabled) => setBrowserAdBlockEnabled(enabled));

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
  // On some Windows installations the first Python import (notably Crawl4AI)
  // can take longer than 30 seconds. Keep waiting for the real health endpoint
  // instead of showing a false startup failure while the backend is still booting.
  const startedAt = Date.now();
  const deadline = startedAt + 120000;
  let lastError = "API has not responded yet";
  while (Date.now() < deadline) {
    if (backendExited) {
      backendExitInfo = backendExitInfo || { error: "Backend exited before the API became ready" };
      return false;
    }
    try {
      const response = await fetch("http://127.0.0.1:8765/api/status", {
        signal: AbortSignal.timeout(1500)
      });
      if (response.ok) return true;
      lastError = "Health endpoint returned HTTP " + response.status;
    } catch (error) {
      lastError = error && error.message ? error.message : String(error);
    }
    await new Promise(resolve => setTimeout(resolve, 400));
  }
  backendExitInfo = { error: "Timed out after " + Math.round((Date.now() - startedAt) / 1000) + " seconds waiting for /api/status; last result: " + lastError };
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
      ? (backendExitInfo.error || ("The Python process exited (" + (backendExitInfo.signal || backendExitInfo.code || "unknown") + ")."))
      : "The local API did not respond within 120 seconds.";
    dialog.showErrorBox("Hermes did not start", detail + " Inspect data/backend.log for the startup traceback.");
    app.quit();
    return;
  }
  await initializeBrowserAdBlocking();
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
