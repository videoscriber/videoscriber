import { showToast } from './toast.js';
import { initTheme, toggleTheme } from './theme.js';
import { initUpload } from './upload.js';
import {
  renderTranscript, renderProcessing, renderError, clearTranscript,
  initVideoSync, initTranscriptSearch,
} from './transcript.js';

// === State ===
let activeJobId = null;
let activeFormat = 'segments';
let activeRecord = null;
let pollingJobs = new Set();
let previousStatuses = {};
let listData = [];

// === DOM refs ===
const transcriptionList = document.getElementById('transcription-list');
const transcriptSection = document.getElementById('transcript-section');
const emptyState = document.getElementById('empty-state');
const transcriptTitle = document.getElementById('transcript-title');
const formatTabs = document.querySelectorAll('#format-tabs .tab');
const btnCopy = document.getElementById('btn-copy');
const btnDownload = document.getElementById('btn-download');
const btnRetry = document.getElementById('btn-retry');
const videoContainer = document.getElementById('video-container');
const videoPlayer = document.getElementById('preview-player');
const previewTrack = document.getElementById('preview-track');
const videoUploadPrompt = document.getElementById('video-upload-prompt');
const btnUploadVideo = document.getElementById('btn-upload-video');
const videoFileInput = document.getElementById('video-file-input');
const btnRecap = document.getElementById('btn-recap');
const recapModal = document.getElementById('recap-modal');
const recapLoading = document.getElementById('recap-loading');
const recapEditor = document.getElementById('recap-editor');
const recapContent = document.getElementById('recap-content');
const recapActions = document.getElementById('recap-actions');
const recapTo = document.getElementById('recap-to');
const recapSubject = document.getElementById('recap-subject');
const recapCopy = document.getElementById('recap-copy');
const recapSend = document.getElementById('recap-send');
const recapRegenerate = document.getElementById('recap-regenerate');
const recapGuidancePanel = document.getElementById('recap-guidance-panel');
const recapGuidance = document.getElementById('recap-guidance');
const recapGuidanceCancel = document.getElementById('recap-guidance-cancel');
const recapGuidanceSubmit = document.getElementById('recap-guidance-submit');
const btnRename = document.getElementById('btn-rename');
const renameInput = document.getElementById('rename-input');

// === Init ===
initTheme();
initUpload({ onComplete: (jobId) => { startPolling(jobId); refreshList(); } });
initVideoSync();
initTranscriptSearch();
initShortcuts();
initModals();
initVideoUpload();
initRename();
initRecap();
refreshList();

// Format tabs
formatTabs.forEach(tab => {
  tab.addEventListener('click', () => {
    formatTabs.forEach(t => { t.classList.remove('active'); t.setAttribute('aria-selected', 'false'); });
    tab.classList.add('active');
    tab.setAttribute('aria-selected', 'true');
    activeFormat = tab.dataset.format;
    if (activeRecord && activeRecord.status === 'done') {
      renderTranscript(activeRecord, activeFormat);
    }
  });
});

// Copy
function formatTs(seconds) {
  const s = Math.max(0, Number(seconds) || 0);
  const m = Math.floor(s / 60);
  const ss = Math.floor(s % 60).toString().padStart(2, '0');
  return `${m}:${ss}`;
}

function buildCopyText(record) {
  // Prefer the segment-level view: timestamp + speaker + text per line.
  // Falls back to the flat transcript_text if segments are unavailable.
  if (!record.transcript_segments_json) return record.transcript_text || '';
  let segments;
  try { segments = JSON.parse(record.transcript_segments_json); }
  catch { return record.transcript_text || ''; }
  if (!Array.isArray(segments) || !segments.length) return record.transcript_text || '';
  return segments.map(seg => {
    const ts = formatTs(seg.start);
    const speaker = (seg.speaker || '').trim();
    const text = (seg.text || '').trim();
    return speaker ? `[${ts}] ${speaker}: ${text}` : `[${ts}] ${text}`;
  }).join('\n');
}

btnCopy.addEventListener('click', async () => {
  if (!activeRecord) return;
  let text;
  if (activeFormat === 'segments') {
    text = buildCopyText(activeRecord);
  } else {
    const fieldMap = { text: 'transcript_text', srt: 'transcript_srt', vtt: 'transcript_vtt' };
    text = activeRecord[fieldMap[activeFormat]] || '';
  }
  await navigator.clipboard.writeText(text);
  const span = btnCopy.querySelector('span');
  const orig = span.textContent;
  span.textContent = 'Copied!';
  setTimeout(() => { span.textContent = orig; }, 1500);
});

