// Local folder watcher — the desktop-side of integrations Phase 4.
//
// Every POLL_MS, fetch the list of local_folder integrations from the
// local backend, and for each one scan the folder for new video files.
// When we find a file we haven't seen before and it's stable (size hasn't
// changed in STABILITY_MS), upload it to /api/integrations/local_folder/upload
// and the backend dedupes + queues it through the transcription pipeline.
//
// Dedupe strategy: external_id = `<absolute_path>::<size>`. The server
// uses that as a unique key in integration_imports. Re-scanning the same
// file on every poll is a no-op; only new or grown files trigger uploads.
const fs = require('fs');
const path = require('path');
const http = require('http');

const POLL_MS = 30_000;          // 30s folder scan cadence
const STABILITY_MS = 15_000;     // file size must be stable for this long
const VIDEO_EXT = new Set([
  '.mp4', '.mov', '.mkv', '.webm', '.avi',
  '.m4a', '.mp3', '.wav', '.flac', '.ogg',
]);
const LOG_PREFIX = '[folder-watcher]';

let timer = null;
let serverPort = null;
// Per-file state: { path: { size, firstSeenAt, uploaded? } }
// Persists across polls but not across app restarts — the server's
// integration_imports table is the durable dedupe store.
const seen = new Map();

function log(...args) {
  console.log(LOG_PREFIX, ...args);
}

function warn(...args) {
  console.warn(LOG_PREFIX, ...args);
}

function apiGetJson(pathname) {
  return new Promise((resolve, reject) => {
    const req = http.request(
      { host: '127.0.0.1', port: serverPort, path: pathname, method: 'GET' },
      (res) => {
        let body = '';
        res.on('data', (c) => { body += c; });
        res.on('end', () => {
          if (res.statusCode >= 200 && res.statusCode < 300) {
            try { resolve(JSON.parse(body)); }
            catch { resolve({}); }
          } else {
            reject(new Error(`GET ${pathname} failed: ${res.statusCode} ${body.slice(0, 200)}`));
          }
        });
      },
    );
    req.on('error', reject);
    req.end();
  });
}

/**
 * Multipart upload of one local file to the backend. Done with raw Node
 * http to avoid pulling in a dep; this is the one HTTP request the
 * watcher actually makes, everything else is tiny JSON.
 */
function uploadFile({ integrationId, externalId, filePath }) {
  return new Promise((resolve, reject) => {
    const boundary = '----wfb' + Math.random().toString(16).slice(2);
    const filename = path.basename(filePath);
    const headerPart = Buffer.from(
      [
        `--${boundary}\r\n`,
        'Content-Disposition: form-data; name="integration_id"\r\n\r\n',
        `${integrationId}\r\n`,
        `--${boundary}\r\n`,
        'Content-Disposition: form-data; name="external_id"\r\n\r\n',
        `${externalId}\r\n`,
        `--${boundary}\r\n`,
        'Content-Disposition: form-data; name="filename"\r\n\r\n',
        `${filename}\r\n`,
        `--${boundary}\r\n`,
        `Content-Disposition: form-data; name="file"; filename="${filename}"\r\n`,
        'Content-Type: application/octet-stream\r\n\r\n',
      ].join(''),
    );
    const trailer = Buffer.from(`\r\n--${boundary}--\r\n`);

    const stat = fs.statSync(filePath);
    const contentLength = headerPart.length + stat.size + trailer.length;

    const req = http.request(
      {
        host: '127.0.0.1',
        port: serverPort,
        path: '/api/integrations/local_folder/upload',
        method: 'POST',
        headers: {
          'Content-Type': `multipart/form-data; boundary=${boundary}`,
          'Content-Length': contentLength,
        },
      },
      (res) => {
        let body = '';
        res.on('data', (c) => { body += c; });
        res.on('end', () => {
          if (res.statusCode >= 200 && res.statusCode < 300) {
            try { resolve(JSON.parse(body)); }
            catch { resolve({}); }
          } else {
            reject(new Error(`upload failed: ${res.statusCode} ${body.slice(0, 400)}`));
          }
        });
      },
    );
    req.on('error', reject);

    req.write(headerPart);
    const stream = fs.createReadStream(filePath);
    stream.on('error', reject);
    stream.on('end', () => {
      req.write(trailer);
      req.end();
    });
    stream.pipe(req, { end: false });
  });
}

