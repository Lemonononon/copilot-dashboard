// copilot-dashboard frontend (vanilla JS).

const API = "/api";
const state = {
  workspace: "",
  search: "",
  onlyActive: false,
  sessions: [],
  selectedId: null,
  detailCache: new Map(),
  tab: "sessions",
  liveItems: [],          // last activity.tick payload
  serverNow: 0,           // server-reported epoch
  clientAtTick: 0,        // client perf timestamp at tick arrival
  alerts: [],             // last alerts payload
  alertSeen: new Set(),   // ids already toasted
  alertFilter: { error: true, warn: true, info: true },
  showHidden: false,      // when true, hidden sessions are listed (faded)
  hiddenSessions: new Set(),
  hiddenWorkspaces: new Set(),
  archivedSessions: new Set(),
  pinnedSessions: new Set(),
  dismissedAlerts: new Set(),
  scope: "active",        // "active" | "archived"
  selectMode: false,
  selectedSids: new Set(),
  selectedAlertIds: new Set(),
  deepSearch: false,
  deepResults: null,   // {query, results:[…]} when active
};

const $ = (id) => document.getElementById(id);
const el = (tag, attrs = {}, ...children) => {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") e.className = v;
    else if (k === "html") e.innerHTML = v;
    else if (k.startsWith("on") && typeof v === "function") e.addEventListener(k.slice(2), v);
    else if (v === true) e.setAttribute(k, "");
    else e.setAttribute(k, v);
  }
  for (const c of children) {
    if (c == null || c === false) continue;
    e.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
  }
  return e;
};

const fmtTime = (epoch) => epoch ? new Date(epoch * 1000).toLocaleString() : "—";
const fmtAgo = (epoch) => {
  if (!epoch) return "—";
  const s = Math.max(0, Math.floor(Date.now() / 1000 - epoch));
  if (s < 60) return s + "s ago";
  if (s < 3600) return Math.floor(s / 60) + "m ago";
  if (s < 86400) return Math.floor(s / 3600) + "h ago";
  return Math.floor(s / 86400) + "d ago";
};
const fmtElapsed = (epoch, now = Date.now() / 1000) => {
  if (!epoch) return "";
  const s = Math.max(0, Math.floor(now - epoch));
  if (s < 60) return s + "s";
  if (s < 3600) return Math.floor(s / 60) + "m" + String(s % 60).padStart(2, "0") + "s";
  return Math.floor(s / 3600) + "h" + String(Math.floor((s % 3600) / 60)).padStart(2, "0") + "m";
};
const fmtDur = (ms) => {
  if (ms == null) return "";
  if (ms < 1000) return ms + "ms";
  if (ms < 60000) return (ms / 1000).toFixed(1) + "s";
  return Math.floor(ms / 60000) + "m" + Math.round((ms % 60000) / 1000) + "s";
};
const escapeHtml = (s) => String(s).replace(/[&<>"']/g, c => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
}[c]));

// Tool icon hint by name (no real icons; use single-char glyphs)
const toolGlyph = (name) => {
  const map = {
    run_in_terminal: "▶", read_file: "▤", grep_search: "⌕",
    file_search: "⌕", semantic_search: "⌕",
    create_file: "+", replace_string_in_file: "✎",
    multi_replace_string_in_file: "✎",
    vscode_askQuestions: "?", runSubagent: "↳",
    open_browser_page: "◐", screenshot_page: "▣", click_element: "⊙",
    manage_todo_list: "☑", get_terminal_output: "▷", send_to_terminal: "▶",
  };
  return map[name] || "⚙";
};

async function api(path) {
  const r = await fetch(API + path);
  if (!r.ok) throw new Error(`${r.status} ${path}`);
  return r.json();
}

// ---------------- WebSocket live updates ----------------
function connectWS() {
  const dot = $("status-dot");
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(`${proto}//${location.host}/ws`);
  ws.onopen = () => { dot.classList.remove("dead"); dot.classList.add("live"); };
  ws.onclose = () => {
    dot.classList.remove("live"); dot.classList.add("dead");
    setTimeout(connectWS, 2000);
  };
  ws.onerror = () => ws.close();
  ws.onmessage = (ev) => {
    let msg; try { msg = JSON.parse(ev.data); } catch { return; }
    if (msg.type === "session.updated") {
      state.detailCache.delete(msg.session_id);
      scheduleListRefresh();
      if (state.selectedId === msg.session_id) loadDetail(msg.session_id);
    } else if (msg.type === "activity.tick") {
      state.liveItems = msg.items || [];
      state.serverNow = msg.now || (Date.now() / 1000);
      state.clientAtTick = Date.now() / 1000;
      renderLiveStrip();
      // also touch any open detail's now-card (without refetching)
      if (state.selectedId) {
        const item = state.liveItems.find(i => i.session_id === state.selectedId);
        renderNowCard(item);
      }
      // alerts piggyback on the tick
      if (Array.isArray(msg.alerts)) {
        applyAlerts(msg.alerts);
      }
      // throttled list refresh — counts may have changed
      scheduleListRefresh();
    } else if (msg.type === "hidden.updated") {
      state.hiddenSessions = new Set(msg.sessions || []);
      state.hiddenWorkspaces = new Set(msg.workspaces || []);
      state.archivedSessions = new Set(msg.archived_sessions || []);
      state.pinnedSessions = new Set(msg.pinned_sessions || []);
      state.dismissedAlerts = new Set(msg.dismissed_alerts || []);
      refreshHiddenBadge();
      loadSessions();
    }
  };
}

let _refreshT = null;
function scheduleListRefresh() {
  clearTimeout(_refreshT);
  _refreshT = setTimeout(loadSessions, 500);
}

// ---------------- Live strip ----------------
function renderLiveStrip() {
  const strip = $("live-strip");
  const inner = strip.querySelector(".live-strip-inner");
  inner.innerHTML = "";
  const items = state.liveItems.filter(i => i.state !== "idle");
  if (!items.length) {
    strip.hidden = true;
    document.documentElement.style.setProperty("--strip-h", "0px");
    return;
  }
  strip.hidden = false;
  document.documentElement.style.setProperty("--strip-h", "60px");

  for (const it of items.sort(stateSortRank)) {
    inner.appendChild(renderLiveCard(it));
  }
  // also update the only-active filter view
  if (state.onlyActive) renderSessionList();
}

function stateSortRank(a, b) {
  const order = { awaiting_input: 0, running_tool: 1, subagent: 2, thinking: 3, idle: 4 };
  return (order[a.state] ?? 9) - (order[b.state] ?? 9) || (b.last_event_at - a.last_event_at);
}