// Download — PDF on Plus, nudge to upgrade on free (with plain-text fallback)
async function downloadPdf() {
  if (!activeJobId) return;
  window.location.href = `/api/transcriptions/${activeJobId}/download/pdf`;
}

function showPdfUpgradeNudge() {
  const existing = document.getElementById('pdf-upgrade-modal');
  if (existing) { existing.hidden = false; return; }
  const modal = document.createElement('div');
  modal.id = 'pdf-upgrade-modal';
  modal.className = 'modal-overlay';
  modal.innerHTML = `
    <div class="modal-dialog plus-nudge-dialog">
      <div class="plus-nudge-icon">
        <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
      </div>
      <h3>PDF export is a Plus feature</h3>
      <p>Download a beautifully formatted PDF with the meeting summary on the cover, speaker labels, and the full transcript — ready to share or archive.</p>
      <div class="plus-nudge-actions">
        <button type="button" class="btn-ghost" data-act="txt">Download plain text</button>
        <a href="/upgrade" class="btn-primary">Upgrade to Plus</a>
      </div>
    </div>`;
  modal.addEventListener('click', (e) => {
    if (e.target === modal) modal.hidden = true;
    if (e.target.dataset.act === 'txt') {
      modal.hidden = true;
      window.location.href = `/api/transcriptions/${activeJobId}/download/txt`;
    }
  });
  document.body.appendChild(modal);
}

btnDownload.addEventListener('click', async () => {
  if (!activeJobId) return;
  try {
    const res = await fetch('/api/config');
    const data = await res.json();
    if ((data.plan?.tier || 'free') === 'plus') {
      downloadPdf();
    } else {
      showPdfUpgradeNudge();
    }
  } catch {
    showPdfUpgradeNudge();
  }
});

// Retry
btnRetry.addEventListener('click', async () => {
  if (!activeJobId) return;
  await fetch(`/api/transcriptions/${activeJobId}/retry`, { method: 'POST' });
  startPolling(activeJobId);
  refreshList();
  showToast('Retrying transcription...', 'info');
});

// === Polling ===
function startPolling(jobId) {
  if (pollingJobs.has(jobId)) return;
  pollingJobs.add(jobId);
  pollJob(jobId);
}

async function pollJob(jobId) {
  if (!pollingJobs.has(jobId)) return;
  try {
    const res = await fetch(`/api/transcriptions/${jobId}`);
    const data = await res.json();

    // Keep listData in sync so queue-position calculations stay accurate
    const listIdx = listData.findIndex(x => x.id === jobId);
    if (listIdx >= 0) listData[listIdx] = data;

    updateListItem(data);

    // Check for status transitions
    const prev = previousStatuses[jobId];
    if (prev && prev !== 'done' && data.status === 'done') {
      showToast(`${data.filename} transcription complete!`, 'success');
    }
    if (prev && prev !== 'error' && data.status === 'error') {
      showToast(`${data.filename} failed: ${data.error_message || 'Unknown error'}`, 'error');
    }
    // If this job moved out of the queue, re-render still-pending items so their position numbers update
    if (prev === 'pending' && data.status !== 'pending') {
      for (const other of listData) {
        if (other.status === 'pending' && other.id !== jobId) updateListItem(other);
      }
    }
    previousStatuses[jobId] = data.status;

    if (activeJobId === jobId) showTranscript(data);

    if (data.status === 'done' || data.status === 'error') {
      pollingJobs.delete(jobId);
      return;
    }
  } catch (e) {
    // Network error, retry
  }
  setTimeout(() => pollJob(jobId), 2000);
}

// === List ===
async function refreshList() {
  const res = await fetch('/api/transcriptions');
  listData = await res.json();

  if (listData.length === 0) {
    transcriptionList.innerHTML = '<p class="empty-state" style="padding:1rem;font-size:0.8rem;">No transcriptions yet</p>';
    return;
  }

  transcriptionList.innerHTML = '';
  for (const item of listData) {
    appendListItem(item);
    previousStatuses[item.id] = item.status;
    if (item.status !== 'done' && item.status !== 'error') {
      startPolling(item.id);
    }
  }
}

