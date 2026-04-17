import { showToast } from './toast.js';

const dropZone = document.getElementById('drop-zone');
const fileInput = document.getElementById('file-input');
const uploadQueue = document.getElementById('upload-queue');

let onUploadComplete = null;
// Diarization runs automatically whenever the server has it configured.
let diarizationAvailable = false;

export async function initUpload(callbacks) {
  onUploadComplete = callbacks.onComplete;

  try {
    const res = await fetch('/api/config');
    const config = await res.json();
    diarizationAvailable = !!config.diarization_available;
  } catch (e) {
    console.warn('Config check failed:', e);
  }

  // === Sidebar drop zone ===
  dropZone.addEventListener('click', (e) => {
    e.stopPropagation();
    fileInput.click();
  });
  dropZone.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); fileInput.click(); }
  });
  dropZone.addEventListener('dragover', (e) => { e.preventDefault(); dropZone.classList.add('dragover'); });
  dropZone.addEventListener('dragleave', () => dropZone.classList.remove('dragover'));
  dropZone.addEventListener('drop', (e) => {
    e.preventDefault();
    dropZone.classList.remove('dragover');
    if (e.dataTransfer.files.length) uploadFiles(e.dataTransfer.files);
  });
  fileInput.addEventListener('change', () => {
    if (fileInput.files.length) uploadFiles(fileInput.files);
    fileInput.value = '';
  });

  // === Empty state drop zone ===
  const emptyState = document.getElementById('empty-state');
  const emptyInput = document.getElementById('empty-state-input');
  if (emptyState && emptyInput) {
    emptyState.addEventListener('click', (e) => {
      if (e.target.closest('input')) return;
      emptyInput.click();
    });
    emptyState.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); emptyInput.click(); }
    });
    emptyState.addEventListener('dragover', (e) => { e.preventDefault(); emptyState.classList.add('dragover'); });
    emptyState.addEventListener('dragleave', () => emptyState.classList.remove('dragover'));
    emptyState.addEventListener('drop', (e) => {
      e.preventDefault();
      emptyState.classList.remove('dragover');
      if (e.dataTransfer.files.length) uploadFiles(e.dataTransfer.files);
    });
    emptyInput.addEventListener('change', () => {
      if (emptyInput.files.length) uploadFiles(emptyInput.files);
      emptyInput.value = '';
    });
  }

  // === Also accept drops anywhere on the page ===
  document.body.addEventListener('dragover', (e) => { e.preventDefault(); });
  document.body.addEventListener('drop', (e) => {
    e.preventDefault();
    if (e.dataTransfer.files.length) uploadFiles(e.dataTransfer.files);
  });
}

function uploadFiles(fileList) {
  for (const file of fileList) {
    uploadSingleFile(file);
  }
}

function uploadSingleFile(file) {
  const id = crypto.randomUUID();
  const el = document.createElement('div');
  el.className = 'upload-queue-item';
  el.id = `upload-${id}`;
  el.innerHTML = `
    <span class="filename">${escapeHtml(file.name)}</span>
    <progress value="0" max="100"></progress>
    <span class="status">0%</span>
  `;
  uploadQueue.appendChild(el);

  const progress = el.querySelector('progress');
  const status = el.querySelector('.status');

  const formData = new FormData();
  formData.append('files', file);
  if (diarizationAvailable) {
    formData.append('diarize', 'true');
  }

  const xhr = new XMLHttpRequest();
  xhr.open('POST', '/api/upload');

  xhr.upload.onprogress = (e) => {
    if (e.lengthComputable) {
      const pct = Math.round((e.loaded / e.total) * 100);
      progress.value = pct;
      status.textContent = `${pct}%`;
    }
  };

  xhr.onload = () => {
    if (xhr.status === 200) {
      const data = JSON.parse(xhr.responseText);
      el.classList.add('complete');
      status.textContent = 'Done';
      setTimeout(() => el.remove(), 2000);
      if (onUploadComplete) {
        const jobs = Array.isArray(data) ? data : [data];
        jobs.forEach(j => onUploadComplete(j.id));
      }
    } else {
      let msg = 'Upload failed';
      try { msg = JSON.parse(xhr.responseText).detail; } catch {}
      status.textContent = 'Error';
      showToast(`${file.name}: ${msg}`, 'error');
      setTimeout(() => el.remove(), 3000);
    }
  };

  xhr.onerror = () => {
    status.textContent = 'Error';
    showToast(`${file.name}: Network error`, 'error');
    setTimeout(() => el.remove(), 3000);
  };

  xhr.send(formData);
}

function escapeHtml(str) {
  const el = document.createElement('span');
  el.textContent = str;
  return el.innerHTML;
}