function renderLiveCard(it) {
  const node = el("div", {
    class: `live-card ${it.state}`,
    onclick: () => { setTab("sessions"); selectSession(it.session_id); },
    title: `${it.workspace?.label || ""}\n${it.label}\n${it.detail || ""}`,
  },
    el("span", { class: "pulse" }),
    el("div", { class: "info" },
      el("div", { class: "ws" }, it.workspace?.short || ""),
      el("div", { class: "lbl" }, it.label || it.state),
      it.detail ? el("div", { class: "det" }, it.detail) : null,
    ),
    el("div", { class: "timer", "data-since": it.since }, fmtElapsed(it.since, currentNow())),
  );
  return node;
}

function currentNow() {
  // server time projected to current wall clock
  if (!state.serverNow) return Date.now() / 1000;
  return state.serverNow + (Date.now() / 1000 - state.clientAtTick);
}

// Update timers every 500ms (covers live strip + now-card + list 'ago')
setInterval(() => {
  const now = currentNow();
  document.querySelectorAll("[data-since]").forEach(node => {
    const since = parseFloat(node.dataset.since);
    node.textContent = fmtElapsed(since, now);
  });
}, 500);

// ---------------- Workspaces / sessions list ----------------
async function loadWorkspaces() {
  const wss = await api("/workspaces");
  const sel = $("ws-filter");
  sel.innerHTML = '<option value="">All workspaces</option>';
  for (const w of wss) {
    const o = el("option", { value: w.hash }, w.short || w.label);
    o.title = w.label;
    sel.appendChild(o);
  }
}

async function loadSessions() {
  const params = new URLSearchParams();
  if (state.workspace) params.set("workspace", state.workspace);
  if (state.showHidden) params.set("include_hidden", "true");
  params.set("scope", state.scope);
  const list = await api("/sessions?" + params);
  state.sessions = list;
  renderSessionList();
  refreshHiddenBadge();
}

async function loadHiddenRegistry() {
  try {
    const h = await api("/hidden");
    state.hiddenSessions = new Set(h.sessions || []);
    state.hiddenWorkspaces = new Set(h.workspaces || []);
    state.archivedSessions = new Set(h.archived_sessions || []);
    state.pinnedSessions = new Set(h.pinned_sessions || []);
    state.dismissedAlerts = new Set(h.dismissed_alerts || []);
    refreshHiddenBadge();
  } catch (e) { /* ignore */ }
}

function refreshHiddenBadge() {
  const btn = $("toggle-hidden");
  if (btn) {
    const n = state.hiddenSessions.size + state.hiddenWorkspaces.size;
    btn.textContent = (state.showHidden ? "\uD83D\uDC41 Hide muted" : "\uD83D\uDEAB Show muted")
                    + (n ? ` (${n})` : "");
    btn.classList.toggle("on", state.showHidden);
    btn.hidden = (n === 0 && !state.showHidden);
  }
  const arc = $("toggle-archived");
  if (arc) {
    const n = state.archivedSessions.size;
    arc.textContent = `\uD83D\uDCE6 ${state.scope === "archived" ? "Back to active" : "Archived"}`
                    + (n ? ` (${n})` : "");
    arc.classList.toggle("on", state.scope === "archived");
    arc.hidden = (n === 0 && state.scope !== "archived");
  }
  updateMuteWsButton();
  refreshBulkBar();
}

function refreshBulkBar() {
  const bar = $("bulk-bar");
  if (!bar) return;
  const cnt = state.tab === "alerts" ? state.selectedAlertIds.size : state.selectedSids.size;
  if (!state.selectMode || cnt === 0) {
    bar.hidden = true;
    return;
  }
  bar.hidden = false;
  bar.querySelector(".bulk-count").textContent = `${cnt} selected`;
  // alerts → only dismiss/cancel; sessions → hide/archive/pin/cancel
  const alertMode = state.tab === "alerts";
  bar.querySelectorAll("[data-bulk]").forEach(b => {
    const k = b.dataset.bulk;
    if (alertMode) b.hidden = !(k === "dismiss" || k === "cancel");
    else            b.hidden = (k === "dismiss");
  });
}

function updateMuteWsButton() {
  const btn = $("mute-workspace");
  if (!btn) return;
  if (!state.workspace) {
    btn.hidden = true;
    return;
  }
  const muted = state.hiddenWorkspaces.has(state.workspace);
  btn.hidden = false;
  btn.textContent = muted ? "\uD83D\uDD0A Unmute WS" : "\uD83D\uDD07 Mute WS";
  btn.classList.toggle("on", muted);
}

async function setHidden(kind, id, hidden) {
  try {
    const r = await fetch(API + "/hidden", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind, id, hidden }),
    });
    if (!r.ok) throw new Error("hide failed");
    const data = await r.json();
    state.hiddenSessions = new Set(data.sessions || []);
    state.hiddenWorkspaces = new Set(data.workspaces || []);
    state.archivedSessions = new Set(data.archived_sessions || []);
    state.pinnedSessions = new Set(data.pinned_sessions || []);
    state.dismissedAlerts = new Set(data.dismissed_alerts || []);
    await loadSessions();
    if (state.tab === "alerts") loadAlerts();
  } catch (e) {
    toast("warn", "Could not update view-state", String(e));
  }
}

async function setBulk(kind, ids, hidden) {
  if (!ids.length) return;
  try {
    const r = await fetch(API + "/hidden/bulk", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind, ids, hidden }),
    });
    if (!r.ok) throw new Error("bulk failed");
    const data = await r.json();
    state.hiddenSessions = new Set(data.sessions || []);
    state.hiddenWorkspaces = new Set(data.workspaces || []);
    state.archivedSessions = new Set(data.archived_sessions || []);
    state.pinnedSessions = new Set(data.pinned_sessions || []);
    state.dismissedAlerts = new Set(data.dismissed_alerts || []);
    state.selectedSids.clear();
    state.selectedAlertIds.clear();
    await loadSessions();
    if (state.tab === "alerts") loadAlerts();
    toast("info", `${kind} updated`, `${ids.length} item${ids.length === 1 ? "" : "s"}`);
  } catch (e) {
    toast("warn", "Bulk action failed", String(e));
  }
}

function passSearch(s) {
  if (state.onlyActive) {
    // require not-idle activity (must be in liveItems with non-idle state)
    const live = state.liveItems.find(i => i.session_id === s.session_id);
    if (!live || live.state === "idle") return false;
  }
  if (!state.search) return true;
  const q = state.search.toLowerCase();
  return [s.first_user_message, s.last_user_message, s.session_id, s.workspace?.label]
    .some(t => t && t.toLowerCase().includes(q));
}

