import { showToast } from './toast.js';

// In-app "Upgrade to Plus" link on the Free plan widget: skip the /upgrade
// comparison page and go straight to Stripe Checkout (monthly). The anchor's
// href="/upgrade" is preserved as a no-JS fallback and as the error path.
const upgradeQuick = document.getElementById('plan-upgrade-quick');
if (upgradeQuick) {
  upgradeQuick.addEventListener('click', async (e) => {
    e.preventDefault();
    const originalText = upgradeQuick.textContent;
    upgradeQuick.textContent = 'Opening checkout…';
    upgradeQuick.style.pointerEvents = 'none';
    upgradeQuick.style.opacity = '0.7';
    try {
      const res = await fetch('/api/billing/checkout', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify({ plan: 'monthly' }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      window.location.href = data.url;
    } catch (err) {
      // Fall back to the plan-picker page so the user still has a path
      upgradeQuick.textContent = originalText;
      upgradeQuick.style.pointerEvents = '';
      upgradeQuick.style.opacity = '';
      showToast('Could not start checkout. Opening the pricing page…', 'error');
      console.error(err);
      setTimeout(() => { window.location.href = '/upgrade'; }, 800);
    }
  });
}

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

// ----- Post-checkout celebration --------------------------------------------
// Stripe's success_url redirects to /app?upgraded=1. The webhook flips the
// plan to 'plus' asynchronously (usually within 1-2s). While that lands in
// the background we show a full-screen "Welcome to Plus" card with confetti,
// then reload the page once the user dismisses so the server-rendered Plus
// widgets pick up the new plan.

const CELEBRATION_STYLES = `
.upgrade-celebration {
  position: fixed; inset: 0; z-index: 10000;
  display: flex; align-items: center; justify-content: center;
  padding: 24px;
}
.upgrade-celebration-backdrop {
  position: absolute; inset: 0;
  background: radial-gradient(ellipse at center, rgba(24, 18, 43, 0.85) 0%, rgba(0, 0, 0, 0.9) 80%);
  backdrop-filter: blur(12px);
  -webkit-backdrop-filter: blur(12px);
  animation: upgrade-fade-in 0.3s ease-out;
}
.upgrade-celebration-card {
  position: relative; z-index: 1;
  max-width: 480px; width: 100%;
  padding: 44px 40px 36px;
  background: linear-gradient(180deg, #1b1530 0%, #120d22 100%);
  border: 1px solid rgba(167, 139, 250, 0.32);
  border-radius: 24px;
  box-shadow: 0 30px 90px -20px rgba(139, 92, 246, 0.55),
              0 0 80px -30px rgba(167, 139, 250, 0.5);
  text-align: center;
  color: #fff;
  animation: upgrade-card-in 0.6s cubic-bezier(0.2, 0.9, 0.3, 1.15) forwards;
  overflow: hidden;
}
.upgrade-celebration-card::before {
  content: ''; position: absolute; inset: 0;
  background: radial-gradient(circle at 50% 0%, rgba(167, 139, 250, 0.25), transparent 60%);
  pointer-events: none;
}
.upgrade-celebration-emoji {
  font-size: 68px; line-height: 1; margin-bottom: 14px;
  animation: upgrade-pop 0.7s cubic-bezier(0.2, 0.9, 0.3, 1.4) both;
  position: relative;
}
.upgrade-celebration-eyebrow {
  display: inline-flex; align-items: center; gap: 8px;
  padding: 6px 14px; border-radius: 999px;
  background: rgba(167, 139, 250, 0.14);
  border: 1px solid rgba(167, 139, 250, 0.32);
  color: #c4b5fd;
  font-size: 11px; font-weight: 600; letter-spacing: 0.12em; text-transform: uppercase;
  margin-bottom: 16px;
  position: relative;
}
.upgrade-celebration-eyebrow::before {
  content: ''; width: 6px; height: 6px; border-radius: 50%;
  background: #a78bfa; box-shadow: 0 0 8px #a78bfa;
}
.upgrade-celebration-card h2 {
  position: relative;
  font-size: 32px; font-weight: 800;
  margin: 0 0 10px; line-height: 1.1; letter-spacing: -0.02em;
  background: linear-gradient(180deg, #ffffff, #a78bfa);
  -webkit-background-clip: text; background-clip: text; color: transparent;
}
.upgrade-celebration-card p {
  position: relative;
  color: #cbd5e1; font-size: 15px; line-height: 1.55;
  margin: 0 0 24px;
}
.upgrade-celebration-perks {
  position: relative;
  text-align: left; list-style: none; padding: 0; margin: 0 0 28px;
  display: flex; flex-direction: column; gap: 10px;
}
.upgrade-celebration-perks li {
  color: #e2e8f0; font-size: 14px;
  padding-left: 28px; position: relative;
  opacity: 0;
  animation: upgrade-perk-in 0.4s ease-out forwards;
}
.upgrade-celebration-perks li::before {
  content: ''; position: absolute; left: 0; top: 3px;
  width: 18px; height: 18px; border-radius: 50%;
  background: rgba(167, 139, 250, 0.18);
  border: 1px solid rgba(167, 139, 250, 0.4);
}
.upgrade-celebration-perks li::after {
  content: ''; position: absolute; left: 5px; top: 6px;
  width: 9px; height: 5px;
  border-left: 2px solid #a78bfa;
  border-bottom: 2px solid #a78bfa;
  transform: rotate(-45deg);
}
.upgrade-celebration-perks li:nth-child(1) { animation-delay: 0.3s; }
.upgrade-celebration-perks li:nth-child(2) { animation-delay: 0.4s; }
.upgrade-celebration-perks li:nth-child(3) { animation-delay: 0.5s; }
.upgrade-celebration-perks li:nth-child(4) { animation-delay: 0.6s; }
.upgrade-celebration-perks li:nth-child(5) { animation-delay: 0.7s; }
.upgrade-celebration-cta {
  position: relative;
  width: 100%; padding: 14px 24px;
  font-size: 16px; font-weight: 600; color: #fff;
  background: linear-gradient(135deg, #a78bfa, #6366f1 55%, #4f46e5);
  border: 1px solid rgba(167, 139, 250, 0.5);
  border-radius: 12px; cursor: pointer;
  box-shadow: 0 12px 36px -10px rgba(139, 92, 246, 0.6);
  transition: transform 0.15s, box-shadow 0.15s;
}
.upgrade-celebration-cta:hover {
  transform: translateY(-2px);
  box-shadow: 0 18px 46px -10px rgba(139, 92, 246, 0.75);
}
.upgrade-celebration-cta:active { transform: translateY(0); }

.confetti-particle {
  position: fixed; pointer-events: none; z-index: 10001;
  will-change: transform, opacity;
  animation: confetti-fall 3s cubic-bezier(0.15, 0.6, 0.3, 1) forwards;
}

@keyframes upgrade-fade-in { from { opacity: 0; } to { opacity: 1; } }
@keyframes upgrade-card-in {
  from { opacity: 0; transform: scale(0.85) translateY(24px); }
  to   { opacity: 1; transform: scale(1) translateY(0); }
}
@keyframes upgrade-pop {
  0%   { transform: scale(0) rotate(-15deg); }
  60%  { transform: scale(1.3) rotate(8deg); }
  100% { transform: scale(1) rotate(0); }
}
@keyframes upgrade-perk-in {
  from { opacity: 0; transform: translateX(-8px); }
  to   { opacity: 1; transform: translateX(0); }
}
@keyframes confetti-fall {
  0%   { transform: translate(0, 0) rotate(0); opacity: 1; }
  100% { transform: translate(var(--dx), var(--dy)) rotate(var(--rot)); opacity: 0; }
}
`;

function injectCelebrationStyles() {
  if (document.getElementById('celebration-styles')) return;
  const style = document.createElement('style');
  style.id = 'celebration-styles';
  style.textContent = CELEBRATION_STYLES;
  document.head.appendChild(style);
}

function fireConfetti(originX = 50, originY = 35) {
  const colors = ['#a78bfa', '#6366f1', '#ec4899', '#fbbf24', '#34d399', '#60a5fa', '#f472b6', '#c084fc'];
  const count = 45;
  for (let i = 0; i < count; i++) {
    const p = document.createElement('div');
    p.className = 'confetti-particle';
    const angle = (Math.random() - 0.5) * Math.PI * 0.85;
    const velocity = 280 + Math.random() * 380;
    const dx = Math.sin(angle) * velocity;
    const dy = 260 + Math.random() * 520;
    const rot = Math.random() * 900 - 450;
    const size = 6 + Math.random() * 6;
    const color = colors[Math.floor(Math.random() * colors.length)];
    const rounded = Math.random() > 0.55;
    p.style.cssText = `
      left: ${originX}%; top: ${originY}%;
      width: ${size}px; height: ${size * (1.3 + Math.random() * 0.5)}px;
      background: ${color};
      border-radius: ${rounded ? '50%' : '2px'};
      --dx: ${dx}px; --dy: ${dy}px; --rot: ${rot}deg;
    `;
    document.body.appendChild(p);
    setTimeout(() => p.remove(), 3200);
  }
}

function showUpgradeCelebration() {
  injectCelebrationStyles();

  const root = document.createElement('div');
  root.className = 'upgrade-celebration';
  root.innerHTML = `
    <div class="upgrade-celebration-backdrop"></div>
    <div class="upgrade-celebration-card" role="dialog" aria-labelledby="upgrade-celebration-title" aria-modal="true">
      <div class="upgrade-celebration-emoji">🎉</div>
      <div class="upgrade-celebration-eyebrow">Plus is active</div>
      <h2 id="upgrade-celebration-title">Welcome to Plus</h2>
      <p>Thanks for supporting Videoscriber. Here's what just unlocked for you:</p>
      <ul class="upgrade-celebration-perks">
        <li>Unlimited transcriptions &mdash; no monthly cap</li>
        <li>Up to 1 GB per recording</li>
        <li>Transcripts kept as long as you want</li>
        <li>AI assistant &mdash; chat across your whole library</li>
        <li>Send recap emails from your own domain</li>
      </ul>
      <button type="button" class="upgrade-celebration-cta">Let's go &rarr;</button>
    </div>
  `;
  document.body.appendChild(root);

  // Stagger three confetti bursts from different origins for a fuller effect
  fireConfetti(50, 30);
  setTimeout(() => fireConfetti(25, 40), 250);
  setTimeout(() => fireConfetti(75, 40), 500);

  const dismiss = () => {
    // Reload so the server re-renders with plan='plus' (Plus widget, new limits,
    // billing section on /settings, etc.)
    window.location.reload();
  };
  root.querySelector('.upgrade-celebration-cta').addEventListener('click', dismiss);
  root.querySelector('.upgrade-celebration-backdrop').addEventListener('click', dismiss);
  document.addEventListener('keydown', function onEsc(e) {
    if (e.key === 'Escape') {
      document.removeEventListener('keydown', onEsc);
      dismiss();
    }
  });
}

const params = new URLSearchParams(window.location.search);
if (params.get('upgraded') === '1') {
  // Clean the URL so a refresh / share doesn't retrigger the celebration
  window.history.replaceState({}, '', window.location.pathname);
  showUpgradeCelebration();

  // Poll in the background so the plan is 'plus' by the time the user
  // dismisses and the page reloads. Silent — the modal is the UX.
  let attempts = 0;
  const maxAttempts = 20;
  const poll = setInterval(async () => {
    attempts++;
    try {
      const res = await fetch('/api/config', { credentials: 'same-origin' });
      if (res.ok) {
        const data = await res.json();
        if (data.plan && data.plan.tier === 'plus') {
          clearInterval(poll);
          return;
        }
      }
    } catch (_) {}
    if (attempts >= maxAttempts) clearInterval(poll);
  }, 1000);
}
