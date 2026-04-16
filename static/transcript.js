const segmentsView = document.getElementById('segments-view');
const rawView = document.getElementById('raw-view');
const videoPlayer = document.getElementById('preview-player');
const transcriptSearch = document.getElementById('transcript-search');
const transcriptSearchInput = document.getElementById('transcript-search-input');
const matchCount = document.getElementById('match-count');
const matchPrev = document.getElementById('match-prev');
const matchNext = document.getElementById('match-next');

let currentSegments = [];
let searchMatches = [];
let currentMatchIndex = -1;
let userScrolling = false;
let scrollTimeout = null;

export function renderTranscript(record, format) {
  const content = document.getElementById('transcript-content');
  const processingState = document.getElementById('processing-state');
  const errorState = document.getElementById('error-state');

  content.hidden = false;
  processingState.hidden = true;
  errorState.hidden = true;

  if (format === 'segments') {
    segmentsView.hidden = false;
    rawView.hidden = true;
    transcriptSearch.hidden = false;
    renderSegments(record);
  } else {
    segmentsView.hidden = true;
    rawView.hidden = false;
    transcriptSearch.hidden = true;
    const fieldMap = { text: 'transcript_text', srt: 'transcript_srt', vtt: 'transcript_vtt' };
    rawView.textContent = record[fieldMap[format]] || '';
  }
}

export function renderProcessing(record) {
  const content = document.getElementById('transcript-content');
  const processingState = document.getElementById('processing-state');
  const errorState = document.getElementById('error-state');

  content.hidden = true;
  processingState.hidden = false;
  errorState.hidden = true;

  const status = document.getElementById('processing-status');
  const progress = document.getElementById('processing-progress');
  const progressBar = document.getElementById('processing-progress-bar');
  const detail = document.getElementById('processing-detail');

  const label = record.status === 'extracting' ? 'Extracting audio...' : 'Transcribing...';
  status.textContent = label;
  progress.value = record.progress;
  if (progressBar) progressBar.style.width = `${record.progress}%`;

  let detailText = `${record.progress}%`;
  if (record.total_chunks && record.completed_chunks !== null) {
    detailText = `Chunk ${record.completed_chunks}/${record.total_chunks}`;
    if (record.processing_started_at && record.completed_chunks > 0) {
      const elapsed = (Date.now() - new Date(record.processing_started_at).getTime()) / 1000;
      const rate = record.completed_chunks / elapsed;
      const remaining = (record.total_chunks - record.completed_chunks) / rate;
      detailText += ` \u2022 ~${formatTime(remaining)} remaining`;
    }
  }
  detail.textContent = detailText;
}

export function renderError(record) {
  const content = document.getElementById('transcript-content');
  const processingState = document.getElementById('processing-state');
  const errorState = document.getElementById('error-state');

  content.hidden = true;
  processingState.hidden = true;
  errorState.hidden = false;

  document.getElementById('error-message').textContent = record.error_message || 'Unknown error';

  const retryBtn = document.getElementById('btn-retry');
  retryBtn.hidden = !record.video_path;
}

export function clearTranscript() {
  currentSegments = [];
  segmentsView.innerHTML = '';
  rawView.textContent = '';
}

function renderSegments(record) {
  currentSegments = [];
  segmentsView.innerHTML = '';

  if (!record.transcript_segments_json) {
    // Fallback: show plain text if no segments
    segmentsView.innerHTML = `<div class="segment"><span class="segment-text">${escapeHtml(record.transcript_text || '')}</span></div>`;
    return;
  }

  try {
    currentSegments = JSON.parse(record.transcript_segments_json);
  } catch {
    segmentsView.innerHTML = `<div class="segment"><span class="segment-text">${escapeHtml(record.transcript_text || '')}</span></div>`;
    return;
  }

  const frag = document.createDocumentFragment();
  const speakerMap = {};
  let speakerIndex = 0;

  for (const seg of currentSegments) {
    const div = document.createElement('div');
    div.className = 'segment';
    div.dataset.start = seg.start;
    div.dataset.end = seg.end;

    const ts = document.createElement('button');
    ts.className = 'segment-timestamp';
    ts.textContent = formatTimestamp(seg.start);
    ts.setAttribute('aria-label', `Seek to ${formatTimestamp(seg.start)}`);
    ts.onclick = () => seekVideo(seg.start);
    div.appendChild(ts);

    if (seg.speaker) {
      if (!(seg.speaker in speakerMap)) {
        speakerMap[seg.speaker] = speakerIndex++;
      }
      const pill = document.createElement('span');
      pill.className = `segment-speaker speaker-${speakerMap[seg.speaker] % 8}`;
      pill.textContent = seg.speaker;
      div.appendChild(pill);
    }

    const text = document.createElement('span');
    text.className = 'segment-text';
    text.textContent = seg.text.trim();
    div.appendChild(text);

    frag.appendChild(div);
  }

  segmentsView.appendChild(frag);
}

