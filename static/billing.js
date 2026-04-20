import { showToast } from './toast.js';

// In-app "Manage billing" link: POST to /api/billing/portal and redirect
// to Stripe's hosted Customer Portal (update card, cancel, invoices).
const manageBtn = document.getElementById('plan-manage-billing');
if (manageBtn) {
  manageBtn.addEventListener('click', async (e) => {
    e.preventDefault();
    const originalText = manageBtn.textContent;
    manageBtn.textContent = 'Opening…';
    manageBtn.style.pointerEvents = 'none';
    manageBtn.style.opacity = '0.7';
    try {
      const res = await fetch('/api/billing/portal', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      window.location.href = data.url;
    } catch (err) {
      manageBtn.textContent = originalText;
      manageBtn.style.pointerEvents = '';
      manageBtn.style.opacity = '';
      showToast('Could not open billing portal. Please try again.', 'error');
      console.error(err);
    }
  });
}

// Post-checkout return from Stripe: Checkout success_url redirects to
// /app?upgraded=1. Webhook flips plan to 'plus' asynchronously — usually
// within 1-2 seconds — so we poll /api/config and reload once it lands.
const params = new URLSearchParams(window.location.search);
if (params.get('upgraded') === '1') {
  window.history.replaceState({}, '', '/app');
  showToast('Payment received. Activating Plus…', 'success');

  let attempts = 0;
  const maxAttempts = 15; // ~15s total
  const poll = setInterval(async () => {
    attempts++;
    try {
      const res = await fetch('/api/config', { credentials: 'same-origin' });
      if (res.ok) {
        const data = await res.json();
        if (data.plan && data.plan.tier === 'plus') {
          clearInterval(poll);
          // Reload so the server-rendered plan widget + limits refresh.
          window.location.reload();
          return;
        }
      }
    } catch (err) {
      // swallow — transient network errors are fine during polling
    }
    if (attempts >= maxAttempts) {
      clearInterval(poll);
      showToast('Payment received — your plan will update shortly. Try refreshing in a moment.', 'info');
    }
  }, 1000);
}
