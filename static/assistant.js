// AI Assistant drawer — Plus-gated, SSE streaming chat with citations.
(() => {
    const btn = document.getElementById('assistant-btn');
    const drawer = document.getElementById('assistant-drawer');
    const closeBtn = document.getElementById('assistant-close');
    const newBtn = document.getElementById('assistant-new');
    const messagesEl = document.getElementById('assistant-messages');
    const inputForm = document.getElementById('assistant-input-form');
    const inputEl = document.getElementById('assistant-input');
    const sendBtn = document.getElementById('assistant-send');
    const scopeEl = document.getElementById('assistant-scope');
    const nudge = document.getElementById('plus-nudge');
    const nudgeClose = document.getElementById('plus-nudge-close');

    if (!btn || !drawer) return;

    // State
    let conversationId = null;
    let scope = 'library';
    let transcriptionId = null;
    let streamAbort = null;

    const requiresPlus = () => btn.dataset.requiresPlus === 'true';

    // --- Open / close -------------------------------------------------------
    btn.addEventListener('click', () => {
        if (requiresPlus()) {
            nudge.hidden = false;
            return;
        }
        openDrawer();
    });

    closeBtn.addEventListener('click', closeDrawer);

    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && !drawer.hidden) closeDrawer();
    });

    nudgeClose && nudgeClose.addEventListener('click', () => { nudge.hidden = true; });
    nudge && nudge.addEventListener('click', (e) => { if (e.target === nudge) nudge.hidden = true; });

    function openDrawer() {
        drawer.hidden = false;
        document.body.classList.add('assistant-open');
        requestAnimationFrame(() => drawer.classList.add('in'));
        if (!conversationId) initConversation('library', null);
        setTimeout(() => inputEl.focus(), 80);
    }

    function closeDrawer() {
        drawer.classList.remove('in');
        document.body.classList.remove('assistant-open');
        setTimeout(() => { drawer.hidden = true; }, 240);
    }

    newBtn.addEventListener('click', () => {
        conversationId = null;
        messagesEl.innerHTML = '';
        initConversation(scope, transcriptionId);
    });

    // --- External: allow the app to open the drawer scoped to a transcript
    window.openAssistantFor = async function(transcription) {
        if (requiresPlus()) { nudge.hidden = false; return; }
        scope = 'transcription';
        transcriptionId = transcription.id;
        scopeEl.textContent = transcription.filename || 'This recording';
        conversationId = null;
        messagesEl.innerHTML = '';
        openDrawer();
        await initConversation('transcription', transcription.id);
    };

    // --- Conversation bootstrap --------------------------------------------
    async function initConversation(nextScope, nextTranscription) {
        scope = nextScope;
        transcriptionId = nextTranscription;
        scopeEl.textContent = scope === 'transcription' ? 'This recording' : 'Your library';
        const body = new URLSearchParams({ scope });
        if (nextTranscription) body.append('transcription_id', nextTranscription);
        try {
            const res = await fetch('/api/chat/conversations', { method: 'POST', body });
            if (!res.ok) throw new Error('Could not start conversation');
            const data = await res.json();
            conversationId = data.id;
            renderAssistantMessage(
                scope === 'transcription'
                    ? "Ask me anything about this recording — quotes, decisions, next steps."
                    : "Ask me anything about your recordings. I'll pull from the transcripts and cite where I found each answer."
            );
        } catch (e) {
            console.error(e);
            renderAssistantMessage('I couldn\'t start a conversation. Try refreshing.', true);
        }
    }

    // --- Render helpers ----------------------------------------------------
    function renderUserMessage(text) {
        const el = document.createElement('div');
        el.className = 'chat-msg user';
        el.innerHTML = `<div class="chat-bubble">${escapeHtml(text)}</div>`;
        messagesEl.appendChild(el);
        messagesEl.scrollTop = messagesEl.scrollHeight;
    }

    function renderAssistantMessage(text, isError = false) {
        const el = document.createElement('div');
        el.className = 'chat-msg assistant' + (isError ? ' error' : '');
        el.innerHTML = `<div class="chat-bubble"></div>`;
        const bubble = el.querySelector('.chat-bubble');
        bubble.textContent = text;
        messagesEl.appendChild(el);
        messagesEl.scrollTop = messagesEl.scrollHeight;
        return bubble;
    }

    function renderSources(sources) {
        if (!sources || !sources.length) return null;
        const el = document.createElement('div');
        el.className = 'chat-sources';
        el.innerHTML = `<div class="chat-sources-label">Sources</div>` +
            sources.map(s => {
                const ts = (s.start != null) ? formatTs(s.start) : '';
                const fn = escapeHtml(s.filename || 'recording');
                return `<div class="chat-source">${fn}${ts ? ` · ${ts}` : ''}</div>`;
            }).join('');
        return el;
    }

    function formatTs(sec) {
        sec = Math.floor(sec || 0);
        const m = Math.floor(sec / 60), s = sec % 60;
        return `${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`;
    }

    function escapeHtml(s) {
        const div = document.createElement('div');
        div.textContent = s;
        return div.innerHTML;
    }

    // --- Send message with SSE streaming -----------------------------------
    inputForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        const text = inputEl.value.trim();
        if (!text || !conversationId) return;
        inputEl.value = '';
        autoGrow();

        renderUserMessage(text);
        const bubble = renderAssistantMessage('');
        bubble.classList.add('streaming');
        sendBtn.disabled = true;

        // Using fetch streaming for SSE, not EventSource (EventSource doesn't support POST bodies)
        const body = new URLSearchParams({ message: text });
        try {
            const controller = new AbortController();
            streamAbort = controller;
            const res = await fetch(`/api/chat/conversations/${conversationId}/messages`, {
                method: 'POST', body, signal: controller.signal,
            });
            if (!res.ok) {
                const data = await res.json().catch(() => ({}));
                throw new Error(data.detail || data.error || 'Assistant failed');
            }
            const reader = res.body.getReader();
            const decoder = new TextDecoder();
            let buf = '';
            let fullText = '';
            let sourcesEl = null;

            while (true) {
                const { value, done } = await reader.read();
                if (done) break;
                buf += decoder.decode(value, { stream: true });

                let idx;
                while ((idx = buf.indexOf('\n\n')) !== -1) {
                    const raw = buf.slice(0, idx);
                    buf = buf.slice(idx + 2);
                    const evt = parseSse(raw);
                    if (!evt) continue;
                    if (evt.event === 'sources') {
                        const data = JSON.parse(evt.data || '{}');
                        sourcesEl = renderSources(data.sources);
                        if (sourcesEl) bubble.parentElement.appendChild(sourcesEl);
                    } else if (evt.event === 'delta') {
                        const data = JSON.parse(evt.data || '{}');
                        fullText += data.delta || '';
                        bubble.textContent = fullText;
                        messagesEl.scrollTop = messagesEl.scrollHeight;
                    } else if (evt.event === 'error') {
                        const data = JSON.parse(evt.data || '{}');
                        bubble.textContent = data.error || 'Assistant failed.';
                        bubble.classList.add('error-text');
                    } else if (evt.event === 'done') {
                        // Done streaming
                    }
                }
            }
            bubble.classList.remove('streaming');
        } catch (err) {
            bubble.textContent = err.message || 'Assistant failed.';
            bubble.classList.remove('streaming');
            bubble.classList.add('error-text');
        } finally {
            streamAbort = null;
            sendBtn.disabled = false;
            inputEl.focus();
        }
    });

    function parseSse(block) {
        const lines = block.split('\n');
        let event = 'message', data = '';
        for (const line of lines) {
            if (line.startsWith('event:')) event = line.slice(6).trim();
            else if (line.startsWith('data:')) data += line.slice(5).trim();
        }
        return { event, data };
    }

    // Auto-grow textarea
    function autoGrow() {
        inputEl.style.height = 'auto';
        inputEl.style.height = Math.min(inputEl.scrollHeight, 200) + 'px';
    }
    inputEl.addEventListener('input', autoGrow);
    inputEl.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            inputForm.requestSubmit();
        }
    });
})();
