/* Logs page: follow the JupyterHub / console log with cursor polling.
   Every piece of log text is inserted with textContent, never as HTML. */
(function () {
  "use strict";
  const MAX_LINES = 2000;      // lines kept in the view (oldest dropped first)
  const POLL_MS = 2000;
  const CATCH_UP_ROUNDS = 5;   // extra fetches in one poll when the server says more is waiting
  const $ = (id) => document.getElementById(id);
  const els = {
    source: $("logs-source"), filter: $("logs-filter"), level: $("logs-level"), autoscroll: $("logs-autoscroll"),
    clear: $("logs-clear"), note: $("logs-note"), alert: $("logs-alert"), alertText: $("logs-alert-text"), retry: $("logs-retry"),
    view: $("logs-view"), empty: $("logs-empty"), emptyTitle: $("logs-empty-title"), emptyText: $("logs-empty-text"),
    emptyClear: $("logs-empty-clear"), jump: $("logs-jump"), status: $("logs-status"), zone: $("logs-zone"), loading: $("logs-loading"),
    download: $("logs-download"), files: $("logs-files"),
  };
  if (!els.view || !window.CC) return;

  const LEVEL_RANK = { DEBUG: 0, OTHER: 1, INFO: 1, WARNING: 2, ERROR: 3, CRITICAL: 3 };
  const MIN_RANK = { all: 0, warn: 2, error: 3 };
  const LEVEL_CLASS = { DEBUG: "is-debug", WARNING: "is-warning", ERROR: "is-error", CRITICAL: "is-error is-critical", OTHER: "is-other" };
  const SOURCE_LABEL = { hub: "JupyterHub", console: "Teacher console" };
  const SOURCE_FILE = { hub: "jupyterhub.log", console: "console.log" };

  const state = {
    source: "hub", filter: "", level: "all",
    cursor: null,          // byte offset to poll from; null = (re)load the tail
    lines: [],             // [{ entry, el }] oldest first, capped at MAX_LINES
    truncated: false,      // older lines exist beyond what is shown
    cleared: false,        // the teacher emptied the view; only lines that arrived since are shown
    failed: false,
    updated: null,         // time of the last good response; null until the first one (or after a source change)
    utcOffset: null,       // the server clock's offset from UTC in seconds (log stamps are server time)
    generation: 0,         // bumps on every reload so stale responses are ignored
  };

  // --- helpers ------------------------------------------------------------
  const span = (cls, text) => { const s = document.createElement("span"); s.className = cls; s.textContent = text; return s; };
  const fmt = (n) => Number(n).toLocaleString();
  const plural = (n, word) => `${fmt(n)} ${word}${n === 1 ? "" : "s"}`;
  // Log stamps are the server's clock (the container runs on UTC), so convert them to the browser's local time:
  // "12:14:00" for today's lines, "Sep 12 12:14:00" for older ones; the stamp as written sits in the tooltip.
  const pad = (n) => String(n).padStart(2, "0");
  const browserOffset = () => -new Date().getTimezoneOffset() * 60;
  const zoneLabel = (sec) => { const a = Math.abs(sec); return sec ? `UTC${sec < 0 ? "-" : "+"}${pad(Math.floor(a / 3600))}:${pad(Math.floor((a % 3600) / 60))}` : "UTC"; };
  const parseTs = (ts) => {
    const m = /^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})/.exec(ts || "");
    if (!m || state.utcOffset === null) return null;
    const d = new Date(Date.UTC(+m[1], m[2] - 1, +m[3], +m[4], +m[5], +m[6]) - state.utcOffset * 1000);
    return isNaN(d) ? null : d;
  };
  const sameDay = (a, b) => a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
  const hms = (d) => `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  const shortTs = (ts) => {
    const d = parseTs(ts);
    if (!d) return ts;
    return sameDay(d, new Date()) ? hms(d) : `${d.toLocaleDateString(undefined, { month: "short", day: "numeric" })} ${hms(d)}`;
  };
  const tsTitle = (ts) => (state.utcOffset === null ? ts : `${ts} on the server's clock (${zoneLabel(state.utcOffset)})`);
  const clock = (d) => d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  const visible = (entry) => (LEVEL_RANK[entry.level] ?? 1) >= MIN_RANK[state.level]
    && (!state.filter || (entry.raw || "").toLowerCase().includes(state.filter.toLowerCase()));

  function lineEl(entry) {
    const div = document.createElement("div");
    div.className = "cc-log__line " + (LEVEL_CLASS[entry.level] || "");
    div.dataset.level = entry.level;
    if (entry.ts) { const ts = span("ts", shortTs(entry.ts)); ts.title = tsTitle(entry.ts); div.appendChild(ts); }
    if (entry.level && entry.level !== "OTHER") div.appendChild(span("lvl", entry.level));
    if (entry.logger) div.appendChild(span("src", entry.logger));
    div.appendChild(span("msg", entry.msg));
    div.hidden = !visible(entry);
    return div;
  }

  function atBottom() { return els.view.scrollHeight - els.view.scrollTop - els.view.clientHeight < 12; }
  function scrollToBottom() { els.view.scrollTop = els.view.scrollHeight; els.jump.hidden = true; }
  function shownCount() { return state.lines.reduce((n, l) => n + (l.el.hidden ? 0 : 1), 0); }

  function append(entries, { replace = false, flash = false } = {}) {
    if (replace) { els.view.replaceChildren(); state.lines = []; }
    const frag = document.createDocumentFragment();
    for (const entry of entries) {
      const el = lineEl(entry);
      if (flash) el.classList.add("is-new");
      state.lines.push({ entry, el });
      frag.appendChild(el);
    }
    els.view.appendChild(frag);
    while (state.lines.length > MAX_LINES) { state.lines.shift().el.remove(); state.truncated = true; }
    refresh();
    if (els.autoscroll.checked) scrollToBottom();
    else if (entries.length && !atBottom()) els.jump.hidden = false;
  }

  // Traceback lines that reach us one poll after their header come back flagged "continues" (with that header's
  // time, level and logger). Join them to the entry they belong to when it is the last one here; otherwise they
  // show as their own line, still red and still found by the level and text filters.
  const headerOf = (raw) => (raw || "").split("\n", 1)[0];
  function absorb(entries) {
    const first = entries[0];
    const last = state.lines[state.lines.length - 1];
    if (!first || !first.continues || !first.msg || !last || headerOf(last.entry.raw) !== headerOf(first.raw)) return entries;
    last.entry.msg += `\n${first.msg}`;
    last.entry.raw += `\n${first.msg}`;
    const el = lineEl(last.entry);
    el.classList.add("is-new");
    last.el.replaceWith(el);
    last.el = el;
    return entries.slice(1);
  }

  function applyVisibility() {
    for (const { entry, el } of state.lines) el.hidden = !visible(entry);
    refresh();
    if (els.autoscroll.checked) scrollToBottom();
  }

  function refresh() {
    const total = state.lines.length;
    const shown = shownCount();
    // note in the toolbar: how much of the log this is
    let note;
    if (state.cleared) note = total ? `${plural(total, "new line")} since clearing` : "View cleared";
    else if (total === 0) note = state.filter ? `No lines match “${state.filter}”` : "No lines yet";
    else note = `${state.truncated ? "Last" : "All"} ${plural(total, "line")}` + (state.filter ? ` matching “${state.filter}”` : "");
    if (shown !== total) note += ` · ${fmt(total - shown)} hidden by level`;
    CC.setText(els.note, note);
    // loading line until the first good response; the empty state only once we know the log really is empty,
    // and never underneath the error banner (they would contradict each other)
    const loading = !state.failed && state.updated === null;
    els.loading.hidden = !loading;
    const empty = shown === 0 && !loading && !state.failed;
    els.empty.hidden = !empty;
    els.emptyClear.hidden = !(empty && total > 0 && state.level !== "all") && !(empty && total === 0 && state.filter);
    if (empty) {
      let title, text;
      if (state.cleared && total === 0) { title = "View cleared"; text = "New lines will appear here as they arrive."; }
      else if (total === 0 && state.filter) { title = `No lines match “${state.filter}”`; text = "Try a shorter word, or clear the filter to see everything."; }
      else if (total === 0) { title = "No log lines yet"; text = `The ${SOURCE_LABEL[state.source]} log is empty. New lines will appear here as the server writes them.`; }
      else { title = "Nothing at this level"; text = `None of the ${plural(total, "line")} here is ${state.level === "error" ? "an error" : "a warning or an error"}. That is usually good news.`; }
      CC.setText(els.emptyTitle, title); CC.setText(els.emptyText, text);
      CC.setText(els.emptyClear, total === 0 ? "Clear filter" : "Show all levels");
    }
    // footer status
    if (state.failed) { els.status.className = "is-paused"; CC.setText(els.status, "Paused — can't read the log right now. Retrying…"); }
    else if (state.updated) { els.status.className = ""; CC.setText(els.status, `Following the ${SOURCE_LABEL[state.source]} log · updated ${clock(state.updated)} · ${plural(shown, "line")} shown`); }
    else { els.status.className = ""; CC.setText(els.status, "Loading the log…"); }
    // say so when the server's clock is in another zone than this browser (its stamps are converted for display)
    const otherZone = state.utcOffset !== null && state.utcOffset !== browserOffset();
    els.zone.hidden = !otherZone;
    if (otherZone) CC.setText(els.zone, `Times are shown in your local time; the server's clock is on ${zoneLabel(state.utcOffset)}.`);
  }

  function takeOffset(r) { if (typeof r.utc_offset === "number") state.utcOffset = r.utc_offset; }

  function setFailed(err) {
    state.failed = true;
    els.alert.hidden = false;
    const detail = err && err.code === "network" ? "The console is not answering — it may be restarting."
      : err && err.message ? err.message : "";
    CC.setText(els.alertText, `Can't read the log file right now. ${detail}`.trim());
    refresh();
  }
  function setOk() {
    if (state.failed) { state.failed = false; els.alert.hidden = true; }
    state.updated = new Date();
    refresh();
  }

  function query(params) {
    const q = new URLSearchParams({ source: state.source, limit: String(MAX_LINES) });
    if (state.filter) q.set("filter", state.filter);
    if (params.after !== undefined) q.set("after", String(params.after));
    return `logs?${q.toString()}`;
  }

  async function loadTail(signal) {
    const generation = ++state.generation;
    const r = await CC.api("GET", query({}), null, { signal });
    if (generation !== state.generation) return;
    takeOffset(r);
    state.cursor = r.cursor;
    state.truncated = r.truncated;
    state.cleared = false;
    append(r.lines, { replace: true });
    setOk();
  }

  async function loadMore(signal) {
    for (let round = 0; round < CATCH_UP_ROUNDS; round++) {
      const generation = state.generation;
      const r = await CC.api("GET", query({ after: state.cursor }), null, { signal });
      if (generation !== state.generation) return;
      takeOffset(r);
      state.cursor = r.cursor;
      if (r.rotated) {
        state.truncated = r.truncated; state.cleared = false;
        append(r.lines, { replace: true });
        CC.toast("The log file was rotated — showing the newest file.", { kind: "info" });
        setOk();
        loadFiles();                                     // the older-files menu has a new entry now
        return;
      }
      if (r.lines.length) append(absorb(r.lines), { flash: true });
      setOk();
      if (!r.truncated) return;
    }
  }

  // A reload runs on its own controller; the poller simply carries on with the new cursor afterwards.
  let direct = null;
  async function reload() {
    if (direct) direct.abort();
    direct = new AbortController();
    state.cursor = null;
    try { await loadTail(direct.signal); } catch (e) { if (e.name !== "AbortError" && e.status !== 401) setFailed(e); }
  }

  // --- controls -----------------------------------------------------------
  // The header button is a plain link; grey it out instead of sending the teacher to a JSON 404 when there is no file yet.
  function setDownloadable(ok) {
    els.download.classList.toggle("disabled", !ok);
    els.download.setAttribute("aria-disabled", String(!ok));
    els.download.tabIndex = ok ? 0 : -1;
    els.download.title = ok
      ? `Download the ${SOURCE_LABEL[state.source]} log (${SOURCE_FILE[state.source]})`
      : `There is no ${SOURCE_LABEL[state.source]} log file yet`;
  }

  function applySource(source) {
    state.source = SOURCE_LABEL[source] ? source : "hub";
    els.source.value = state.source;
    els.download.href = `${CC.prefix}/api/logs/download?source=${state.source}`;
    setDownloadable(true);
    loadFiles();
  }

  function syncUrl() {
    const q = new URLSearchParams();
    if (state.source !== "hub") q.set("source", state.source);
    if (state.filter) q.set("filter", state.filter);
    const qs = q.toString();
    try { history.replaceState(null, "", location.pathname + (qs ? `?${qs}` : "")); } catch (e) { /* not important */ }
  }

  async function loadFiles() {
    const source = state.source;
    const textItem = (text) => { const li = document.createElement("li"); const s = document.createElement("span"); s.className = "dropdown-item-text cc-muted"; s.textContent = text; li.appendChild(s); return li; };
    try {
      const r = await CC.api("GET", `logs/files?source=${source}`);
      if (source !== state.source) return;
      const items = [];
      const header = document.createElement("li"); const h = document.createElement("h6"); h.className = "dropdown-header"; h.textContent = `${SOURCE_LABEL[source]} log files`; header.appendChild(h); items.push(header);
      for (const f of r.files) {
        const li = document.createElement("li");
        const a = document.createElement("a");
        a.className = "dropdown-item"; a.href = `${CC.prefix}/api/logs/download?file=${encodeURIComponent(f.name)}`;
        const part = /\.log\.(\d+)$/.exec(f.name);
        const name = document.createElement("span"); name.textContent = f.current ? "Current log" : `Older log ${part ? part[1] : ""}`.trim();
        const meta = document.createElement("small"); meta.textContent = `${CC.bytes(f.size)} · ${new Date(f.modified).toLocaleDateString(undefined, { month: "short", day: "numeric" })}`;
        a.append(name, meta); li.appendChild(a); items.push(li);
      }
      if (!r.files.length) items.push(textItem("No log files yet."));
      els.files.replaceChildren(...items);
      setDownloadable(r.files.some((f) => f.current));
    } catch (e) {
      if (source !== state.source) return;
      els.files.replaceChildren(textItem("Couldn't list the log files."));
    }
  }

  els.source.addEventListener("change", () => {
    applySource(els.source.value);
    state.lines = []; els.view.replaceChildren(); state.truncated = false; state.cleared = false; els.jump.hidden = true;
    state.updated = null;                                // back to "Loading…" until the other log answers
    syncUrl();
    reload();
  });

  let filterTimer = null;
  els.filter.addEventListener("input", () => {
    clearTimeout(filterTimer);
    filterTimer = setTimeout(() => {
      const value = els.filter.value.trim().slice(0, 200);
      if (value === state.filter) return;
      state.filter = value;
      applyVisibility();                                 // instant, on what is already here
      syncUrl();
      reload();                                          // then ask the server for the last lines that match
    }, 300);
  });
  els.filter.addEventListener("keydown", (e) => { if (e.key === "Escape" && els.filter.value) { els.filter.value = ""; els.filter.dispatchEvent(new Event("input")); } });

  els.level.addEventListener("change", () => { state.level = els.level.value; applyVisibility(); });

  els.autoscroll.addEventListener("change", () => { if (els.autoscroll.checked) scrollToBottom(); else if (!atBottom()) els.jump.hidden = false; });

  els.view.addEventListener("scroll", () => {
    const bottom = atBottom();
    els.jump.hidden = bottom;
    if (bottom !== els.autoscroll.checked) els.autoscroll.checked = bottom;   // scrolling up pauses following; back at the bottom resumes it
  });

  els.jump.addEventListener("click", () => { els.autoscroll.checked = true; scrollToBottom(); els.view.focus({ preventScroll: true }); });

  els.clear.addEventListener("click", () => {
    state.lines = []; els.view.replaceChildren(); state.cleared = true; state.truncated = false; els.jump.hidden = true;
    refresh();
  });

  els.emptyClear.addEventListener("click", () => {
    els.level.value = "all"; state.level = "all";
    if (state.filter) { els.filter.value = ""; state.filter = ""; syncUrl(); applyVisibility(); reload(); } else applyVisibility();
    els.filter.focus();
  });

  els.retry.addEventListener("click", () => {
    els.alert.hidden = true; state.failed = false; state.updated = null;   // show "Loading…" while we try
    refresh();
    reload();
  });

  // --- start --------------------------------------------------------------
  const params = new URLSearchParams(location.search);
  const initialFilter = (params.get("filter") || "").trim().slice(0, 200);
  if (initialFilter) { state.filter = initialFilter; els.filter.value = initialFilter; }
  applySource(params.get("source") || "hub");
  refresh();

  const poller = CC.poll(async (signal) => {
    try {
      if (state.cursor === null) await loadTail(signal); else await loadMore(signal);
    } catch (e) {
      if (e.name === "AbortError" || e.status === 401) return;   // 401: CC.api is already sending us to the login page
      setFailed(e);
      throw e;                                                   // let CC.poll back off
    }
  }, POLL_MS);
  window.addEventListener("pagehide", () => poller.stop());
})();
