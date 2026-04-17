// Fade-in-on-scroll for sections with class="reveal" on all public pages.
// Without this, .reveal { opacity: 0 } in landing.css would leave those
// sections permanently invisible on pages that don't ship their own
// IntersectionObserver bootstrap (e.g. /download, /brand, /setup).
(() => {
    const targets = document.querySelectorAll('.reveal');
    if (!targets.length) return;
    const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (reduce || !('IntersectionObserver' in window)) {
        targets.forEach(el => el.classList.add('in'));
        return;
    }
    const io = new IntersectionObserver((entries) => {
        for (const e of entries) {
            if (e.isIntersecting) { e.target.classList.add('in'); io.unobserve(e.target); }
        }
    }, { rootMargin: '0px 0px -10% 0px', threshold: 0.12 });
    targets.forEach(el => io.observe(el));
})();
