const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("hermesDesktop", {
  chooseFile: (kind) => ipcRenderer.invoke("hermes:choose-file", kind),
  getAdblockStatus: () => ipcRenderer.invoke("hermes:adblock:get-status"),
  setAdblockEnabled: (enabled) => ipcRenderer.invoke("hermes:adblock:set-enabled", Boolean(enabled)),
  refreshAdblockFilters: () => ipcRenderer.invoke("hermes:adblock:refresh")
});
