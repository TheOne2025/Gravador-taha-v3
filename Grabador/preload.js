const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('electron', {
  minimize: () => ipcRenderer.send('window:minimize'),
  toggleFullscreen: () => ipcRenderer.send('window:maximize'),
  close: () => ipcRenderer.send('window:close')
});
