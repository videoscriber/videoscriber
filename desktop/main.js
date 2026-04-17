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
  // Prefer the per-user venv created by scripts/setup-mac.sh.
  const userVenv = path.join(app.getPath('userData'), 'venv', 'bin', 'python');
  if (fs.existsSync(userVenv)) return userVenv;

  // Fallback: bundled venv (only exists if the build produced one).
  const bundledVenv = path.join(resourcesPath, '.venv', 'bin', 'python');
  if (fs.existsSync(bundledVenv)) return bundledVenv;

  // Last resort: system python. Imports will fail unless requirements are
  // installed globally — setup-mac.sh is the supported path.
  for (const cmd of ['python3', 'python']) {
    try {
      const result = execSync(`which ${cmd}`, { encoding: 'utf8' }).trim();
      if (result) return result;
    } catch {}
  }
  return null;
}

function setupScriptPath() {
  return path.join(resourcesPath, 'scripts', 'setup-mac.sh');
}

function userVenvPath() {
  return path.join(app.getPath('userData'), 'venv');
}

async function ensureUserVenv() {
  const venvDir = userVenvPath();
  const venvPython = path.join(venvDir, 'bin', 'python');
  if (fs.existsSync(venvPython)) return venvPython;

  // Bootstrap with system python — we need it to create the venv.
  let sysPython = null;
  for (const cmd of ['python3.12', 'python3.11', 'python3.10', 'python3']) {
    try {
      const result = execSync(`which ${cmd}`, { encoding: 'utf8' }).trim();
      if (result) { sysPython = result; break; }
    } catch {}
  }
  if (!sysPython) throw new Error('Python 3.10+ not found on PATH.');

  const reqFile = path.join(resourcesPath, 'requirements.txt');
  if (!fs.existsSync(reqFile)) throw new Error(`requirements.txt missing at ${reqFile}`);

  fs.mkdirSync(path.dirname(venvDir), { recursive: true });
  execSync(`"${sysPython}" -m venv "${venvDir}"`, { stdio: 'inherit' });
  execSync(`"${venvPython}" -m pip install --upgrade pip --quiet`, { stdio: 'inherit' });
  execSync(`"${venvPython}" -m pip install -r "${reqFile}" --quiet`, {
    stdio: 'inherit',
    // pip install can take a minute; let it run.
    timeout: 10 * 60 * 1000,
  });
  return venvPython;
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
      'Videoscriber requires Python 3.10+.\n\n' +
      'Install Python (e.g. `brew install python@3.12`), then run the setup script:\n\n' +
      setupScriptPath());
    app.quit();
    return;
  }

  // If the per-user venv isn't set up yet, auto-create it and install
  // requirements. One-time cost (~1 min); avoids making the user open Terminal.
  if (!fs.existsSync(userVenvPath())) {
    try {
      await ensureUserVenv();
    } catch (err) {
      dialog.showErrorBox('First-run setup failed',
        'Could not create the Python environment automatically:\n\n' +
        err.message + '\n\n' +
        'As a workaround, open Terminal and run:\n' +
        `bash "${setupScriptPath()}"`);
      app.quit();
      return;
    }
  }

  const ffmpegDir = findFfmpeg();
  if (!ffmpegDir) {
    dialog.showErrorBox('ffmpeg not found',
      'Videoscriber requires ffmpeg.\n\nbrew install ffmpeg');
    app.quit();
    return;
  }

  serverPort = await findAvailablePort();

  // Build environment
  const userEnvPath = path.join(app.getPath('userData'), '.env');
  const env = {
    ...process.env,
    PATH: `${ffmpegDir}:${process.env.PATH}`,
    HOST: '127.0.0.1',
    PORT: String(serverPort),
    VIDEOSCRIBER_DESKTOP: '1',
    VIDEOSCRIBER_USER_ENV_PATH: userEnvPath,
  };

  // Prefer the user-data .env (written by setup-mac.sh); fall back to a .env
  // next to the bundled backend (useful for `npm start` during development).
  const envCandidates = [
    path.join(app.getPath('userData'), '.env'),
    path.join(resourcesPath, '.env'),
  ];
  for (const envFile of envCandidates) {
    if (!fs.existsSync(envFile)) continue;
    const lines = fs.readFileSync(envFile, 'utf8').split('\n');
    for (const line of lines) {
      const match = line.match(/^([^#=]+)=(.*)$/);
      if (match) env[match[1].trim()] = match[2].trim();
    }
    break;
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