function appendListItem(item) {
  const el = document.createElement('div');
  el.className = 'transcription-item';
  el.dataset.id = item.id;
  if (item.id === activeJobId) el.classList.add('active');
  updateListItemContent(el, item);

  el.addEventListener('click', (e) => {
    if (e.target.closest('.item-delete') || e.target.closest('.item-retry') || e.target.closest('.confirm-yes') || e.target.closest('.confirm-no')) return;
    selectJob(item.id);
  });

  transcriptionList.appendChild(el);
}

function computeQueuePosition(item) {
  if (item.status !== 'pending') return null;
  const pending = listData
    .filter(x => x.status === 'pending')
    .sort((a, b) => new Date(a.created_at) - new Date(b.created_at));
  const idx = pending.findIndex(x => x.id === item.id);
  if (idx < 0) return null;
  return { position: idx + 1, total: pending.length };
}

function updateListItemContent(el, item) {
  let statusLabel;
  if (item.status === 'done') statusLabel = 'Done';
  else if (item.status === 'error') statusLabel = 'Error';
  else if (item.status === 'extracting') statusLabel = 'Extracting…';
  else if (item.status === 'transcribing') statusLabel = `${item.progress}%`;
  else if (item.status === 'pending') {
    const q = computeQueuePosition(item);
    statusLabel = q && q.total > 1 ? `Queued #${q.position}` : 'Pending';
  } else {
    statusLabel = 'Pending';
  }

  let progressHtml = '';
  if (item.status === 'transcribing' || item.status === 'extracting') {
    progressHtml = `<div class="item-progress"><progress value="${item.progress}" max="100"></progress></div>`;
  }

  const metaParts = [formatTimeAgo(item.created_at)];
  if (item.duration_seconds) metaParts.push(formatDuration(item.duration_seconds));
  metaParts.push(`<span class="item-status-inline status-${item.status}">${statusLabel}</span>`);
  const metaHtml = metaParts.join(' <span class="meta-sep">•</span> ');

  const warningBadge = renderPostprocessWarning(item);

  // Primary right-hand action swaps between retry/rename based on state.
  let primaryActionHtml = '';
  if (item.status === 'error' && item.video_path) {
    primaryActionHtml = `<button class="item-action item-retry" title="Retry" onclick="window._retryJob('${item.id}', event)">
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 1 0 3-6.7"/><path d="M3 3v6h6"/></svg>
    </button>`;
  } else if (item.status === 'done') {
    primaryActionHtml = `<button class="item-action item-rename" title="Rename" onclick="window._startItemRename('${item.id}', event)">
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4z"/></svg>
    </button>`;
  }

  const deleteHtml = `<button class="item-action item-delete" title="Delete" onclick="window._deleteJob('${item.id}', event)">
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/></svg>
    </button>`;

  el.innerHTML = `
    <div class="item-info">
      <span class="item-filename" data-id="${item.id}">${escapeHtml(item.filename)}</span>
      <span class="item-meta">${metaHtml}</span>
    </div>
    <div class="item-right">
      ${progressHtml}
      ${warningBadge}
      <div class="item-actions">${primaryActionHtml}${deleteHtml}</div>
    </div>
  `;
}

function renderPostprocessBanner(data) {
  const banner = document.getElementById('postprocess-warning');
  if (!banner) return;
  if (data.status !== 'done') {
    banner.hidden = true;
    banner.innerHTML = '';
    return;
  }
  const items = [];
  if (data.recap_status === 'failed') {
    items.push({ label: 'Recap generation failed', retry: 'recap' });
  }
  if (data.speaker_id_status === 'failed') {
    items.push({ label: 'Speaker name identification failed — generic labels kept' });
  }
  if (data.enhancement_status === 'failed') {
    items.push({
      label: 'Video enhancement failed — original video kept',
      details: data.enhancement_error || null,
      hint: data.enhancement_error ? null : 'Details not captured on this run — re-upload to diagnose.',
    });
  }
  if (!items.length) {
    banner.hidden = true;
    banner.innerHTML = '';
    return;
  }
  banner.hidden = false;
  banner.innerHTML = items.map(it => {
    const retry = it.retry === 'recap'
      ? '<button class="postprocess-retry" data-retry="recap">Try again</button>'
      : '';
    const details = it.details
      ? `<details class="postprocess-details"><summary>Show details</summary><pre>${escapeHtml(it.details)}</pre></details>`
      : '';
    const hint = it.hint
      ? `<div class="postprocess-hint">${escapeHtml(it.hint)}</div>`
      : '';
    return `<div class="postprocess-warning-item"><span>${escapeHtml(it.label)}</span>${retry}</div>${details}${hint}`;
  }).join('');
  const retryBtn = banner.querySelector('[data-retry="recap"]');
  if (retryBtn) {
    retryBtn.addEventListener('click', () => {
      retryBtn.disabled = true;
      retryBtn.textContent = 'Generating...';
      // Trigger the existing recap flow in regenerate mode
      if (typeof window._openRecap === 'function') {
        window._openRecap(true);
      } else {
        document.getElementById('btn-recap')?.click();
      }
    });
  }
}

