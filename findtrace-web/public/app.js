(() => {
  'use strict';

  const HISTORY_KEY = 'findtrace.history';
  const MAX_HISTORY = 10;

  const form = document.getElementById('search-form');
  const requestIdInput = document.getElementById('request-id');
  const requestIdError = document.getElementById('request-id-error');
  const searchBtn = document.getElementById('search-btn');
  const clearBtn = document.getElementById('clear-btn');

  const statusPanel = document.getElementById('status-panel');
  const statusLoading = document.getElementById('status-loading');
  const statusSummary = document.getElementById('status-summary');
  const statusError = document.getElementById('status-error');
  const statusEmpty = document.getElementById('status-empty');

  const summaryRequestId = document.getElementById('summary-request-id');
  const summarySource = document.getElementById('summary-source');
  const summaryTime = document.getElementById('summary-time');
  const summaryStatus = document.getElementById('summary-status');

  const resultPanel = document.getElementById('result-panel');
  const logOutput = document.getElementById('log-output');

  const copyBtn = document.getElementById('copy-btn');
  const downloadBtn = document.getElementById('download-btn');
  const wrapBtn = document.getElementById('wrap-btn');
  const expandBtn = document.getElementById('expand-btn');
  const rawBtn = document.getElementById('raw-btn');

  const findInput = document.getElementById('find-input');
  const findCount = document.getElementById('find-count');
  const findPrev = document.getElementById('find-prev');
  const findNext = document.getElementById('find-next');

  const historyPanel = document.getElementById('history-panel');
  const historyList = document.getElementById('history-list');

  const REQUEST_ID_PATTERN = /^[A-Za-z0-9_.:-]+$/;
  const MAX_REQUEST_ID_LENGTH = 200;

  let currentRawOutput = '';
  let currentRequestId = '';
  let rawViewOn = false;
  let wrapOn = false;
  let findMatches = [];
  let findActiveIndex = -1;

  // ---------------------------------------------------------------------
  // Validation (mirrors server-side rules so users get instant feedback;
  // the server re-validates independently and is the source of truth)
  // ---------------------------------------------------------------------

  function validateRequestId(raw) {
    const value = (raw || '').trim();
    if (!value) return 'Request ID is required.';
    if (value.length > MAX_REQUEST_ID_LENGTH) {
      return `Request ID must be at most ${MAX_REQUEST_ID_LENGTH} characters.`;
    }
    if (/[\n\r]/.test(value)) return 'Request ID must not contain newline characters.';
    if (value.includes('\0')) return 'Request ID must not contain null bytes.';
    if (!REQUEST_ID_PATTERN.test(value)) {
      return 'Request ID contains unsupported characters. Allowed: letters, numbers, - _ . :';
    }
    return null;
  }

  function setFieldError(message) {
    if (message) {
      requestIdError.textContent = message;
      requestIdError.hidden = false;
      requestIdInput.setAttribute('aria-invalid', 'true');
    } else {
      requestIdError.textContent = '';
      requestIdError.hidden = true;
      requestIdInput.removeAttribute('aria-invalid');
    }
  }

  // ---------------------------------------------------------------------
  // Escaping helper: this is the single point through which all log
  // content passes before ever touching the DOM as markup.
  // ---------------------------------------------------------------------

  function escapeHtml(str) {
    return str
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  // ---------------------------------------------------------------------
  // Highlighting: operates only on already-escaped text and only wraps
  // matches of a fixed, known keyword list in <span> tags. It never
  // introduces attacker-controlled markup, since the source text has
  // already had every HTML-significant character escaped.
  // ---------------------------------------------------------------------

  const HIGHLIGHT_RULES = [
    { cls: 'hl-error', pattern: /\b(ERROR|Exception|Failure|FAILED|FATAL)\b/g },
    { cls: 'hl-warn', pattern: /\b(WARN|WARNING|Timeout)\b/g },
    { cls: 'hl-http', pattern: /\bHTTP\s?(400|401|403|404|500|502|503)\b/g },
    {
      cls: 'hl-middleware',
      pattern: /\b(HTTPRequest|HTTPReply|MQInput|MQOutput|SOAPRequest|RESTRequest|Backend)\b/g,
    },
  ];

  function highlightEscaped(escaped, requestId) {
    let html = escaped;
    for (const rule of HIGHLIGHT_RULES) {
      html = html.replace(rule.pattern, (m) => `<span class="${rule.cls}">${m}</span>`);
    }
    if (requestId) {
      const escapedId = escapeHtml(requestId).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
      if (escapedId) {
        const reqPattern = new RegExp(escapedId, 'g');
        html = html.replace(reqPattern, (m) => `<span class="hl-reqid">${m}</span>`);
      }
    }
    return html;
  }

  // ---------------------------------------------------------------------
  // Rendering
  // ---------------------------------------------------------------------

  function renderLogOutput() {
    const escaped = escapeHtml(currentRawOutput);
    if (rawViewOn) {
      // Safe: textContent never interprets markup.
      logOutput.textContent = currentRawOutput;
    } else {
      logOutput.innerHTML = highlightEscaped(escaped, currentRequestId);
    }
    applyFindHighlighting();
  }

  function showPanel(el) {
    el.hidden = false;
  }
  function hidePanel(el) {
    el.hidden = true;
  }

  function resetStatusPanel() {
    hidePanel(statusLoading);
    hidePanel(statusSummary);
    hidePanel(statusError);
    hidePanel(statusEmpty);
  }

  // ---------------------------------------------------------------------
  // History (sessionStorage) — only Request ID, source, and timestamp.
  // ---------------------------------------------------------------------

  function loadHistory() {
    try {
      const raw = sessionStorage.getItem(HISTORY_KEY);
      return raw ? JSON.parse(raw) : [];
    } catch (_e) {
      return [];
    }
  }

  function saveHistory(entries) {
    try {
      sessionStorage.setItem(HISTORY_KEY, JSON.stringify(entries));
    } catch (_e) {
      // sessionStorage unavailable (private mode, quota, etc.) — non-fatal.
    }
  }

  function addHistoryEntry(requestId, sourceValue, sourceLabel) {
    const entries = loadHistory().filter(
      (e) => !(e.requestId === requestId && e.source === sourceValue)
    );
    entries.unshift({
      requestId,
      source: sourceValue,
      sourceLabel,
      timestamp: new Date().toISOString(),
    });
    saveHistory(entries.slice(0, MAX_HISTORY));
    renderHistory();
  }

  function renderHistory() {
    const entries = loadHistory();
    historyList.innerHTML = '';
    if (entries.length === 0) {
      hidePanel(historyPanel);
      return;
    }
    showPanel(historyPanel);
    for (const entry of entries) {
      const li = document.createElement('li');
      const btn = document.createElement('button');
      btn.type = 'button';

      const idSpan = document.createElement('span');
      idSpan.textContent = entry.requestId;
      btn.appendChild(idSpan);

      const srcSpan = document.createElement('span');
      srcSpan.className = 'history-source';
      srcSpan.textContent = entry.sourceLabel || '';
      btn.appendChild(srcSpan);

      btn.addEventListener('click', () => {
        requestIdInput.value = entry.requestId;
        document.querySelector(`input[name="source"][value="${entry.source}"]`).checked = true;
        runSearch();
      });
      li.appendChild(btn);
      historyList.appendChild(li);
    }
  }

  // ---------------------------------------------------------------------
  // Find in Result
  // ---------------------------------------------------------------------

  function applyFindHighlighting() {
    const term = findInput.value.trim();
    findMatches = [];
    findActiveIndex = -1;

    if (!term) {
      findCount.textContent = '0 matches';
      return;
    }

    // Re-render base content, then wrap matches. Works in both raw and
    // highlighted modes by walking text nodes so we never touch content
    // we didn't already put there ourselves.
    const escapedTerm = term.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const pattern = new RegExp(escapedTerm, 'gi');

    const walker = document.createTreeWalker(logOutput, NodeFilter.SHOW_TEXT, null);
    const textNodes = [];
    let node;
    while ((node = walker.nextNode())) {
      textNodes.push(node);
    }

    for (const textNode of textNodes) {
      const text = textNode.nodeValue;
      pattern.lastIndex = 0;
      if (!pattern.test(text)) continue;
      pattern.lastIndex = 0;

      const frag = document.createDocumentFragment();
      let lastIndex = 0;
      let match;
      while ((match = pattern.exec(text)) !== null) {
        if (match.index > lastIndex) {
          frag.appendChild(document.createTextNode(text.slice(lastIndex, match.index)));
        }
        const mark = document.createElement('span');
        mark.className = 'hl-findmatch';
        mark.textContent = match[0];
        frag.appendChild(mark);
        findMatches.push(mark);
        lastIndex = match.index + match[0].length;
        if (match[0].length === 0) pattern.lastIndex++;
      }
      if (lastIndex < text.length) {
        frag.appendChild(document.createTextNode(text.slice(lastIndex)));
      }
      textNode.parentNode.replaceChild(frag, textNode);
    }

    findCount.textContent = `${findMatches.length} match${findMatches.length === 1 ? '' : 'es'}`;
    if (findMatches.length > 0) {
      setActiveMatch(0);
    }
  }

  function setActiveMatch(index) {
    if (findMatches.length === 0) return;
    if (findActiveIndex >= 0 && findMatches[findActiveIndex]) {
      findMatches[findActiveIndex].classList.remove('hl-findmatch--active');
    }
    findActiveIndex = ((index % findMatches.length) + findMatches.length) % findMatches.length;
    const el = findMatches[findActiveIndex];
    el.classList.add('hl-findmatch--active');
    el.scrollIntoView({ block: 'center', inline: 'nearest' });
    findCount.textContent = `${findActiveIndex + 1} of ${findMatches.length} matches`;
  }

  findInput.addEventListener('input', () => renderLogOutput());
  findNext.addEventListener('click', () => setActiveMatch(findActiveIndex + 1));
  findPrev.addEventListener('click', () => setActiveMatch(findActiveIndex - 1));
  findInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      if (e.shiftKey) {
        setActiveMatch(findActiveIndex - 1);
      } else {
        setActiveMatch(findActiveIndex + 1);
      }
    }
  });

  // ---------------------------------------------------------------------
  // Toolbar actions
  // ---------------------------------------------------------------------

  copyBtn.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(currentRawOutput);
      copyBtn.textContent = 'Copied!';
      setTimeout(() => (copyBtn.textContent = 'Copy'), 1500);
    } catch (_e) {
      copyBtn.textContent = 'Copy failed';
      setTimeout(() => (copyBtn.textContent = 'Copy'), 1500);
    }
  });

  downloadBtn.addEventListener('click', () => {
    const filename = `${currentRequestId || 'findtrace-result'}.log`;
    const blob = new Blob([currentRawOutput], { type: 'text/plain;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  });

  wrapBtn.addEventListener('click', () => {
    wrapOn = !wrapOn;
    logOutput.classList.toggle('wrap', wrapOn);
    wrapBtn.textContent = `Wrap Lines: ${wrapOn ? 'ON' : 'OFF'}`;
    wrapBtn.setAttribute('aria-pressed', String(wrapOn));
  });

  expandBtn.addEventListener('click', () => {
    const expanded = resultPanel.classList.toggle('result-panel--expanded');
    expandBtn.textContent = expanded ? 'Collapse' : 'Expand';
    expandBtn.setAttribute('aria-pressed', String(expanded));
  });

  rawBtn.addEventListener('click', () => {
    rawViewOn = !rawViewOn;
    rawBtn.textContent = `Raw View: ${rawViewOn ? 'ON' : 'OFF'}`;
    rawBtn.setAttribute('aria-pressed', String(rawViewOn));
    renderLogOutput();
  });

  // ---------------------------------------------------------------------
  // Search flow
  // ---------------------------------------------------------------------

  function getSelectedSource() {
    const el = document.querySelector('input[name="source"]:checked');
    return el ? el.value : null;
  }

  function getSelectedSourceLabel() {
    const el = document.querySelector('input[name="source"]:checked');
    return el ? el.closest('.source-card').querySelector('.source-card__label').textContent : '';
  }

  async function runSearch() {
    const rawId = requestIdInput.value;
    const validationError = validateRequestId(rawId);
    setFieldError(validationError);
    if (validationError) {
      requestIdInput.focus();
      return;
    }

    const requestId = rawId.trim();
    const source = getSelectedSource();
    const sourceLabel = getSelectedSourceLabel();

    searchBtn.disabled = true;
    showPanel(statusPanel);
    resetStatusPanel();
    showPanel(statusLoading);
    hidePanel(resultPanel);
    findInput.value = '';

    try {
      const resp = await fetch('/api/findtrace', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ source, requestId }),
      });

      let data;
      try {
        data = await resp.json();
      } catch (_e) {
        throw new Error('Unexpected response from server.');
      }

      resetStatusPanel();

      if (!resp.ok || !data.success) {
        statusError.textContent = data && data.error ? data.error : 'Log search failed.';
        showPanel(statusError);
        return;
      }

      addHistoryEntry(requestId, source, sourceLabel);

      const execSeconds = (data.executionTimeMs / 1000).toFixed(2);

      if (data.found === false) {
        summaryRequestId.textContent = data.requestId;
        summarySource.textContent = data.source;
        summaryTime.textContent = `${execSeconds} sec`;
        summaryStatus.textContent = 'Not Found';
        summaryStatus.className = 'status--not-found';
        showPanel(statusSummary);
        showPanel(statusEmpty);
        return;
      }

      summaryRequestId.textContent = data.requestId;
      summarySource.textContent = data.source;
      summaryTime.textContent = `${execSeconds} sec`;
      summaryStatus.textContent = 'Found';
      summaryStatus.className = 'status--found';
      showPanel(statusSummary);

      currentRawOutput = data.output;
      currentRequestId = data.requestId;
      rawViewOn = false;
      rawBtn.textContent = 'Raw View: OFF';
      rawBtn.setAttribute('aria-pressed', 'false');
      renderLogOutput();
      showPanel(resultPanel);
    } catch (err) {
      resetStatusPanel();
      statusError.textContent = 'Unable to reach the server. Please try again.';
      showPanel(statusError);
      console.error(err);
    } finally {
      searchBtn.disabled = false;
    }
  }

  form.addEventListener('submit', (e) => {
    e.preventDefault();
    runSearch();
  });

  requestIdInput.addEventListener('input', () => setFieldError(null));

  clearBtn.addEventListener('click', () => {
    requestIdInput.value = '';
    setFieldError(null);
    hidePanel(statusPanel);
    hidePanel(resultPanel);
    currentRawOutput = '';
    currentRequestId = '';
    requestIdInput.focus();
  });

  renderHistory();
})();