function renderSessionList() {
  if (state.deepSearch && state.deepResults) {
    renderDeepResults();
    return;
  }
  const root = $("session-list");
  root.innerHTML = "";
  const items = state.sessions.filter(passSearch);
  if (!items.length) {
    if (state.scope === "archived") {
      root.appendChild(el("div", { class: "empty-state" },
        el("span", { class: "big-icon" }, "📦"),
        "No archived sessions yet.",
        el("br"), "Use ", el("strong", {}, "Select"), " then ", el("strong", {}, "Archive"),
        " to move sessions here."));
    } else {
      root.appendChild(el("div", { class: "hint" }, "No sessions match."));
    }
    return;
  }
  for (const s of items) {
    const live = state.liveItems.find(i => i.session_id === s.session_id);
    const isLive = live && live.state !== "idle";
    const isHidden = !!s.hidden;
    const isArchived = !!s.archived;
    const isPinned = !!s.pinned;
    const isSelected = state.selectedSids.has(s.session_id);
    const cls = "session-item"
              + (s.session_id === state.selectedId ? " active" : "")
              + (isLive ? " live" : "")
              + (isHidden ? " hidden-row" : "")
              + (isArchived ? " archived-row" : "")
              + (isPinned ? " pinned" : "")
              + (isSelected ? " selected" : "");
    const item = el("div", {
      class: cls,
      onclick: (e) => {
        if (state.selectMode || e.metaKey || e.ctrlKey || e.shiftKey) {
          toggleSelectSession(s.session_id, e.shiftKey);
          return;
        }
        selectSession(s.session_id);
      },
    },
      el("div", { class: "si-title" },
        isPinned ? el("span", { class: "si-pin", title: "pinned" }, "⭐ ") : null,
        isArchived ? el("span", { class: "hidden-mark", title: "archived" }, "📦 ") : null,
        isHidden ? el("span", { class: "hidden-mark", title: "muted" }, "\uD83D\uDEAB ") : null,
        s.first_user_message || "(no user message)",
      ),
      el("div", { class: "si-meta" },
        el("span", { class: "ws", title: s.workspace?.label || "" }, s.workspace?.short || ""),
        isLive ? el("span", { class: "pill live-mini" }, live.label || live.state) : null,
        el("span", {}, fmtAgo(s.last_event_at)),
        el("span", { class: "pill" }, `${s.tool_calls} tools`),
        s.tool_failures ? el("span", { class: "pill fail" }, `${s.tool_failures} fail`) : null,
        s.subagent_calls ? el("span", { class: "pill sub" }, `${s.subagent_calls} sub`) : null,
        renderTodoMini(s.todo),
        el("button", {
          class: "si-mute icon-btn",
          title: isPinned ? "Unpin" : "Pin to top",
          onclick: (e) => { e.stopPropagation(); setHidden("pin", s.session_id, !isPinned); },
        }, isPinned ? "★" : "☆"),
        el("button", {
          class: "si-mute icon-btn",
          title: isArchived ? "Unarchive" : "Archive",
          onclick: (e) => { e.stopPropagation(); setHidden("archive", s.session_id, !isArchived); },
        }, isArchived ? "↩" : "📦"),
        el("button", {
          class: "si-mute icon-btn",
          title: isHidden
            ? (s.hidden_via_workspace ? "Hidden via workspace mute" : "Click to unmute")
            : "Mute this session (no files deleted)",
          onclick: (e) => {
            e.stopPropagation();
            if (s.hidden_via_workspace && isHidden) {
              toast("info", "Workspace muted", "Unmute the workspace to show this session.");
              return;
            }
            setHidden("session", s.session_id, !isHidden);
          },
        }, isHidden ? "\uD83D\uDC41" : "\uD83D\uDEAB"),
      ),
    );
    root.appendChild(item);
  }
}

// ---------------- Deep search (cross-session) ----------------
let _deepSearchT = null;
function scheduleDeepSearch() {
  clearTimeout(_deepSearchT);
  _deepSearchT = setTimeout(runDeepSearch, 400);
}
async function runDeepSearch() {
  const q = state.search;
  if (!state.deepSearch || !q || q.length < 2) {
    state.deepResults = null;
    renderSessionList();
    return;
  }
  try {
    const r = await fetch(API + "/search?q=" + encodeURIComponent(q) + "&limit=50");
    if (!r.ok) throw new Error("search failed");
    state.deepResults = await r.json();
    renderSessionList();
  } catch (e) {
    toast("warn", "Deep search failed", String(e));
  }
}
function highlight(text, q) {
  if (!q) return [document.createTextNode(String(text))];
  const re = new RegExp("(" + q.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + ")", "ig");
  return String(text).split(re).map(part =>
    part && part.toLowerCase() === q.toLowerCase()
      ? el("mark", { class: "search-hl" }, part)
      : document.createTextNode(part)
  );
}
function renderDeepResults() {
  const root = $("session-list");
  root.innerHTML = "";
  const dr = state.deepResults;
  if (!dr.results.length) {
    root.appendChild(el("div", { class: "empty-state" },
      el("span", { class: "big-icon" }, "🔬"),
      `No matches for "${dr.query}" across ${dr.scanned || "all"} sessions.`));
    return;
  }
  root.appendChild(el("div", { class: "hint" },
    `🔬 ${dr.results.length} session${dr.results.length === 1 ? "" : "s"} matched "${dr.query}" (scanned ${dr.scanned})`));
  for (const r of dr.results) {
    const item = el("div", {
      class: "session-item" + (r.session_id === state.selectedId ? " active" : ""),
      onclick: () => {
        selectSession(r.session_id);
        if (r.hits[0] && r.hits[0].step_index != null) {
          setTimeout(() => jumpToStep(r.session_id, r.hits[0].step_index), 350);
        }
      },
    },
      el("div", { class: "si-title" }, ...highlight(r.first_user_message || "(no user message)", dr.query)),
      el("div", { class: "si-meta" },
        el("span", { class: "ws", title: r.workspace?.label || "" }, r.workspace?.short || ""),
        el("span", {}, fmtAgo(r.last_event_at)),
        el("span", { class: "pill" }, `${r.match_count} hit${r.match_count === 1 ? "" : "s"}`),
      ),
    );
    for (const h of r.hits) {
      item.appendChild(el("div", { class: "search-snippet" },
        el("span", { class: "snippet-tag " + h.kind }, h.tool_name || h.kind),
        " ",
        ...highlight(h.snippet, dr.query),
      ));
    }
    root.appendChild(item);
  }
}