function renderPostprocessWarning(item) {
  if (item.status !== 'done') return '';
  const failures = [];
  if (item.recap_status === 'failed') failures.push('Recap');
  if (item.speaker_id_status === 'failed') failures.push('Speaker ID');
  if (item.enhancement_status === 'failed') failures.push('Video enhancement');
  if (!failures.length) return '';
  const tooltip = `${failures.join(', ')} failed — transcript is complete, but these extras did not finish. Click the item to see details.`;
  return `<span class="item-warn" role="img" aria-label="${escapeHtml(tooltip)}" title="${escapeHtml(tooltip)}">!</span>`;
}

function updateListItem(data) {
  const el = document.querySelector(`[data-id="${data.id}"]`);
  if (el) updateListItemContent(el, data);
}

// === Select / Show ===
async function selectJob(jobId) {
  activeJobId = jobId;
  document.querySelectorAll('.transcription-item').forEach(el => {
    el.classList.toggle('active', el.dataset.id === jobId);
  });

  const res = await fetch(`/api/transcriptions/${jobId}`);
  const data = await res.json();
  showTranscript(data);
}

function showTranscript(data) {
  activeRecord = data;
  transcriptSection.hidden = false;
  emptyState.hidden = true;
  transcriptTitle.textContent = data.filename;

  renderPostprocessBanner(data);

  // Video preview
  if (data.video_path && data.status === 'done') {
    videoContainer.hidden = false;
    videoUploadPrompt.hidden = true;
    videoPlayer.src = `/api/transcriptions/${data.id}/video`;
    previewTrack.src = `/api/transcriptions/${data.id}/vtt-inline`;
  } else if (data.status === 'done') {
    videoContainer.hidden = true;
    videoUploadPrompt.hidden = false;
  } else {
    videoContainer.hidden = true;
    videoUploadPrompt.hidden = true;
  }

  if (data.status === 'done') {
    renderTranscript(data, activeFormat);
    btnCopy.disabled = false;
    btnDownload.disabled = false;
    btnRecap.disabled = false;
  } else if (data.status === 'error') {
    renderError(data);
    btnCopy.disabled = true;
    btnDownload.disabled = true;
    btnRecap.disabled = true;
  } else {
    renderProcessing(data);
    btnCopy.disabled = true;
    btnDownload.disabled = true;
    btnRecap.disabled = true;
  }
}

// === Delete with confirmation ===
window._deleteJob = function(jobId, event) {
  event.stopPropagation();
  // Click may land on the SVG or an inner path — normalize to the button
  const btn = event.currentTarget || event.target.closest('button');
  if (!btn) return;

  if (btn.dataset.confirming === 'true') {
    doDelete(jobId);
    return;
  }

  // Preserve the SVG so we can restore it after the timeout/cancel
  if (!btn.dataset.originalHtml) {
    btn.dataset.originalHtml = btn.innerHTML;
  }
  btn.dataset.confirming = 'true';
  btn.innerHTML = '<span class="item-delete-confirm">Sure?</span>';
  btn.classList.add('confirming');

  clearTimeout(btn._cancelTimer);
  btn._cancelTimer = setTimeout(() => {
    btn.dataset.confirming = '';
    btn.innerHTML = btn.dataset.originalHtml;
    btn.classList.remove('confirming');
  }, 3000);
};