function seekVideo(seconds) {
  if (videoPlayer && videoPlayer.src) {
    videoPlayer.currentTime = seconds;
    videoPlayer.play();
  }
}

export function initVideoSync() {
  const scrollContainer = document.getElementById('transcript-content');

  videoPlayer.addEventListener('timeupdate', () => {
    if (userScrolling || !scrollContainer) return;
    const t = videoPlayer.currentTime;
    const segments = segmentsView.querySelectorAll('.segment');
    segments.forEach(seg => {
      const start = parseFloat(seg.dataset.start);
      const end = parseFloat(seg.dataset.end);
      const isActive = t >= start && t < end;
      seg.classList.toggle('active', isActive);
      if (isActive) {
        // Scroll the active segment to the top of the transcript scroll area
        const containerTop = scrollContainer.getBoundingClientRect().top;
        const segTop = seg.getBoundingClientRect().top;
        const offset = segTop - containerTop + scrollContainer.scrollTop;
        scrollContainer.scrollTo({ top: offset - 8, behavior: 'smooth' });
      }
    });
  });

  // Pause auto-scroll when user manually scrolls
  scrollContainer?.addEventListener('scroll', () => {
    if (videoPlayer.paused) return;
    userScrolling = true;
    clearTimeout(scrollTimeout);
    scrollTimeout = setTimeout(() => { userScrolling = false; }, 3000);
  }, { passive: true });
}

// In-transcript search
export function initTranscriptSearch() {
  let debounce;
  transcriptSearchInput.addEventListener('input', () => {
    clearTimeout(debounce);
    debounce = setTimeout(() => searchInTranscript(transcriptSearchInput.value), 200);
  });

  matchPrev.addEventListener('click', () => navigateMatch(-1));
  matchNext.addEventListener('click', () => navigateMatch(1));
}

function searchInTranscript(query) {
  searchMatches = [];
  currentMatchIndex = -1;

  const segments = segmentsView.querySelectorAll('.segment');
  segments.forEach(seg => {
    const textEl = seg.querySelector('.segment-text');
    if (!textEl) return;
    // Restore original text
    textEl.textContent = textEl.textContent;
  });

  if (!query || query.length < 2) {
    matchCount.textContent = '';
    return;
  }

  const queryLower = query.toLowerCase();
  segments.forEach((seg, i) => {
    const textEl = seg.querySelector('.segment-text');
    if (!textEl) return;
    const text = textEl.textContent;
    if (text.toLowerCase().includes(queryLower)) {
      searchMatches.push({ index: i, element: seg, textEl });
      // Highlight
      const regex = new RegExp(`(${escapeRegex(query)})`, 'gi');
      textEl.innerHTML = text.replace(regex, '<mark>$1</mark>');
    }
  });

  matchCount.textContent = searchMatches.length > 0
    ? `${searchMatches.length} match${searchMatches.length > 1 ? 'es' : ''}`
    : 'No matches';

  if (searchMatches.length > 0) navigateMatch(1);
}

function navigateMatch(direction) {
  if (searchMatches.length === 0) return;
  currentMatchIndex = (currentMatchIndex + direction + searchMatches.length) % searchMatches.length;
  searchMatches.forEach(m => m.element.classList.remove('active'));
  const match = searchMatches[currentMatchIndex];
  match.element.classList.add('active');
  match.element.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  matchCount.textContent = `${currentMatchIndex + 1} of ${searchMatches.length}`;
}

// Helpers
function formatTimestamp(seconds) {
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${m}:${s.toString().padStart(2, '0')}`;
}

function formatTime(seconds) {
  if (seconds < 60) return `${Math.ceil(seconds)}s`;
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  return m > 0 && s > 0 ? `${m}m ${s}s` : `${m}m`;
}

function escapeHtml(str) {
  const el = document.createElement('span');
  el.textContent = str;
  return el.innerHTML;
}

function escapeRegex(str) {
  return str.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}