let _lastSelectedSid = null;
function toggleSelectSession(sid, isShift) {
  if (isShift && _lastSelectedSid) {
    const items = state.sessions.filter(passSearch);
    const a = items.findIndex(s => s.session_id === _lastSelectedSid);
    const b = items.findIndex(s => s.session_id === sid);
    if (a >= 0 && b >= 0) {
      const [lo, hi] = a < b ? [a, b] : [b, a];
      for (let i = lo; i <= hi; i++) state.selectedSids.add(items[i].session_id);
    }
  } else if (state.selectedSids.has(sid)) {
    state.selectedSids.delete(sid);
  } else {
    state.selectedSids.add(sid);
  }
  _lastSelectedSid = sid;
  if (!state.selectMode) {
    state.selectMode = true;
    document.body.classList.add("select-mode");
    $("toggle-select").classList.add("on");
  }
  renderSessionList();
  refreshBulkBar();
}

// ---------------- Detail ----------------
async function selectSession(sid) {
  state.selectedId = sid;
  renderSessionList();
  await loadDetail(sid);
}

async function loadDetail(sid) {
  const d = await api("/session/" + encodeURIComponent(sid));
  state.detailCache.set(sid, d);
  renderDetail(d);
}

function renderDetail(d) {
  const root = $("session-detail");
  root.innerHTML = "";
  const head = el("div", { class: "detail-head" },
    el("h2", {}, d.first_user_message || "(no user message)"),
    el("div", { class: "detail-meta" },
      d.in_progress
        ? el("span", { class: "badge live" }, "in-progress")
        : el("span", { class: "badge" }, "completed"),
      el("span", { class: "badge ws" }, d.workspace?.short || ""),
      el("span", {}, "started " + fmtTime(d.started_at)),
      el("span", {}, "last "), el("span", { "data-since": d.last_event_at }, fmtElapsed(d.last_event_at, currentNow()) + " ago"),
      el("span", { class: "badge" }, `${d.turns} turns`),
      el("span", { class: "badge" }, `${d.tool_calls} tools`),
      d.tool_failures ? el("span", { class: "badge fail" }, `${d.tool_failures} failed`) : null,
      d.subagent_calls ? el("span", { class: "badge" }, `${d.subagent_calls} subagent`) : null,
      el("span", {}, "copilot " + (d.copilot_version || "?")),
      el("button", {
        class: "icon-btn",
        title: "Copy session id",
        onclick: (e) => copyToClipboard(d.session_id, e.currentTarget, "session id"),
      }, "⧉ id"),
      el("button", {
        class: "icon-btn",
        title: "Generate a 'rehydrate' prompt summarizing this session — paste it into a fresh chat to bring a new agent up to speed.",
        onclick: (e) => copyRehydrate(d.session_id, e.currentTarget),
      }, "🔁 rehydrate"),
    ),
  );
  root.appendChild(head);

  // Now card placeholder
  const nowSlot = el("div", { id: "now-slot" });
  root.appendChild(nowSlot);
  const live = state.liveItems.find(i => i.session_id === d.session_id);
  renderNowCard(live);

  // Progress (todo snapshot)
  if (d.todo) root.appendChild(renderProgressCard(d.todo));

  // Subagent tree (Gantt-mini)
  if (Array.isArray(d.subagents) && d.subagents.length) {
    root.appendChild(renderSubagentsCard(d.subagents, d));
  }

  // Tool stats
  const ts = d.tool_stats || {};
  const names = Object.keys(ts).sort((a, b) => ts[b].count - ts[a].count);
  if (names.length) {
    root.appendChild(el("div", { class: "sect-h" }, "Tool usage"));
    const table = el("table", { class: "tool-table" },
      el("thead", {}, el("tr", {},
        el("th", {}, "Tool"),
        el("th", {}, "Calls"),
        el("th", {}, "Failures"),
        el("th", {}, "Avg"),
        el("th", {}, "Total"),
      )),
    );
    const tb = el("tbody", {});
    for (const n of names) {
      const s = ts[n];
      tb.appendChild(el("tr", {},
        el("td", { class: "name" }, n),
        el("td", { class: "num" }, String(s.count)),
        el("td", { class: "num" + (s.failures ? " err" : "") }, s.failures ? String(s.failures) : "—"),
        el("td", { class: "num" }, fmtDur(s.avg_ms)),
        el("td", { class: "num" }, fmtDur(s.total_ms)),
      ));
    }
    table.appendChild(tb);
    root.appendChild(table);
  }

  // Timeline grouped by turn
  root.appendChild(el("div", { class: "sect-h" }, "Timeline"));
  const groups = groupByTurn(d.steps);
  for (const g of groups) root.appendChild(renderTurnGroup(g, d.session_id));
}

function renderNowCard(item) {
  const slot = $("now-slot");
  if (!slot) return;
  slot.innerHTML = "";
  if (!item || item.state === "idle") return;
  const card = el("div", { class: `now-card ${item.state}` },
    el("span", { class: "pulse" }),
    el("div", { class: "body" },
      el("div", { class: "head" },
        el("span", { class: "state" }, item.state.replace("_", " ")),
        el("span", { class: "timer", "data-since": item.since }, fmtElapsed(item.since, currentNow())),
      ),
      el("div", { class: "lbl" }, item.label || ""),
      item.detail ? el("div", { class: "det" }, item.detail) : null,
    ),
  );
  slot.appendChild(card);
}

// Group consecutive steps by turn_id (steps without turn_id = ungrouped, attach to nearest)
function groupByTurn(steps) {
  const groups = [];
  let cur = null;
  for (const s of steps) {
    const tid = s.turn_id || (cur ? cur.turn_id : "0");
    if (!cur || cur.turn_id !== tid) {
      cur = { turn_id: tid, steps: [] };
      groups.push(cur);
    }
    cur.steps.push(s);
  }
  return groups;
}

function turnSummary(g) {
  // produce a one-line preview
  const userStep = g.steps.find(s => s.kind === "user");
  if (userStep && userStep.text) return "→ " + userStep.text.slice(0, 120);
  const reason = g.steps.find(s => s.kind === "reasoning");
  if (reason && reason.text) return reason.text.slice(0, 120);
  const say = g.steps.find(s => s.kind === "say");
  if (say && say.text) return say.text.slice(0, 120);
  const tool = g.steps.find(s => s.kind === "tool");
  if (tool) return "uses " + (tool.tool_name || "tool");
  return "(turn)";
}