async function doDelete(jobId) {
  const el = document.querySelector(`[data-id="${jobId}"]`);
  if (el) {
    el.classList.add('removing');
    el.addEventListener('animationend', () => el.remove());
  }

  await fetch(`/api/transcriptions/${jobId}`, { method: 'DELETE' });
  pollingJobs.delete(jobId);

  if (activeJobId === jobId) {
    activeJobId = null;
    activeRecord = null;
    transcriptSection.hidden = true;
    emptyState.hidden = false;
    clearTranscript();
  }
}

// === Retry ===
window._retryJob = async function(jobId, event) {
  event.stopPropagation();
  await fetch(`/api/transcriptions/${jobId}/retry`, { method: 'POST' });
  startPolling(jobId);
  refreshList();
  showToast('Retrying transcription...', 'info');
};

// Inline rename from the sidebar list. Replaces the filename span with an input.
window._startItemRename = function(jobId, event) {
  event.stopPropagation();
  const item = document.querySelector(`.transcription-item[data-id="${jobId}"]`);
  if (!item) return;
  const nameEl = item.querySelector('.item-filename');
  if (!nameEl) return;
  const original = nameEl.textContent;

  const input = document.createElement('input');
  input.type = 'text';
  input.className = 'item-filename-input';
  input.value = original;
  input.addEventListener('click', (e) => e.stopPropagation());

  const finish = async (commit) => {
    const next = input.value.trim();
    input.replaceWith(nameEl);
    nameEl.textContent = original;
    if (!commit || !next || next === original) return;
    try {
      const body = new URLSearchParams({ filename: next });
      const res = await fetch(`/api/transcriptions/${jobId}`, { method: 'PATCH', body });
      if (!res.ok) throw new Error('Rename failed');
      const data = await res.json();
      nameEl.textContent = data.filename || next;
      // Sync the detail-view title if this is the open transcript
      if (jobId === activeJobId && activeRecord) {
        activeRecord.filename = data.filename || next;
        if (typeof transcriptTitle !== 'undefined' && transcriptTitle) {
          transcriptTitle.textContent = activeRecord.filename;
        }
      }
    } catch (e) {
      showToast('Could not rename. Try again.', 'error');
    }
  };

  input.addEventListener('keydown', (e) => {
    e.stopPropagation();
    if (e.key === 'Enter') { e.preventDefault(); finish(true); }
    if (e.key === 'Escape') { finish(false); }
  });
  input.addEventListener('blur', () => finish(true));

  nameEl.replaceWith(input);
  input.focus();
  input.select();
};

// === Rename ===
function initRename() {
  const titleEl = transcriptTitle;

  function startRename() {
    if (!activeJobId || !activeRecord) return;
    titleEl.hidden = true;
    btnRename.hidden = true;
    renameInput.hidden = false;
    renameInput.value = activeRecord.filename;
    renameInput.focus();
    renameInput.select();
  }

  function cancelRename() {
    renameInput.hidden = true;
    titleEl.hidden = false;
    btnRename.hidden = false;
  }

  async function commitRename() {
    const newName = renameInput.value.trim();
    if (!newName || !activeJobId) { cancelRename(); return; }
    if (newName === activeRecord.filename) { cancelRename(); return; }

    const formData = new FormData();
    formData.append('filename', newName);
    await fetch(`/api/transcriptions/${activeJobId}`, { method: 'PATCH', body: formData });

    activeRecord.filename = newName;
    titleEl.textContent = newName;
    cancelRename();
    refreshList();
    showToast('Renamed', 'success');
  }

  titleEl.addEventListener('click', startRename);
  btnRename.addEventListener('click', startRename);

  renameInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); commitRename(); }
    if (e.key === 'Escape') cancelRename();
  });

  renameInput.addEventListener('blur', commitRename);
}

// === Video Upload ===
function initVideoUpload() {
  btnUploadVideo.addEventListener('click', () => videoFileInput.click());
  videoFileInput.addEventListener('change', async () => {
    if (!videoFileInput.files.length || !activeJobId) return;
    const formData = new FormData();
    formData.append('file', videoFileInput.files[0]);
    await fetch(`/api/transcriptions/${activeJobId}/video`, { method: 'POST', body: formData });
    videoFileInput.value = '';
    showToast('Video uploaded for preview', 'success');
    // Refresh to show player
    const res = await fetch(`/api/transcriptions/${activeJobId}`);
    const data = await res.json();
    showTranscript(data);
  });
}

