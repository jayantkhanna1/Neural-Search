/* Neural Search frontend: session management, research progress polling, RAG chat. */
(function () {
  "use strict";

  const POLL_INTERVAL_MS = 2500;

  const el = {
    homeView: document.getElementById("home-view"),
    sessionView: document.getElementById("session-view"),
    searchForm: document.getElementById("search-form"),
    searchInput: document.getElementById("search-input"),
    homeError: document.getElementById("home-error"),
    sessionList: document.getElementById("session-list"),
    newSessionBtn: document.getElementById("new-session-btn"),
    sessionTitle: document.getElementById("session-title"),
    taskList: document.getElementById("task-list"),
    summaryCard: document.getElementById("summary-card"),
    summaryContent: document.getElementById("summary-content"),
    claimsCard: document.getElementById("claims-card"),
    claimsList: document.getElementById("claims-list"),
    sourcesCard: document.getElementById("sources-card"),
    sourceList: document.getElementById("source-list"),
    sourceCount: document.getElementById("source-count"),
    chatLog: document.getElementById("chat-log"),
    chatForm: document.getElementById("chat-form"),
    chatInput: document.getElementById("chat-input"),
    chatError: document.getElementById("chat-error"),
  };

  let currentSessionId = null;
  let pollTimer = null;
  let eventSource = null;

  // ------------------------------------------------------------------ utils
  function csrfToken() {
    const input = document.querySelector("input[name=csrfmiddlewaretoken]");
    return input ? input.value : "";
  }

  async function api(path, options = {}) {
    const response = await fetch(path, {
      headers: {
        "Content-Type": "application/json",
        "X-CSRFToken": csrfToken(),
      },
      ...options,
    });
    let data = {};
    try { data = await response.json(); } catch (e) { /* non-JSON error page */ }
    if (!response.ok) {
      throw new Error(data.error || `Request failed (${response.status})`);
    }
    return data;
  }

  function escapeHtml(text) {
    const div = document.createElement("div");
    div.textContent = text;
    return div.innerHTML;
  }

  // Minimal, safe markdown rendering for summaries (escapes first).
  function renderMarkdown(text) {
    let html = escapeHtml(text);
    html = html.replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
    html = html.replace(/^### (.*)$/gm, "<h3>$1</h3>");
    html = html.replace(/^## (.*)$/gm, "<h2>$1</h2>");
    html = html.replace(/^# (.*)$/gm, "<h2>$1</h2>");
    html = html.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    // Group consecutive bullet lines into lists.
    html = html.replace(/(^|\n)((?:[-*] .*(?:\n|$))+)/g, function (_, lead, block) {
      const items = block.trim().split("\n")
        .map((line) => "<li>" + line.replace(/^[-*] /, "") + "</li>").join("");
      return lead + "<ul>" + items + "</ul>";
    });
    return html.split(/\n{2,}/).map((p) =>
      /^<(ul|h2|h3)/.test(p.trim()) ? p : "<p>" + p.replace(/\n/g, "<br>") + "</p>"
    ).join("");
  }

  // -------------------------------------------------------------- rendering
  function renderSessionList(sessions) {
    el.sessionList.innerHTML = "";
    sessions.forEach((s) => {
      const btn = document.createElement("button");
      btn.textContent = s.title;
      btn.title = s.title;
      if (s.id === currentSessionId) btn.classList.add("active");
      btn.addEventListener("click", () => openSession(s.id));
      el.sessionList.appendChild(btn);
    });
  }

  function renderTasks(tasks) {
    el.taskList.innerHTML = "";
    tasks.forEach((t) => {
      const wrap = document.createElement("div");
      wrap.className = "card task";
      const stageClass = t.status === "failed" ? "failed" : (t.status === "completed" ? "completed" : "");
      const stageText = t.status === "failed"
        ? (t.error || "Failed")
        : (t.status === "completed"
            ? `Done — ${t.sources_kept} sources kept`
            : (t.stage_detail || t.status_label));
      wrap.innerHTML =
        `<div class="task-row">
           <span class="task-query">${escapeHtml(t.query)}</span>
           <span class="task-stage ${stageClass}">${escapeHtml(stageText)}</span>
         </div>
         <div class="progress-bar">
           <div class="progress-fill ${stageClass}" style="width:${t.status === "failed" ? 100 : t.progress}%"></div>
         </div>`;
      el.taskList.appendChild(wrap);
    });
  }

  function renderSummary(session) {
    if (session.summary) {
      el.summaryCard.hidden = false;
      el.summaryContent.innerHTML = renderMarkdown(session.summary);
    } else {
      el.summaryCard.hidden = true;
    }
  }

  function renderClaims(claimsData) {
    const claims = claimsData || [];
    if (!claims.length) { el.claimsCard.hidden = true; return; }
    el.claimsCard.hidden = false;
    el.claimsList.innerHTML = "";
    claims.forEach((c) => {
      const li = document.createElement("li");
      const n = (c.source_urls || []).length;
      li.innerHTML =
        `<span class="badge ${escapeHtml(c.confidence || "low")}">${escapeHtml(c.confidence || "low")}</span>` +
        `${escapeHtml(c.claim)}` +
        `<span class="claim-sources">corroborated by ${n} source${n === 1 ? "" : "s"}</span>`;
      el.claimsList.appendChild(li);
    });
  }

  function renderSources(sources) {
    if (!sources.length) { el.sourcesCard.hidden = true; return; }
    el.sourcesCard.hidden = false;
    el.sourceCount.textContent = `(${sources.length})`;
    el.sourceList.innerHTML = "";
    sources.forEach((s) => {
      const li = document.createElement("li");
      const score = (s.quality_score * 100).toFixed(0);
      li.innerHTML =
        `<a href="${escapeHtml(s.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(s.title)}</a>` +
        `<span class="source-meta">${escapeHtml(s.domain)} · quality ${score}%</span>`;
      el.sourceList.appendChild(li);
    });
  }

  function appendMessage(role, content, extraClass) {
    const div = document.createElement("div");
    div.className = `msg ${role}${extraClass ? " " + extraClass : ""}`;
    div.textContent = content;
    el.chatLog.appendChild(div);
    el.chatLog.scrollTop = el.chatLog.scrollHeight;
    return div;
  }

  function renderMessages(messages) {
    el.chatLog.innerHTML = "";
    messages.forEach((m) => appendMessage(m.role, m.content));
  }

  function renderSession(session, opts = {}) {
    el.homeView.hidden = true;
    el.sessionView.hidden = false;
    el.sessionTitle.textContent = session.title;
    renderTasks(session.tasks);
    renderSummary(session);
    renderClaims(session.claims);
    renderSources(session.sources);
    if (!opts.skipMessages) renderMessages(session.messages);
  }

  // ------------------------------------------------------------ live updates
  // Primary channel is Server-Sent Events; falls back to polling if the SSE
  // connection fails (e.g. behind a buffering proxy).
  function stopLiveUpdates() {
    if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
    if (eventSource) { eventSource.close(); eventSource = null; }
  }

  function startLiveUpdates() {
    stopLiveUpdates();
    if (!currentSessionId) return;
    if (typeof EventSource !== "undefined") {
      const sessionId = currentSessionId;
      eventSource = new EventSource(`/api/sessions/${sessionId}/events/`);
      eventSource.onmessage = (event) => {
        if (sessionId !== currentSessionId) { stopLiveUpdates(); return; }
        try {
          const { session } = JSON.parse(event.data);
          renderSession(session, { skipMessages: true });
        } catch (err) { /* malformed frame; next one will recover */ }
      };
      eventSource.addEventListener("done", () => stopLiveUpdates());
      eventSource.onerror = () => {
        stopLiveUpdates();
        pollTimer = setTimeout(pollSession, POLL_INTERVAL_MS);
      };
    } else {
      pollTimer = setTimeout(pollSession, POLL_INTERVAL_MS);
    }
  }

  async function pollSession() {
    if (!currentSessionId) return;
    try {
      const { session } = await api(`/api/sessions/${currentSessionId}/`);
      if (session.id !== currentSessionId) return;
      renderSession(session, { skipMessages: true });
      if (session.is_researching) {
        pollTimer = setTimeout(pollSession, POLL_INTERVAL_MS);
      }
    } catch (err) {
      pollTimer = setTimeout(pollSession, POLL_INTERVAL_MS * 3);
    }
  }

  // ----------------------------------------------------------------- actions
  async function openSession(sessionId) {
    stopLiveUpdates();
    currentSessionId = sessionId;
    const { session } = await api(`/api/sessions/${sessionId}/`);
    renderSession(session);
    refreshSessionList();
    if (session.is_researching) startLiveUpdates();
  }

  async function refreshSessionList() {
    try {
      const { sessions } = await api("/api/sessions/");
      renderSessionList(sessions);
    } catch (err) { /* sidebar is non-critical */ }
  }

  el.searchForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const query = el.searchInput.value.trim();
    if (!query) return;
    const button = el.searchForm.querySelector("button");
    button.disabled = true;
    el.homeError.hidden = true;
    try {
      const { session } = await api("/api/research/", {
        method: "POST",
        body: JSON.stringify({ query }),
      });
      el.searchInput.value = "";
      currentSessionId = session.id;
      renderSession(session);
      refreshSessionList();
      startLiveUpdates();
    } catch (err) {
      el.homeError.textContent = err.message;
      el.homeError.hidden = false;
    } finally {
      button.disabled = false;
    }
  });

  el.chatForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const message = el.chatInput.value.trim();
    if (!message || !currentSessionId) return;
    el.chatInput.value = "";
    el.chatError.hidden = true;
    appendMessage("user", message);
    const pending = appendMessage("assistant", "Thinking…", "pending");
    const button = el.chatForm.querySelector("button");
    button.disabled = true;
    try {
      const data = await api(`/api/sessions/${currentSessionId}/chat/`, {
        method: "POST",
        body: JSON.stringify({ message }),
      });
      pending.classList.remove("pending");
      pending.textContent = data.reply;
      if (data.citations && data.citations.length) {
        const cite = document.createElement("div");
        cite.className = "msg-citations";
        cite.innerHTML = "Sources: " + data.citations.map((c) =>
          `<a href="${escapeHtml(c.url)}" target="_blank" rel="noopener noreferrer">[${c.n}] ${escapeHtml(c.title || c.url)}</a>`
        ).join(" · ");
        el.chatLog.appendChild(cite);
        el.chatLog.scrollTop = el.chatLog.scrollHeight;
      }
      if (data.research_task) {
        appendMessage("assistant", `🔍 Background research started: “${data.research_task.query}”`, "notice");
        startLiveUpdates();
      }
    } catch (err) {
      pending.remove();
      el.chatError.textContent = err.message;
      el.chatError.hidden = false;
    } finally {
      button.disabled = false;
      el.chatInput.focus();
    }
  });

  el.newSessionBtn.addEventListener("click", () => {
    stopLiveUpdates();
    currentSessionId = null;
    el.sessionView.hidden = true;
    el.homeView.hidden = false;
    refreshSessionList();
    el.searchInput.focus();
  });

  // ---------------------------------------------------------------- startup
  refreshSessionList();
  el.searchInput.focus();
})();