function renderTurnGroup(g, sid) {
  const toolCount = g.steps.filter(s => s.kind === "tool").length;
  const failed = g.steps.filter(s => s.success === false).length;
  // Auto-open the latest turn (last group) by default
  const isLast = (renderTurnGroup._groupIdx = (renderTurnGroup._groupIdx || 0) + 1);
  // reset counter at start of each renderDetail
  // (we use a weak heuristic: set open=true if this is the very last child append)
  const wrap = el("div", { class: "turn-group" },
    el("div", { class: "turn-head" },
      el("span", { class: "arrow" }, "▸"),
      el("span", { class: "turn-id" }, "T" + (g.turn_id || "?")),
      el("span", { class: "summary" }, turnSummary(g)),
      el("span", { class: "stats" },
        toolCount ? el("span", { class: "pill" }, toolCount + " tools") : null,
        failed ? el("span", { class: "pill" }, failed + " failed") : null,
      ),
    ),
    el("div", { class: "turn-body" }, ...g.steps.map(renderStep)),
  );
  const head = wrap.querySelector(".turn-head");
  head.addEventListener("click", () => wrap.classList.toggle("open"));
  return wrap;
}

// open the very last group automatically after all children appended
function openLastTurn(root) {
  const groups = root.querySelectorAll(".turn-group");
  if (groups.length) groups[groups.length - 1].classList.add("open");
}

function renderStep(s) {
  const cls = ["step", s.kind,
               s.is_subagent ? "subagent" : "",
               s.success === false ? "failed" : "",
               (s.kind === "tool" && s.success == null && s.duration_ms == null) ? "running" : ""]
              .filter(Boolean).join(" ");
  const headChildren = [
    el("span", { class: "kind" }, s.is_subagent ? "subagent" : s.kind),
    s.kind === "tool" ? el("span", { class: "tool-name" },
      el("span", { class: "tool-icon" }, toolGlyph(s.tool_name)), " ", s.tool_name || "") : null,
    el("span", { class: "ts" }, fmtTime(s.ts)),
    s.duration_ms != null ? el("span", { class: "dur" }, fmtDur(s.duration_ms)) : null,
    s.success === true ? el("span", { class: "ok" }, "✓") :
      s.success === false ? el("span", { class: "err" }, "✗") :
      (s.kind === "tool") ? el("span", { class: "dur" }, "…") : null,
    s.kind === "user" && s.text ? el("button", {
      class: "icon-btn",
      title: "Copy prompt",
      onclick: (e) => { e.stopPropagation(); copyToClipboard(s.text, e.currentTarget, "prompt"); },
    }, "⧉") : null,
  ];
  const node = el("div", { class: cls }, el("div", { class: "head" }, ...headChildren));

  if (s.kind === "tool") {
    if (s.is_subagent && s.arguments && typeof s.arguments === "object") {
      const prompt = s.arguments.prompt || s.arguments.description;
      if (prompt) node.appendChild(el("div", { class: "subagent-prompt" }, prompt));
    } else if (s.arguments != null) {
      const preview = argPreview(s.tool_name, s.arguments);
      if (preview) node.appendChild(el("div", { class: "arg-preview" }, preview));
    }
    if (s.arguments != null) {
      const detail = el("details", { class: "args" },
        el("summary", {}, "raw arguments"),
        el("pre", { class: "args" }, JSON.stringify(s.arguments, null, 2)),
      );
      node.appendChild(detail);
    }
  } else if (s.kind === "reasoning" && s.text && s.text.length > 240) {
    if (Array.isArray(s.decisions) && s.decisions.length) {
      node.appendChild(renderDecisions(s.decisions));
    }
    const body = el("div", { class: "body collapsed" }, s.text);
    const btn = el("button", { class: "toggle-btn", onclick: () => {
      body.classList.toggle("collapsed");
      btn.textContent = body.classList.contains("collapsed") ? "Show more" : "Show less";
    }}, "Show more");
    node.appendChild(body);
    node.appendChild(btn);
  } else {
    if (s.kind === "reasoning" && Array.isArray(s.decisions) && s.decisions.length) {
      node.appendChild(renderDecisions(s.decisions));
    }
    node.appendChild(el("div", { class: "body" }, s.text || ""));
  }
  return node;
}

function renderDecisions(decisions) {
  const wrap = el("div", { class: "decisions" });
  for (const d of decisions) {
    wrap.appendChild(el("div", { class: "decision dec-" + d.kind, title: d.text },
      el("span", { class: "dec-kind" }, d.kind),
      el("span", { class: "dec-text" }, d.text),
    ));
  }
  return wrap;
}

function argPreview(name, args) {
  if (!args || typeof args !== "object") return "";
  if (args.__truncated__) return args.preview || "";
  switch (name) {
    case "run_in_terminal": return "$ " + (args.command || "");
    case "send_to_terminal": return "↵ " + (args.command || "");
    case "read_file":
      return (args.filePath || "") + (args.startLine ? ` :${args.startLine}-${args.endLine || ""}` : "");
    case "grep_search":
    case "file_search":
    case "semantic_search": return (args.query || args.pattern || "");
    case "create_file":
    case "replace_string_in_file":
    case "multi_replace_string_in_file":
      return (args.filePath || args.explanation || "");
    case "vscode_askQuestions": {
      const q = (args.questions || [])[0];
      return q ? (q.question || q.message || "") : "";
    }
    case "manage_todo_list":
      return `${(args.todoList || []).length} todos`;
    case "open_browser_page": return args.url || "";
    case "click_element": return args.element || args.selector || "";
  }
  // Generic: take first short string field
  for (const [k, v] of Object.entries(args)) {
    if (typeof v === "string" && v.length < 200) return `${k}: ${v}`;
  }
  return "";
}

// ---------------- Stats ----------------
let charts = { tools: null, dur: null };
async function loadStats() {
  const params = new URLSearchParams();
  if (state.workspace) params.set("workspace", state.workspace);
  const s = await api("/stats?" + params);
  $("s-sessions").textContent = s.sessions;
  $("s-active").textContent = s.in_progress;
  $("s-tools").textContent = s.total_tool_calls;
  $("s-fail").textContent = s.total_failures;
  $("s-sub").textContent = s.subagent_calls;
  drawCharts(s);
}

