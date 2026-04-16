const container = document.getElementById('toast-container');

export function showToast(message, type = 'info') {
  const icons = { success: '\u2713', error: '\u2717', info: '\u2139' };
  const toast = document.createElement('div');
  toast.className = `toast toast-${type}`;
  toast.innerHTML = `
    <span class="toast-icon">${icons[type] || icons.info}</span>
    <span class="toast-message">${escapeHtml(message)}</span>
    <button class="toast-close" aria-label="Dismiss">&times;</button>
    <div class="toast-progress"></div>
  `;
  toast.querySelector('.toast-close').onclick = () => dismiss(toast);
  container.appendChild(toast);
  setTimeout(() => dismiss(toast), 4000);
}

function dismiss(toast) {
  if (toast.classList.contains('leaving')) return;
  toast.classList.add('leaving');
  toast.addEventListener('animationend', () => toast.remove());
}

function escapeHtml(str) {
  const el = document.createElement('span');
  el.textContent = str;
  return el.innerHTML;
}