/** List video files in a directory, non-recursive. Ignores hidden files
 *  (lots of tools create .DS_Store-style siblings that would pollute
 *  the watcher's state). */
function listVideoFiles(dir) {
  let entries;
  try {
    entries = fs.readdirSync(dir, { withFileTypes: true });
  } catch (e) {
    warn('cannot read folder', dir, e.message);
    return [];
  }
  const out = [];
  for (const entry of entries) {
    if (entry.name.startsWith('.')) continue;
    if (!entry.isFile()) continue;
    const ext = path.extname(entry.name).toLowerCase();
    if (!VIDEO_EXT.has(ext)) continue;
    const full = path.join(dir, entry.name);
    try {
      const st = fs.statSync(full);
      out.push({ path: full, size: st.size, mtimeMs: st.mtimeMs });
    } catch (e) {
      // File disappeared between readdir and stat — ignore, we'll catch it
      // on the next scan.
    }
  }
  return out;
}

/** Run one full scan pass across every local_folder integration. */
async function tick() {
  let integrations;
  try {
    const status = await apiGetJson('/api/integrations/status');
    integrations = (status.cards || [])
      .filter((c) => c.provider === 'local_folder' && c.integration)
      .map((c) => c.integration);
  } catch (e) {
    warn('could not fetch integrations:', e.message);
    return;
  }

  for (const integration of integrations) {
    if (integration.sync_mode === 'off') continue;
    const folder = (integration.settings || {}).folder_path;
    if (!folder || !fs.existsSync(folder)) {
      warn('folder missing for integration', integration.id, folder);
      continue;
    }

    const files = listVideoFiles(folder);
    const nowMs = Date.now();

    for (const f of files) {
      const externalId = `${f.path}::${f.size}`;
      const state = seen.get(externalId);
      if (state && state.uploaded) {
        // Already uploaded in this process — server also has a permanent
        // record, so this is just a local fast-path.
        continue;
      }
      if (!state) {
        seen.set(externalId, {
          size: f.size,
          firstSeenAt: nowMs,
          lastChangeAt: nowMs,
          uploaded: false,
        });
        continue;
      }
      if (state.size !== f.size) {
        state.size = f.size;
        state.lastChangeAt = nowMs;
        continue;
      }
      // Size stable — has it been stable long enough to trust?
      if (nowMs - state.lastChangeAt < STABILITY_MS) continue;

      try {
        log('uploading', f.path, '→ integration', integration.id);
        const result = await uploadFile({
          integrationId: integration.id,
          externalId,
          filePath: f.path,
        });
        state.uploaded = true;
        log('queued', result.status, 'transcription_id=' + (result.transcription_id || 'n/a'));
      } catch (e) {
        warn('upload failed for', f.path, '—', e.message);
        // Leave state in place so next tick retries. If the failure is
        // permanent (e.g. server-side 413), the user will notice the
        // file never makes it; good enough for Phase 4.
      }
    }
  }
}

function start({ port }) {
  serverPort = port;
  if (timer) return;
  log('starting (port=' + port + ', interval=' + POLL_MS + 'ms)');
  // Kick once after a short delay so the backend is definitely up, then
  // on a steady cadence. setInterval skips a tick if a previous one is
  // still running via the `running` flag.
  let running = false;
  const runOnce = async () => {
    if (running) return;
    running = true;
    try { await tick(); } catch (e) { warn('tick failed', e.message); }
    running = false;
  };
  setTimeout(runOnce, 5_000);
  timer = setInterval(runOnce, POLL_MS);
}

function stop() {
  if (timer) {
    clearInterval(timer);
    timer = null;
    log('stopped');
  }
}

module.exports = { start, stop };