function drawCharts(s) {
  // Top tools: include all
  const tools = s.top_tools.slice(0, 15).reverse();
  const names = tools.map(t => t.name);
  const counts = tools.map(t => t.count);
  const fails = tools.map(t => t.failures || 0);
  // For duration chart, exclude vscode_askQuestions (avg dominated by user wait time)
  const durTools = s.top_tools
    .filter(t => t.name !== "vscode_askQuestions" && t.avg_ms != null)
    .slice(0, 15).reverse();
  const durNames = durTools.map(t => t.name);
  const avg = durTools.map(t => t.avg_ms || 0);

  if (!window.echarts) return;
  charts.tools = charts.tools || echarts.init($("chart-tools"), "dark");
  charts.tools.setOption({
    backgroundColor: "transparent",
    title: { text: "Top tools — calls vs failures", textStyle: { fontSize: 14, fontWeight: 500 } },
    tooltip: { trigger: "axis", axisPointer: { type: "shadow" } },
    legend: { data: ["calls", "failures"], top: 28, textStyle: { color: "#b6c2d2" } },
    grid: { left: 150, right: 30, top: 60, bottom: 30 },
    xAxis: { type: "value", axisLine: { lineStyle: { color: "#374052" } } },
    yAxis: { type: "category", data: names, axisLabel: { color: "#b6c2d2", fontFamily: "ui-monospace" } },
    series: [
      { name: "calls", type: "bar", data: counts, itemStyle: { color: "#3ed1c8", borderRadius: [0, 4, 4, 0] } },
      { name: "failures", type: "bar", data: fails, itemStyle: { color: "#ff6b6b", borderRadius: [0, 4, 4, 0] } },
    ],
  });

  charts.dur = charts.dur || echarts.init($("chart-dur"), "dark");
  charts.dur.setOption({
    backgroundColor: "transparent",
    title: {
      text: "Avg duration per tool (ms)",
      subtext: "excluding vscode_askQuestions (waits for user)",
      textStyle: { fontSize: 14, fontWeight: 500 },
      subtextStyle: { fontSize: 11, color: "#7a869a" },
    },
    tooltip: { trigger: "axis", valueFormatter: v => v + " ms" },
    grid: { left: 150, right: 30, top: 65, bottom: 30 },
    xAxis: { type: "value" },
    yAxis: { type: "category", data: durNames, axisLabel: { color: "#b6c2d2", fontFamily: "ui-monospace" } },
    series: [{ type: "bar", data: avg, itemStyle: { color: "#b18bff", borderRadius: [0, 4, 4, 0] } }],
  });
}
window.addEventListener("resize", () => {
  charts.tools && charts.tools.resize();
  charts.dur && charts.dur.resize();
});

// ---------------- Tab switching ----------------
function setTab(name) {
  state.tab = name;
  document.querySelectorAll(".tab").forEach(b =>
    b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".view").forEach(v =>
    v.classList.toggle("active", v.id === "view-" + name));
  if (name === "stats") loadStats();
}

// ---------------- Boot ----------------
window.addEventListener("DOMContentLoaded", async () => {
  document.querySelectorAll(".tab").forEach(b =>
    b.addEventListener("click", () => setTab(b.dataset.tab)));
  $("ws-filter").addEventListener("change", e => {
    state.workspace = e.target.value; loadSessions(); if (state.tab === "stats") loadStats();
    updateMuteWsButton();
  });
  $("search").addEventListener("input", e => {
    state.search = e.target.value;
    if (state.deepSearch) scheduleDeepSearch();
    else renderSessionList();
  });
  $("deep-search").addEventListener("click", () => {
    state.deepSearch = !state.deepSearch;
    $("deep-search").classList.toggle("on", state.deepSearch);
    if (state.deepSearch) {
      if (state.search.length >= 2) runDeepSearch();
      else toast("info", "Deep search on", "Type at least 2 characters in the search box.");
    } else {
      state.deepResults = null;
      renderSessionList();
    }
  });
  $("only-active").addEventListener("change", e => {
    state.onlyActive = e.target.checked; renderSessionList();
  });
  $("toggle-hidden").addEventListener("click", () => {
    state.showHidden = !state.showHidden;
    refreshHiddenBadge();
    loadSessions();
  });
  $("mute-workspace").addEventListener("click", () => {
    if (!state.workspace) {
      toast("info", "Pick a workspace first", "Use the dropdown above to select one, then mute it.");
      return;
    }
    const isMuted = state.hiddenWorkspaces.has(state.workspace);
    setHidden("workspace", state.workspace, !isMuted);
  });

  // archived scope toggle
  $("toggle-archived").addEventListener("click", () => {
    state.scope = state.scope === "archived" ? "active" : "archived";
    refreshHiddenBadge();
    loadSessions();
  });

  // select mode toggle
  $("toggle-select").addEventListener("click", () => {
    state.selectMode = !state.selectMode;
    document.body.classList.toggle("select-mode", state.selectMode);
    $("toggle-select").classList.toggle("on", state.selectMode);
    if (!state.selectMode) {
      state.selectedSids.clear();
      state.selectedAlertIds.clear();
      renderSessionList();
      if (state.tab === "alerts") renderAlerts();
    }
    refreshBulkBar();
  });

  // bulk-bar buttons
  $("bulk-bar").addEventListener("click", (e) => {
    const btn = e.target.closest("[data-bulk]");
    if (!btn) return;
    const action = btn.dataset.bulk;
    const sids = [...state.selectedSids];
    const aids = [...state.selectedAlertIds];
    if (action === "cancel") {
      state.selectedSids.clear(); state.selectedAlertIds.clear();
      state.selectMode = false;
      document.body.classList.remove("select-mode");
      $("toggle-select").classList.remove("on");
      renderSessionList();
      if (state.tab === "alerts") renderAlerts();
      refreshBulkBar();
      return;
    }
    if (action === "dismiss")   return setBulk("alert",   aids, true);
    if (action === "hide")      return setBulk("session", sids, true);
    if (action === "unhide")    return setBulk("session", sids, false);
    if (action === "archive")   return setBulk("archive", sids, true);
    if (action === "unarchive") return setBulk("archive", sids, false);
    if (action === "pin")       return setBulk("pin",     sids, true);
    if (action === "unpin")     return setBulk("pin",     sids, false);
  });

  await loadHiddenRegistry();
  await loadWorkspaces();
  await loadSessions();
  // initial activity poll (WS will keep it fresh)
  try {
    const a = await api("/activity");
    state.liveItems = a.items || []; state.serverNow = a.now || (Date.now() / 1000);
    state.clientAtTick = Date.now() / 1000;
    renderLiveStrip();
  } catch {}
  connectWS();
  setInterval(loadSessions, 15000);
});

// patch renderDetail to open last turn after render
const _renderDetail_orig = renderDetail;
renderDetail = function (d) {
  renderTurnGroup._groupIdx = 0;
  _renderDetail_orig(d);
  openLastTurn($("session-detail"));
};