// === Search ===
const globalSearch = document.getElementById('global-search');
const searchOverlay = document.getElementById('search-overlay');
const searchOverlayInput = document.getElementById('search-overlay-input');
const searchResults = document.getElementById('search-results');

globalSearch.addEventListener('focus', () => openSearch());

function openSearch() {
  searchOverlay.hidden = false;
  searchOverlayInput.value = '';
  searchOverlayInput.focus();
  searchResults.innerHTML = '<p class="search-empty">Type to search across all transcriptions</p>';
}

function closeSearch() {
  searchOverlay.hidden = true;
}

searchOverlay.addEventListener('click', (e) => {
  if (e.target === searchOverlay) closeSearch();
});

let searchDebounce;
searchOverlayInput.addEventListener('input', () => {
  clearTimeout(searchDebounce);
  searchDebounce = setTimeout(() => doGlobalSearch(searchOverlayInput.value), 300);
});

searchOverlayInput.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') closeSearch();
  if (e.key === 'Enter') {
    const focused = searchResults.querySelector('.search-result.focused') || searchResults.querySelector('.search-result');
    if (focused) focused.click();
  }
  if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
    e.preventDefault();
    navigateSearchResults(e.key === 'ArrowDown' ? 1 : -1);
  }
});

async function doGlobalSearch(query) {
  if (!query || query.length < 2) {
    searchResults.innerHTML = '<p class="search-empty">Type to search across all transcriptions</p>';
    return;
  }

  const res = await fetch(`/api/search?q=${encodeURIComponent(query)}`);
  const results = await res.json();

  if (results.length === 0) {
    searchResults.innerHTML = '<p class="search-empty">No results found</p>';
    return;
  }

  searchResults.innerHTML = '';
  for (const result of results) {
    for (const match of result.matches.slice(0, 3)) {
      const el = document.createElement('div');
      el.className = 'search-result';
      const snippet = highlightSnippet(match.text, query);
      el.innerHTML = `
        <div class="search-result-filename">${escapeHtml(result.filename)}</div>
        <div class="search-result-snippet">${snippet}</div>
      `;
      el.addEventListener('click', () => {
        closeSearch();
        selectJob(result.id);
      });
      searchResults.appendChild(el);
    }
  }
}

function navigateSearchResults(direction) {
  const items = searchResults.querySelectorAll('.search-result');
  if (!items.length) return;
  const current = searchResults.querySelector('.search-result.focused');
  let idx = current ? Array.from(items).indexOf(current) : -1;
  if (current) current.classList.remove('focused');
  idx = (idx + direction + items.length) % items.length;
  items[idx].classList.add('focused');
  items[idx].scrollIntoView({ block: 'nearest' });
}

function highlightSnippet(text, query) {
  const escaped = escapeHtml(text.trim());
  const regex = new RegExp(`(${escapeRegex(query)})`, 'gi');
  return escaped.replace(regex, '<mark>$1</mark>');
}

