// Preload script — bridges the Electron main process to the renderer's
// window context via the structured `window.electronAPI` object. Exposes
// only the methods the renderer needs; everything else stays main-side.
//
// Current surface:
//   window.electronAPI.pickFolder()
//     → opens a native folder picker and returns the chosen path (or null
//       if the user cancels). Used by Settings → Integrations → Local
//       folder to collect the folder the Electron watcher should sync.
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('electronAPI', {
  pickFolder: () => ipcRenderer.invoke('local-folder:pick'),
});