// ========================================================
// v3 — todo progress, subagent gantt, alerts, toasts, keys
// ========================================================

function renderTodoMini(todo) {
  if (!todo || !todo.counts || !todo.counts.total) return null;
  const c = todo.counts;
  const pct = c.total ? Math.round((c.completed / c.total) * 100) : 0;
  const wrap = el("span", { class: "todo-mini",
    title: `${c.completed} done · ${c.in_progress} active · ${c.not_started} pending` });
  const bar = el("span", { class: "bar-mini" });
  bar.appendChild(el("span", { style: `width:${pct}%` }));
  wrap.appendChild(bar);
  wrap.appendChild(document.createTextNode(`${c.completed}/${c.total}`));
  return wrap;
}

function renderProgressCard(todo) {
  const c = todo.counts;
  const card = el("div", { class: "progress-card" });
  card.appendChild(el("div", { class: "ph" },
    el("span", { class: "ph-title" }, "Task progress (manage_todo_list)"),
    el("span", { class: "ph-count" },
      `${c.completed} done · ${c.in_progress} active · ${c.not_started} pending · ${c.total} total`),
  ));
  const bar = el("div", { class: "progress-bar" });
  const widths = {
    completed:  c.total ? (c.completed   / c.total) * 100 : 0,
    in_progress:c.total ? (c.in_progress / c.total) * 100 : 0,
    not_started:c.total ? (c.not_started / c.total) * 100 : 0,
  };
  for (const k of ["completed", "in_progress", "not_started"]) {
    bar.appendChild(el("span", { class: k, style: `width:${widths[k]}%` }));
  }
  card.appendChild(bar);
  const list = el("ul", { class: "todo-list" });
  for (const it of todo.items) {
    const mark = it.status === "completed" ? "✓" :
                 it.status === "in_progress" ? "◐" : "○";
    list.appendChild(el("li", { class: it.status },
      el("span", { class: "mark" }, mark),
      el("span", {}, it.title)));
  }
  card.appendChild(list);
  return card;
}

function renderSubagentsCard(subs, d) {
  const card = el("div", { class: "subagents-card" });
  // header
  const total = subs.length;
  const failed = subs.filter(s => s.success === false).length;
  const running = subs.filter(s => s.success === null && s.duration_ms === null).length;
  card.appendChild(el("div", { class: "subagents-head" },
    el("span", { class: "title" },
      `Subagents — ${total} call${total === 1 ? "" : "s"}` +
      (failed ? ` · ${failed} failed` : "") +
      (running ? ` · ${running} running` : "")),
    el("span", { class: "ph-count" },
      `total ${fmtDur(subs.reduce((a, s) => a + (s.duration_ms || 0), 0))}`),
  ));
  // gantt mini — relative to session
  const t0 = subs.reduce((a, s) => Math.min(a, s.started_at || a), subs[0].started_at);
  const t1 = subs.reduce((a, s) => Math.max(a, (s.started_at || 0) + (s.duration_ms || 0) / 1000), t0 + 1);
  const span = Math.max(1, t1 - t0);
  const gantt = el("div", { class: "gantt" });
  // limit to 30 visible to keep layout sane; collapse rest
  const visible = subs.slice(0, 30);
  for (const sa of visible) {
    const x0 = ((sa.started_at - t0) / span) * 100;
    const w = Math.max(0.4, ((sa.duration_ms || 0) / 1000 / span) * 100);
    const cls = "gantt-row" + (sa.success === false ? " failed" :
                              (sa.success === null && sa.duration_ms === null) ? " running" : "");
    const row = el("div", {
      class: cls,
      title: (sa.description || sa.prompt || "").slice(0, 400),
      onclick: () => jumpToStep(d.session_id, sa.step_index),
    },
      el("span", { class: "name" },
        toolGlyph(sa.tool_name) + " ",
        sa.description || sa.tool_name),
      el("div", { class: "track" },
        el("span", { class: "bar", style: `left:${x0}%; width:${w}%` })),
      el("span", { class: "dur" }, sa.duration_ms != null ? fmtDur(sa.duration_ms) : "…"),
    );
    gantt.appendChild(row);
  }
  if (subs.length > 30) {
    gantt.appendChild(el("div", { class: "gantt-empty" },
      `+ ${subs.length - 30} more (scroll the timeline below)`));
  }
  card.appendChild(gantt);
  return card;
}

function jumpToStep(sid, idx) {
  if (idx == null) return;
  const root = $("session-detail");
  // ensure all turns open so the index resolves
  root.querySelectorAll(".turn-group").forEach(g => g.classList.add("open"));
  const steps = root.querySelectorAll(".turn-body .step");
  const node = steps[idx];
  if (node) {
    node.scrollIntoView({ behavior: "smooth", block: "center" });
    node.classList.add("flash");
    setTimeout(() => node.classList.remove("flash"), 1600);
  }
}

// ---------------- Alerts ----------------
function applyAlerts(items) {
  // toast new error/warn alerts
  const newErrWarn = [];
  for (const a of items) {
    if (!state.alertSeen.has(a.id)) {
      state.alertSeen.add(a.id);
      if (a.severity !== "info") newErrWarn.push(a);
    }
  }
  state.alerts = items;
  // badge
  const errs = items.filter(a => a.severity !== "info").length;
  const badge = $("alert-badge");
  if (badge) {
    badge.textContent = errs;
    badge.hidden = errs === 0;
  }
  updateFavicon(errs);
  // toast (cap at 3 at once)
  for (const a of newErrWarn.slice(0, 3)) {
    toast(a.severity, a.label, `${a.workspace?.short || ""} · ${a.kind}`);
  }
  if (state.tab === "alerts") renderAlerts();
}

async function loadAlerts() {
  try {
    const a = await api("/alerts");
    applyAlerts(a.items || []);
    // clear seen so we don't re-toast entries already loaded on cold start
    state.alertSeen = new Set((a.items || []).map(x => x.id));
  } catch {}
}

function renderAlerts() {
  const root = $("alerts-list");
  const summary = $("alerts-summary");
  if (!root) return;
  root.innerHTML = "";
  const items = state.alerts.filter(a => state.alertFilter[a.severity]);
  const total = state.alerts.length;
  const errs = state.alerts.filter(a => a.severity === "error").length;
  const warns = state.alerts.filter(a => a.severity === "warn").length;
  const infos = state.alerts.filter(a => a.severity === "info").length;
  summary.textContent = total
    ? `${total} alerts — ${errs} error · ${warns} warn · ${infos} info`
    : "No alerts.";
  if (!items.length) {
    root.appendChild(el("div", { class: "alerts-empty" },
      total ? "No alerts match the current filter." : "Nothing to flag right now."));
    return;
  }
  for (const a of items) {
    root.appendChild(renderAlertRow(a));
  }
}