// === Keyboard Shortcuts ===
function initShortcuts() {
  const shortcutsModal = document.getElementById('shortcuts-modal');
  const shortcutsBtn = document.getElementById('shortcuts-btn');

  shortcutsBtn.addEventListener('click', () => {
    shortcutsModal.hidden = !shortcutsModal.hidden;
  });

  shortcutsModal.addEventListener('click', (e) => {
    if (e.target === shortcutsModal || e.target.closest('.modal-close')) {
      shortcutsModal.hidden = true;
    }
  });

  document.addEventListener('keydown', (e) => {
    // Skip if typing in an input
    if (e.target.matches('input, textarea, select, [contenteditable]')) {
      if (e.key === 'Escape') {
        e.target.blur();
        if (!searchOverlay.hidden) closeSearch();
      }
      return;
    }

    const mod = e.metaKey || e.ctrlKey;

    if (mod && e.key === 'k') { e.preventDefault(); openSearch(); return; }
    if (e.key === 'Escape') {
      if (!shortcutsModal.hidden) { shortcutsModal.hidden = true; return; }
      if (!searchOverlay.hidden) { closeSearch(); return; }
      if (activeJobId) {
        activeJobId = null;
        activeRecord = null;
        transcriptSection.hidden = true;
        emptyState.hidden = false;
        clearTranscript();
        document.querySelectorAll('.transcription-item.active').forEach(el => el.classList.remove('active'));
      }
      return;
    }

    if (e.key === '?' || (e.shiftKey && e.key === '/')) { shortcutsModal.hidden = !shortcutsModal.hidden; return; }
    if (e.key === 'd' || e.key === 'D') { toggleTheme(); return; }
    if (e.key === 'c' || e.key === 'C') {
      // Copy only makes sense when viewing a transcript
      if (!btnCopy.disabled) btnCopy.click();
      return;
    }
    if (e.key === 'u' || e.key === 'U') {
      // Upload — open the hidden file picker
      const fi = document.getElementById('file-input');
      if (fi) fi.click();
      return;
    }
    if (e.key === 'a' || e.key === 'A') {
      // AI Assistant — click the topbar button (handles free/plus gating)
      const ab = document.getElementById('assistant-btn');
      if (ab) ab.click();
      return;
    }
    if (mod && e.key === 'f') {
      // Find in transcript — focus the in-transcript search if visible
      const s = document.getElementById('transcript-search-input');
      if (s && !s.closest('.transcript-search').hidden) {
        e.preventDefault();
        s.focus();
        s.select();
        return;
      }
    }

    if (e.key === 'j' || e.key === 'k') {
      e.preventDefault();
      navigateList(e.key === 'j' ? 1 : -1);
      return;
    }

    if (e.key === 'Enter' && activeJobId) {
      selectJob(activeJobId);
      return;
    }
  });
}

function setFormat(index) {
  const tabs = Array.from(formatTabs);
  if (tabs[index]) tabs[index].click();
}

function navigateList(direction) {
  const items = document.querySelectorAll('.transcription-item');
  if (!items.length) return;

  let currentIdx = -1;
  items.forEach((el, i) => { if (el.classList.contains('active')) currentIdx = i; });

  const nextIdx = currentIdx === -1 ? 0 : Math.max(0, Math.min(items.length - 1, currentIdx + direction));
  const jobId = items[nextIdx].dataset.id;
  selectJob(jobId);
}

// === Modals ===
function initModals() {
  const sidebarToggle = document.getElementById('sidebar-toggle');
  const themeToggle = document.getElementById('theme-toggle');

  sidebarToggle.addEventListener('click', () => {
    const sidebar = document.getElementById('sidebar');
    if (window.innerWidth <= 768) {
      sidebar.classList.toggle('open');
    } else {
      document.body.classList.toggle('sidebar-collapsed');
    }
  });

  themeToggle.addEventListener('click', toggleTheme);
}

// === Recap Email ===
function closeRecapModal() {
  recapModal.hidden = true;
  recapModal.style.display = 'none';
}

function openRecapModal() {
  recapModal.hidden = false;
  recapModal.style.display = '';
}

function initRecap() {
  btnRecap.addEventListener('click', () => generateRecap());

  // Close on overlay click or X button
  recapModal.addEventListener('mousedown', (e) => {
    if (e.target === recapModal) closeRecapModal();
  });
  recapModal.querySelector('.modal-close').addEventListener('click', closeRecapModal);

  // Close on Escape
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !recapModal.hidden) closeRecapModal();
  });

  recapCopy.addEventListener('click', async () => {
    const text = `Subject: ${recapSubject.value}\n\n${recapContent.value}`;
    await navigator.clipboard.writeText(text);
    const span = recapCopy.querySelector('svg + *') || recapCopy;
    const orig = recapCopy.textContent;
    recapCopy.textContent = 'Copied!';
    setTimeout(() => { recapCopy.textContent = orig; }, 1500);
  });

  recapRegenerate.addEventListener('click', () => openGuidancePanel());
  recapGuidanceCancel.addEventListener('click', () => closeGuidancePanel());
  recapGuidanceSubmit.addEventListener('click', () => {
    const guidance = recapGuidance.value.trim();
    closeGuidancePanel();
    generateRecap(true, guidance);
  });

  recapSend.addEventListener('click', sendRecapEmail);

  window._openRecap = generateRecap;
}

function openGuidancePanel() {
  recapEditor.hidden = true;
  recapActions.hidden = true;
  recapGuidancePanel.hidden = false;
  recapGuidance.value = '';
  recapGuidance.focus();
}

