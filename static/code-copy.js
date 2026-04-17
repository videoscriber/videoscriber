// Click-to-copy for every .code-block and every inline <code> on public pages.
// Strips the leading `$` prompt markers so pasting into a shell "just works".
(() => {
    function extractBlockCommand(block) {
        const clone = block.cloneNode(true);
        clone.querySelectorAll('.prompt, .code-copy-btn').forEach(el => el.remove());
        return clone.textContent
            .split('\n')
            .map(line => line.replace(/^\s+/, ''))
            .join('\n')
            .replace(/\n{3,}/g, '\n\n')
            .trim();
    }

    function flash(btn, msg, copied = false) {
        const original = btn.dataset.label || btn.textContent;
        if (!btn.dataset.label) btn.dataset.label = original;
        btn.textContent = msg;
        if (copied) btn.classList.add('copied');
        clearTimeout(btn._copyTimer);
        btn._copyTimer = setTimeout(() => {
            btn.textContent = btn.dataset.label;
            btn.classList.remove('copied');
        }, 1400);
    }

    async function copyText(text, btn) {
        try {
            await navigator.clipboard.writeText(text);
            flash(btn, 'Copied', true);
        } catch {
            // Fallback for older browsers / file:// contexts
            const ta = document.createElement('textarea');
            ta.value = text;
            ta.style.position = 'fixed';
            ta.style.opacity = '0';
            document.body.appendChild(ta);
            ta.select();
            try { document.execCommand('copy'); flash(btn, 'Copied', true); }
            catch { flash(btn, 'Error'); }
            document.body.removeChild(ta);
        }
    }

    // Block-level copy button (top-right of each .code-block)
    document.querySelectorAll('.code-block').forEach(block => {
        if (block.querySelector('.code-copy-btn')) return;
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'code-copy-btn';
        btn.textContent = 'Copy';
        btn.setAttribute('aria-label', 'Copy snippet to clipboard');
        btn.addEventListener('click', (e) => {
            e.stopPropagation();
            copyText(extractBlockCommand(block), btn);
        });
        block.appendChild(btn);
    });

    // Inline <code> click-to-copy. Skip anything already inside a code-block.
    // Also skip very short tokens like `.env` or keyboard shortcuts — click-to-copy
    // is only meaningful for tokens you'd actually paste somewhere.
    const INLINE_MIN = 4;
    document.querySelectorAll('main code').forEach(code => {
        if (code.closest('.code-block')) return;
        const text = code.textContent.trim();
        if (text.length < INLINE_MIN) return;
        code.classList.add('code-inline-copy');
        code.setAttribute('role', 'button');
        code.setAttribute('tabindex', '0');
        code.setAttribute('title', 'Click to copy');
        const doCopy = (e) => {
            e.preventDefault();
            e.stopPropagation();
            copyText(text, code);
        };
        code.addEventListener('click', doCopy);
        code.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' || e.key === ' ') doCopy(e);
        });
    });
})();