function renderAlertRow(a) {
  const isSelected = state.selectedAlertIds.has(a.id);
  return el("div", {
    class: `alert-row ${a.severity}` + (isSelected ? " selected" : ""),
    onclick: (e) => {
      if (state.selectMode || e.metaKey || e.ctrlKey) {
        toggleSelectAlert(a.id);
        return;
      }
      setTab("sessions");
      selectSession(a.session_id);
      if (a.step_index != null) {
        setTimeout(() => jumpToStep(a.session_id, a.step_index), 300);
      }
    },
  },
    el("div", { class: "severity" }),
    el("div", { class: "alert-main" },
      el("div", { class: "alert-head" },
        el("span", { class: "alert-kind " + a.severity }, a.kind),
        el("span", { class: "alert-label" }, a.label),
        el("span", { class: "alert-ws" }, a.workspace?.short || ""),
      ),
      a.hint ? el("div", { class: "alert-hint" }, a.hint) : null,
    ),
    el("div", { class: "alert-meta" },
      el("span", { class: "session" }, a.session_label || a.session_id.slice(0, 8)),
      el("span", {}, fmtAgo(a.ts)),
      el("div", { class: "alert-actions" },
        el("button", {
          class: "icon-btn",
          title: "Dismiss this alert (won't be re-shown)",
          onclick: (e) => { e.stopPropagation(); setHidden("alert", a.id, true); },
        }, "✕"),
      ),
    ),
  );
}

function toggleSelectAlert(aid) {
  if (state.selectedAlertIds.has(aid)) state.selectedAlertIds.delete(aid);
  else state.selectedAlertIds.add(aid);
  if (!state.selectMode) {
    state.selectMode = true;
    document.body.classList.add("select-mode");
    $("toggle-select").classList.add("on");
  }
  renderAlerts();
  refreshBulkBar();
}

// ---------------- Toasts ----------------
function toast(severity, title, body, ttl = 6000) {
  const host = $("toast-host");
  if (!host) return;
  const t = el("div", { class: `toast ${severity}` },
    el("div", { class: "title" }, title),
    body ? el("div", { class: "body" }, body) : null,
  );
  host.appendChild(t);
  setTimeout(() => {
    t.style.transition = "opacity .3s, transform .3s";
    t.style.opacity = "0";
    t.style.transform = "translateX(20px)";
    setTimeout(() => t.remove(), 300);
  }, ttl);
}

// ---------------- Keyboard shortcuts ----------------
window.addEventListener("keydown", (e) => {
  if (e.target.matches("input, textarea, select")) return;
  if (e.key === "/") {
    e.preventDefault();
    $("search").focus();
  } else if (e.key === "j" || e.key === "k") {
    const items = state.sessions.filter(passSearch);
    if (!items.length) return;
    let idx = items.findIndex(s => s.session_id === state.selectedId);
    if (idx < 0) idx = 0;
    else idx = e.key === "j" ? Math.min(items.length - 1, idx + 1) : Math.max(0, idx - 1);
    selectSession(items[idx].session_id);
  } else if (e.key === "g") {
    setTab("sessions");
  } else if (e.key === "a") {
    setTab("alerts");
  } else if (e.key === "s") {
    setTab("stats");
  } else if (e.key === "m") {
    $("toggle-select").click();
  } else if (e.key === "Escape") {
    if (state.selectMode) {
      state.selectMode = false;
      state.selectedSids.clear();
      state.selectedAlertIds.clear();
      document.body.classList.remove("select-mode");
      $("toggle-select").classList.remove("on");
      renderSessionList();
      if (state.tab === "alerts") renderAlerts();
      refreshBulkBar();
    }
    if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
  }
});

// patch setTab to load alerts on demand
const _setTab_orig = setTab;
setTab = function(name) {
  _setTab_orig(name);
  refreshBulkBar();
  if (name === "alerts") {
    if (!state.alerts.length) loadAlerts();
    else renderAlerts();
  }
};

// alert filter checkboxes
window.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll(".alerts-filter input").forEach(cb => {
    cb.addEventListener("change", () => {
      state.alertFilter[cb.dataset.sev] = cb.checked;
      renderAlerts();
    });
  });
  // also load alerts initially so the badge shows up
  setTimeout(loadAlerts, 200);
});

// === copy-to-clipboard helper with inline confirmation ===
async function copyToClipboard(text, btn, label) {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
    } else {
      // fallback for non-https/local
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      document.body.removeChild(ta);
    }
    if (btn) {
      const orig = btn.textContent;
      btn.textContent = "✓ copied";
      btn.classList.add("copied");
      setTimeout(() => { btn.textContent = orig; btn.classList.remove("copied"); }, 1400);
    }
  } catch (e) {
    if (btn) { btn.textContent = "× failed"; }
    console.error("copy failed", e);
  }
}

// === favicon alert state ===
// Renders a tiny circle SVG as data URL; red dot when there are warn+ alerts.
function updateFavicon(errCount) {
  const link = document.querySelector("link[rel='icon']") || (() => {
    const l = document.createElement("link");
    l.rel = "icon";
    document.head.appendChild(l);
    return l;
  })();
  const dot = errCount > 0 ? "#ef4444" : "#7c3aed";
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">
    <circle cx="16" cy="16" r="14" fill="${dot}"/>
    ${errCount > 0
      ? `<text x="16" y="22" text-anchor="middle" font-family="-apple-system,sans-serif" font-size="16" font-weight="700" fill="#fff">${errCount > 9 ? "!" : errCount}</text>`
      : `<circle cx="16" cy="16" r="6" fill="#fff" opacity="0.85"/>`}
  </svg>`;
  link.type = "image/svg+xml";
  link.href = "data:image/svg+xml;utf8," + encodeURIComponent(svg);
}

// initial favicon (no alerts yet)
updateFavicon(0);

async function copyRehydrate(sid, btn) {
  try {
    const r = await fetch(API + "/session/" + encodeURIComponent(sid) + "/rehydrate");
    if (!r.ok) throw new Error("rehydrate failed");
    const d = await r.json();
    await copyToClipboard(d.markdown, btn, "rehydrate prompt");
    toast("info", "Rehydrate prompt copied", `${d.char_count} chars — paste into a fresh chat to continue this session.`);
  } catch (e) {
    toast("warn", "Could not generate rehydrate prompt", String(e));
  }
}
