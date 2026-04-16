const { app, BrowserWindow, dialog, Menu } = require('electron');
const { spawn, execSync } = require('child_process');
const path = require('path');
const net = require('net');
const fs = require('fs');

// Handle Squirrel events for Windows installer
if (require('electron-squirrel-startup')) app.quit();

let mainWindow = null;
let pythonProcess = null;
let serverPort = null;

// Paths
const isDev = !app.isPackaged;
const resourcesPath = isDev
  ? path.join(__dirname, '..')
  : path.join(process.resourcesPath);

const appDataPath = path.join(app.getPath('userData'), 'AppData');
const dataDir = path.join(appDataPath, 'data');
const uploadsDir = path.join(appDataPath, 'uploads');
const audioDir = path.join(appDataPath, 'audio');

// Ensure directories exist
[dataDir, uploadsDir, audioDir].forEach(dir => {
  fs.mkdirSync(dir, { recursive: true });
});

function findAvailablePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.listen(0, '127.0.0.1', () => {
      const port = server.address().port;
      server.close(() => resolve(port));
    });
    server.on('error', reject);
  });
}

function findPython() {
  // Check for bundled Python first, then system Python
  const bundledVenv = path.join(resourcesPath, '.venv', 'bin', 'python');
  if (fs.existsSync(bundledVenv)) return bundledVenv;

  // Try system python
  for (const cmd of ['python3', 'python']) {
    try {
      const result = execSync(`which ${cmd}`, { encoding: 'utf8' }).trim();
      if (result) return result;
    } catch {}
  }
  return null;
}

function findFfmpeg() {
  // Check bundled ffmpeg first
  const bundledFfmpeg = path.join(resourcesPath, 'bin', 'ffmpeg');
  if (fs.existsSync(bundledFfmpeg)) return path.dirname(bundledFfmpeg);

  // System ffmpeg
  try {
    const result = execSync('which ffmpeg', { encoding: 'utf8' }).trim();
    if (result) return path.dirname(result);
  } catch {}
  return null;
}

async function startPythonBackend() {
  const pythonPath = findPython();
  if (!pythonPath) {
    dialog.showErrorBox('Python not found',
      'Videoscriber requires Python 3.10+. Please install Python and try again.');
    app.quit();
    return;
  }

  const ffmpegDir = findFfmpeg();
  if (!ffmpegDir) {
    dialog.showErrorBox('ffmpeg not found',
      'Videoscriber requires ffmpeg. Please install ffmpeg and try again.\n\nbrew install ffmpeg');
    app.quit();
    return;
  }

  serverPort = await findAvailablePort();

  // Build environment
  const env = {
    ...process.env,
    PATH: `${ffmpegDir}:${process.env.PATH}`,
    HOST: '127.0.0.1',
    PORT: String(serverPort),
  };

  // Load .env file if it exists
  const envFile = path.join(resourcesPath, '.env');
  if (fs.existsSync(envFile)) {
    const lines = fs.readFileSync(envFile, 'utf8').split('\n');
    for (const line of lines) {
      const match = line.match(/^([^#=]+)=(.*)$/);
      if (match) env[match[1].trim()] = match[2].trim();
    }
  }

  // Override data paths to use app data directory
  env.HOST = '127.0.0.1';
  env.PORT = String(serverPort);

  const appPy = path.join(resourcesPath, 'app.py');

  pythonProcess = spawn(pythonPath, [appPy], {
    cwd: resourcesPath,
    env,
    stdio: ['ignore', 'pipe', 'pipe'],
  });

  pythonProcess.stdout.on('data', (data) => {
    console.log(`[python] ${data.toString().trim()}`);
  });

  pythonProcess.stderr.on('data', (data) => {
    console.error(`[python] ${data.toString().trim()}`);
  });

  pythonProcess.on('exit', (code) => {
    console.log(`Python process exited with code ${code}`);
    pythonProcess = null;
  });

  // Wait for the server to be ready
  await waitForServer(serverPort);
}

function waitForServer(port, timeout = 30000) {
  const start = Date.now();
  return new Promise((resolve, reject) => {
    function check() {
      if (Date.now() - start > timeout) {
        reject(new Error('Server start timeout'));
        return;
      }
      const req = net.createConnection({ port, host: '127.0.0.1' }, () => {
        req.destroy();
        resolve();
      });
      req.on('error', () => {
        setTimeout(check, 300);
      });
    }
    check();
  });
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1400,
    height: 900,
    minWidth: 900,
    minHeight: 600,
    titleBarStyle: 'hiddenInset',
    trafficLightPosition: { x: 16, y: 16 },
    backgroundColor: '#09090b',
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
    },
  });

  mainWindow.loadURL(`http://127.0.0.1:${serverPort}`);

  mainWindow.on('closed', () => {
    mainWindow = null;
  });

  // Build menu
  const template = [
    {
      label: 'Videoscriber',
      submenu: [
        { role: 'about' },
        { type: 'separator' },
        { role: 'hide' },
        { role: 'hideOthers' },
        { role: 'unhide' },
        { type: 'separator' },
        { role: 'quit' },
      ],
    },
    { label: 'Edit', submenu: [
      { role: 'undo' }, { role: 'redo' }, { type: 'separator' },
      { role: 'cut' }, { role: 'copy' }, { role: 'paste' }, { role: 'selectAll' },
    ]},
    { label: 'View', submenu: [
      { role: 'reload' }, { role: 'forceReload' },
      { role: 'toggleDevTools' }, { type: 'separator' },
      { role: 'resetZoom' }, { role: 'zoomIn' }, { role: 'zoomOut' },
      { type: 'separator' }, { role: 'togglefullscreen' },
    ]},
    { label: 'Window', submenu: [
      { role: 'minimize' }, { role: 'zoom' }, { role: 'close' },
    ]},
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

app.whenReady().then(async () => {
  try {
    await startPythonBackend();
    createWindow();
  } catch (err) {
    dialog.showErrorBox('Startup Error', err.message);
    app.quit();
  }
});

app.on('window-all-closed', () => {
  if (pythonProcess) {
    pythonProcess.kill();
    pythonProcess = null;
  }
  app.quit();
});

app.on('before-quit', () => {
  if (pythonProcess) {
    pythonProcess.kill();
    pythonProcess = null;
  }
});

app.on('activate', () => {
  if (mainWindow === null && serverPort) {
    createWindow();
  }
});