function closeGuidancePanel() {
  recapGuidancePanel.hidden = true;
  recapEditor.hidden = false;
  recapActions.hidden = false;
}

async function generateRecap(regenerate = false, guidance = '') {
  if (!activeJobId) return;
  openRecapModal();
  recapGuidancePanel.hidden = true;

  // Fast path: the recap was pre-generated during post-processing. Open the
  // editor immediately so the user sees real content, not a spinner.
  if (!regenerate && activeRecord?.recap) {
    populateRecapEditor(activeRecord.recap);
    return;
  }

  recapLoading.hidden = false;
  recapEditor.hidden = true;
  recapActions.hidden = true;

  try {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 45000);

    const url = `/api/transcriptions/${activeJobId}/recap${regenerate ? '?regenerate=true' : ''}`;
    const body = new FormData();
    if (guidance) body.append('guidance', guidance);
    const res = await fetch(url, { method: 'POST', body, signal: controller.signal });
    clearTimeout(timeout);

    const data = await res.json();

    if (!res.ok) {
      showToast(data.detail || 'Failed to generate recap', 'error');
      closeRecapModal();
      return;
    }

    // Cache on the in-memory record so subsequent opens are instant too
    if (activeRecord) activeRecord.recap = data.recap;
    populateRecapEditor(data.recap);
  } catch (e) {
    const msg = e.name === 'AbortError' ? 'Recap generation timed out' : 'Failed to generate recap';
    showToast(msg, 'error');
    closeRecapModal();
  }
}

function populateRecapEditor(recap) {
  recap = recap || '';
  // Match "Subject: ..." or "**Subject:** ..." on its own line
  const subjectRegex = /^(?:\*\*)?Subject(?:\s*[Ll]ine)?:?\*?\*?\s*(.+)$/m;
  const subjectMatch = recap.match(subjectRegex);
  if (subjectMatch) {
    recapSubject.value = subjectMatch[1].trim();
    // Remove the entire subject line from the body
    recapContent.value = recap.replace(subjectRegex, '').replace(/^\n+/, '').trim();
  } else {
    recapSubject.value = `Recap: ${activeRecord?.filename || 'Meeting'}`;
    recapContent.value = recap;
  }

  recapTo.value = '';
  recapLoading.hidden = true;
  recapEditor.hidden = false;
  recapActions.hidden = false;
  recapTo.focus();
}

async function sendRecapEmail() {
  const to = recapTo.value.trim();
  if (!to) {
    showToast('Please enter a recipient email address', 'error');
    recapTo.focus();
    return;
  }

  const sendBtn = recapSend;
  const origText = sendBtn.innerHTML;
  sendBtn.disabled = true;
  sendBtn.textContent = 'Sending...';

  try {
    const formData = new FormData();
    formData.append('to', to);
    formData.append('subject', recapSubject.value);
    formData.append('body', recapContent.value);

    const res = await fetch('/api/send-email', { method: 'POST', body: formData });
    const data = await res.json();

    if (res.ok) {
      showToast(`Email sent to ${to}`, 'success');
      recapModal.hidden = true;
    } else {
      showToast(data.detail || 'Failed to send email', 'error');
    }
  } catch (e) {
    showToast('Failed to send email', 'error');
  } finally {
    sendBtn.disabled = false;
    sendBtn.innerHTML = origText;
  }
}

// === Helpers ===
function escapeHtml(str) {
  const el = document.createElement('span');
  el.textContent = str;
  return el.innerHTML;
}

function escapeRegex(str) {
  return str.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function formatTimeAgo(isoString) {
  const date = new Date(isoString);
  const now = new Date();
  const diffMs = now - date;
  const diffMin = Math.floor(diffMs / 60000);
  if (diffMin < 1) return 'just now';
  if (diffMin < 60) return `${diffMin}m ago`;
  const diffHr = Math.floor(diffMin / 60);
  if (diffHr < 24) return `${diffHr}h ago`;
  const diffDay = Math.floor(diffHr / 24);
  return `${diffDay}d ago`;
}

function formatDuration(seconds) {
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.round(seconds % 60);
  if (h > 0) return `${h}h ${m}m`;
  return m > 0 ? `${m}m ${s}s` : `${s}s`;
}

// Periodic list refresh
setInterval(refreshList, 15000);
