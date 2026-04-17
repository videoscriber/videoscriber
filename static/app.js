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
btnCopy.addEventListener('click', async () => {
  if (!activeRecord) return;
  const fieldMap = { segments: 'transcript_text', text: 'transcript_text', srt: 'transcript_srt', vtt: 'transcript_vtt' };
  const text = activeRecord[fieldMap[activeFormat]] || '';
  await navigator.clipboard.writeText(text);
  const span = btnCopy.querySelector('span');
  const orig = span.textContent;
  span.textContent = 'Copied!';
  setTimeout(() => { span.textContent = orig; }, 1500);
});

// Download
btnDownload.addEventListener('click', () => {
  if (!activeJobId) return;
  const fmt = activeFormat === 'segments' ? 'txt' : activeFormat === 'text' ? 'txt' : activeFormat;
  window.location.href = `/api/transcriptions/${activeJobId}/download/${fmt}`;
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

    updateListItem(data);

    // Check for status transitions
    const prev = previousStatuses[jobId];
    if (prev && prev !== 'done' && data.status === 'done') {
      showToast(`${data.filename} transcription complete!`, 'success');
    }
    if (prev && prev !== 'error' && data.status === 'error') {
      showToast(`${data.filename} failed: ${data.error_message || 'Unknown error'}`, 'error');
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

function updateListItemContent(el, item) {
  const statusLabel = item.status === 'done' ? 'Done' :
    item.status === 'error' ? 'Error' :
    item.status === 'extracting' ? 'Extracting...' :
    item.status === 'transcribing' ? `${item.progress}%` :
    'Pending';

  let progressHtml = '';
  if (item.status === 'transcribing' || item.status === 'extracting') {
    progressHtml = `<div class="item-progress"><progress value="${item.progress}" max="100"></progress></div>`;
  }

  let actionsHtml = '';
  if (item.status === 'error' && item.video_path) {
    actionsHtml += `<button class="item-retry" onclick="window._retryJob('${item.id}', event)">Retry</button>`;
  }
  actionsHtml += `<button class="item-delete" title="Delete" onclick="window._deleteJob('${item.id}', event)">&times;</button>`;

  const warningBadge = renderPostprocessWarning(item);

  el.innerHTML = `
    <div class="item-info">
      <span class="item-filename">${escapeHtml(item.filename)}</span>
      <span class="item-meta">
        ${formatTimeAgo(item.created_at)}${item.duration_seconds ? ' \u2022 ' + formatDuration(item.duration_seconds) : ''}
      </span>
    </div>
    <div class="item-right">
      ${progressHtml}
      ${warningBadge}
      <span class="item-status status-${item.status}">${statusLabel}</span>
      <div class="item-actions">${actionsHtml}</div>
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
    items.push({ label: 'Video enhancement failed — original video kept' });
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
    return `<div class="postprocess-warning-item"><span>${escapeHtml(it.label)}</span>${retry}</div>`;
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
  const btn = event.target;

  if (btn.dataset.confirming === 'true') {
    // Confirmed
    doDelete(jobId);
    return;
  }

  btn.dataset.confirming = 'true';
  btn.textContent = 'Sure?';
  btn.classList.add('confirming');

  const timer = setTimeout(() => {
    btn.dataset.confirming = '';
    btn.textContent = '\u00d7';
    btn.classList.remove('confirming');
  }, 3000);

  btn._cancelTimer = timer;
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

  recapRegenerate.addEventListener('click', () => generateRecap(true));

  recapSend.addEventListener('click', sendRecapEmail);

  window._openRecap = generateRecap;
}

async function generateRecap(regenerate = false) {
  if (!activeJobId) return;
  openRecapModal();

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
    const timeout = setTimeout(() => controller.abort(), 30000);

    const url = `/api/transcriptions/${activeJobId}/recap${regenerate ? '?regenerate=true' : ''}`;
    const res = await fetch(url, { method: 'POST', signal: controller.signal });
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
