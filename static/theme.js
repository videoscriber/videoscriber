export function initTheme() {
  // Always default to dark. Only honor a saved preference from a previous visit.
  const saved = localStorage.getItem('theme');
  applyTheme(saved || 'dark');
}

export function toggleTheme() {
  const current = document.documentElement.getAttribute('data-theme');
  applyTheme(current === 'dark' ? 'light' : 'dark');
}

function applyTheme(theme) {
  document.documentElement.setAttribute('data-theme', theme);
  localStorage.setItem('theme', theme);
}
