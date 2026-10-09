const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("hermesDesktop", {
  chooseFile: (kind) => ipcRenderer.invoke("hermes:choose-file", kind)
});
