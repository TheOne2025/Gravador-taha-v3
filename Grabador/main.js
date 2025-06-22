require('electron-reload')(__dirname, {
  electron: require(`${__dirname}/node_modules/electron`)
});


const { spawn } = require('child_process');
const path = require('path');

const pythonCmd = process.env.PYTHON || (process.platform === 'win32' ? 'python' : 'python3');
const scriptPath = path.join(__dirname, 'grabador_api_backend.py');

const pythonProcess = spawn(pythonCmd, [scriptPath], {
  cwd: __dirname
});

pythonProcess.on('error', (err) => {
  console.error('Failed to start Python process:', err);
});

pythonProcess.on('exit', (code) => {
  console.log(`Python process exited with code ${code}`);
});

pythonProcess.stdout.on('data', (data) => {
  console.log(`PYTHON: ${data}`);
});

pythonProcess.stderr.on('data', (data) => {
  console.error(`PYTHON ERROR: ${data}`);
});

const { app, BrowserWindow, ipcMain } = require('electron');

let win;

function createWindow () {
  win = new BrowserWindow({
    width: 1000,
    height: 655,
    minWidth: 1010,     // Establece ancho mínimo
    minHeight: 655,     // Establece alto mínimo
    frame: false,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false
    }
  });

  win.loadFile('renderer/index.html');
}

app.whenReady().then(createWindow);

ipcMain.on('window:minimize', () => win.minimize());
ipcMain.on('window:maximize', () => {
  win.isMaximized() ? win.unmaximize() : win.maximize();
});
ipcMain.on('window:close', () => win.close());

app.on('will-quit', () => {
  try {
    pythonProcess.kill();
  } catch (e) {
    console.error('Failed to terminate Python process:', e);
  }
});
