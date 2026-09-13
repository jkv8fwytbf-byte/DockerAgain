/* Dashboard: live tiles and the student table, patched in place every 5 s.
   Rows are keyed by data-user and only changed cells are touched, so sort,
   search and keyboard focus survive each refresh. */
(function () {
  "use strict";
  const CC = window.CC;
  if (!CC || CC.page !== "dashboard") return;
  const $ = (sel, root) => (root || document).querySelector(sel);

  const POLL_MS = 5000;          // matches the sampler interval
  const OPTIMISTIC_MS = 20000;   // how long a "Stopping…" we set ourselves may outlive the Hub's answer
  const STATES = {
    running: { label: "Running", order: 0, icon: "fa-circle" },
    starting: { label: "Starting…", order: 1, icon: "fa-circle cc-pulse" },
    stopping: { label: "Stopping…", order: 2, icon: "fa-spinner fa-spin" },
    stopped: { label: "Stopped", order: 3, icon: "fa-circle" },
    failed: { label: "Failed", order: 4, icon: "fa-circle" },
    unknown: { label: "Unknown", order: 5, icon: "fa-circle-question" },
  };

  const els = {
    live: $("#cc-live"), table: $("#dashboard-table"), tbody: $("#dash-rows"), loading: $("#dash-loading"),
    card: $("#dash-card"), empty: $("#dash-empty"), count: $("#dash-count"), search: $("#dash-search"),
    offline: $("#dash-offline"), offlineSince: $("#dash-offline-since"), offlineDetail: $("#dash-offline-detail"),
    hubAlert: $("#dash-hub"), hubDetail: $("#dash-hub-detail"),
    stopAll: $("#stop-all"), stopAllWrap: $("#stop-all-wrap"), subtitle: $(".cc-page-subtitle"), versions: $("#dash-versions"),
  };
  if (!els.table || !els.tbody) return;

  const tableCtl = CC.table(els.table);
  const rows = new Map();        // username -> <tr>
  const byUser = new Map();      // username -> last server record from the API
  const optimistic = new Map();  // username -> { state, from, until }
  const baseSubtitle = els.subtitle ? els.subtitle.textContent.trim() : "";
  let latest = null, lastOk = 0, failures = 0, offlineDismissed = false, needSort = false, initialised = false;

  // --- small helpers ---------------------------------------------------------
  const setValue = (td, v) => { const s = String(v); if (td && td.dataset.value !== s) td.dataset.value = s; };
  const firstName = (s) => ((s.display_name || s.username).trim().split(/\s+/)[0]) || s.username;
  const isDisabled = (btn) => btn.getAttribute("aria-disabled") === "true";
  const hubOk = () => !!(latest && latest.hub && latest.hub.ok);
  function setDisabled(btn, off) {
    btn.classList.toggle("disabled", off);          // .disabled keeps the button focusable, unlike the attribute
    btn.setAttribute("aria-disabled", String(off));
    btn.tabIndex = off ? -1 : 0;
    if (btn.disabled) btn.disabled = false;         // the real attribute would swallow clicks for good
  }
  function fmtUptime(s) {
    s = Math.max(0, Math.floor(Number(s) || 0));
    const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
    if (d) return `${d} d ${h} h`;
    if (h) return `${h} h ${m} min`;
    return `${m} min`;
  }
  function agoShort(ms) {
    const s = Math.round(ms / 1000);
    if (s < 10) return "just now";
    if (s < 60) return `${Math.floor(s / 10) * 10} s ago`;
    return `${Math.floor(s / 60)} min ago`;
  }

  function effectiveState(s) {
    const o = optimistic.get(s.username);
    if (o) {
      if (Date.now() > o.until || s.state !== o.from) optimistic.delete(s.username);   // the Hub caught up (or gave up)
      else return o.state;
    }
    if (s.error) return "failed";
    return STATES[s.state] ? s.state : "unknown";
  }
  function setOptimistic(username, state, from) { optimistic.set(username, { state, from, until: Date.now() + OPTIMISTIC_MS }); }
  function countRunning() { let n = 0; byUser.forEach((s) => { if (!s.is_admin && effectiveState(s) === "running") n += 1; }); return n; }

  // --- rows ------------------------------------------------------------------
  function makeRow(s) {
    const tr = document.createElement("tr");
    tr.dataset.user = s.username;
    tr.innerHTML =
      '<td data-key="name"><a class="cc-student-link" href="#"><strong class="js-name"></strong></a>' +
      '<span class="cc-teacher-tag js-tag" hidden>Teacher</span><span class="cc-student-user cc-muted cc-mono js-user"></span></td>' +
      '<td data-key="state"><span class="cc-pill js-pill"><i class="fa fa-circle" aria-hidden="true"></i><span class="js-pill-text"></span></span></td>' +
      '<td data-key="activity"><time class="js-time"></time></td>' +
      '<td class="num" data-key="cpu"><span class="js-cpu"></span></td>' +
      '<td class="num" data-key="mem"><span class="js-mem"></span></td>' +
      '<td class="text-end"><button type="button" class="btn btn-sm btn-outline-secondary cc-row-action js-action" aria-disabled="false">' +
      '<i class="fa fa-play" aria-hidden="true"></i> <span class="cc-row-action__text"></span></button></td>';
    tr.querySelector(".cc-student-link").href = `${CC.prefix}/students?q=${encodeURIComponent(s.username)}`;
    tr.querySelector(".js-action").addEventListener("click", () => onAction(s.username));
    return tr;
  }

  function patchRow(tr, s) {
    const name = s.display_name || s.username;
    CC.setText(tr.querySelector(".js-name"), name);
    CC.setText(tr.querySelector(".js-user"), s.username);
    tr.querySelector(".js-tag").hidden = !s.is_admin;
    const search = `${name} ${s.username}`.toLowerCase();
    if (tr.dataset.search !== search) tr.dataset.search = search;
    setValue(tr.querySelector('[data-key="name"]'), name.toLowerCase());

    const state = effectiveState(s), def = STATES[state];
    const pill = tr.querySelector(".js-pill");
    const cls = `cc-pill js-pill cc-pill--${state}`;
    if (pill.className !== cls) { pill.className = cls; pill.querySelector(".fa").className = `fa ${def.icon}`; }
    CC.setText(pill.querySelector(".js-pill-text"), def.label);
    const title = state === "failed" && s.error ? String(s.error) : "";
    if (pill.title !== title) pill.title = title;
    setValue(tr.querySelector('[data-key="state"]'), def.order);

    const t = tr.querySelector(".js-time");
    const iso = s.last_activity || "";
    if ((t.getAttribute("datetime") || "") !== iso) { if (iso) t.setAttribute("datetime", iso); else t.removeAttribute("datetime"); }
    CC.setText(t, iso ? CC.ago(iso) : "Never");
    const tTitle = iso ? new Date(iso).toLocaleString() : "No activity recorded yet";
    if (t.title !== tTitle) t.title = tTitle;
    setValue(tr.querySelector('[data-key="activity"]'), iso ? (Date.parse(iso) || 0) : 0);

    const active = (s.nproc || 0) > 0;
    CC.setText(tr.querySelector(".js-cpu"), active ? CC.pct(s.cpu_pct) : "—");
    setValue(tr.querySelector('[data-key="cpu"]'), active ? Number(s.cpu_pct) || 0 : -1);
    CC.setText(tr.querySelector(".js-mem"), active ? CC.bytes(s.mem_bytes) : "—");
    setValue(tr.querySelector('[data-key="mem"]'), active ? Number(s.mem_bytes) || 0 : -1);

    patchAction(tr.querySelector(".js-action"), s, state);
  }

  function patchAction(btn, s, state) {
    let label, icon, off = false, busy = false;
    if (state === "running") { label = "Stop"; icon = "fa-stop"; }
    else if (state === "starting") { label = "Starting…"; icon = "fa-spinner fa-spin"; off = true; busy = true; }
    else if (state === "stopping") { label = "Stopping…"; icon = "fa-spinner fa-spin"; off = true; busy = true; }
    else { label = "Start"; icon = "fa-play"; }
    if (!hubOk() || state === "unknown") off = true;
    const iconEl = btn.querySelector(".fa");
    if (iconEl.className !== `fa ${icon}`) iconEl.className = `fa ${icon}`;
    CC.setText(btn.querySelector(".cc-row-action__text"), label);
    const aria = `${label.replace("…", "")} ${s.display_name || s.username}'s server`;
    if (btn.getAttribute("aria-label") !== aria) btn.setAttribute("aria-label", aria);
    if (isDisabled(btn) !== off) setDisabled(btn, off);
    btn.setAttribute("aria-busy", String(busy));
    const want = state === "running" ? "btn btn-sm btn-outline-danger cc-row-action js-action" : "btn btn-sm btn-outline-secondary cc-row-action js-action";
    if (!btn.className.startsWith(want)) btn.className = want + (off ? " disabled" : "");
  }

  function matchesSearch(tr) {
    const q = (els.search && els.search.value || "").trim().toLowerCase();
    return !q || tr.dataset.search.includes(q);
  }

  // Re-sorting moves rows, which would steal keyboard focus: wait until focus leaves the table.
  function resort() {
    const active = document.activeElement;
    if (active && active !== document.body && els.tbody.contains(active)) { needSort = true; return; }
    tableCtl.apply();
    needSort = false;
  }
  els.tbody.addEventListener("focusout", () => setTimeout(() => { if (needSort) resort(); }, 0));

  function repaint(username) {
    const s = byUser.get(username), tr = rows.get(username);
    if (s && tr) patchRow(tr, s);
    updateStopAll();
  }
  function repaintAll() { byUser.forEach((s, u) => { const tr = rows.get(u); if (tr) patchRow(tr, s); }); updateStopAll(); }

  // --- tiles -------------------------------------------------------------------
  function setStat(sel, text) {
    const el = $(sel);
    if (!el || el.textContent === text) return;
    el.classList.add("is-changing");
    el.textContent = text;
    setTimeout(() => el.classList.remove("is-changing"), 160);
  }
  function meter(sel, pct, warn, danger) {
    const m = $(sel);
    if (!m) return;
    CC.setMeter(m.firstElementChild, pct, warn, danger);
    m.setAttribute("aria-valuenow", String(Math.round(pct)));
  }
  function renderTiles(d) {
    const h = d.host || {}, st = d.students || {};
    const total = st.total == null ? null : Number(st.total);
    setStat("#stat-online", total == null ? "—" : String(st.online));
    CC.setText($("#stat-online-sub"), total == null ? "of — accounts" : `of ${total} ${total === 1 ? "account" : "accounts"}`);

    const cpu = h.cpu_pct == null ? null : Number(h.cpu_pct);
    setStat("#stat-cpu", cpu == null ? "—" : `${Math.round(cpu)}\u00a0%`);
    CC.setText($("#stat-cpu-sub"), h.ncpu ? `${h.ncpu} ${h.ncpu === 1 ? "core" : "cores"}${h.load1 != null ? ` · load ${Number(h.load1).toFixed(1)}` : ""}` : "whole machine");
    meter("#meter-cpu", cpu == null ? 0 : cpu, 75, 90);

    const memPct = h.mem_total ? (h.mem_used / h.mem_total) * 100 : null;
    setStat("#stat-mem", h.mem_used == null ? "—" : CC.bytes(h.mem_used));
    CC.setText($("#stat-mem-sub"), h.mem_total ? `of ${CC.bytes(h.mem_total)}` : "of —");
    meter("#meter-mem", memPct == null ? 0 : memPct, 75, 90);

    const diskPct = h.disk_total ? ((h.disk_total - h.disk_free) / h.disk_total) * 100 : null;
    setStat("#stat-disk", h.disk_free == null ? "—" : CC.bytes(h.disk_free));
    CC.setText($("#stat-disk-sub"), h.disk_total ? `free of ${CC.bytes(h.disk_total)}` : "free of —");
    meter("#meter-disk", diskPct == null ? 0 : diskPct, 85, 95);   // warn below 15 % free, danger below 5 %

    if (els.subtitle) CC.setText(els.subtitle, h.uptime_s != null ? `${baseSubtitle} · up ${fmtUptime(h.uptime_s)}` : baseSubtitle);
    if (els.versions) {
      const bits = [];
      if (d.hub && d.hub.jupyterhub) bits.push(`JupyterHub ${d.hub.jupyterhub}`);
      if (d.hub && d.hub.image_version) bits.push(`image ${d.hub.image_version}`);
      CC.setText(els.versions, bits.length ? `(${bits.join(" · ")})` : "");
    }
  }

  // --- stop all ---------------------------------------------------------------
  function updateStopAll() {
    if (!els.stopAll) return;
    const n = countRunning();
    const ok = hubOk() && n > 0;
    if (isDisabled(els.stopAll) !== !ok) setDisabled(els.stopAll, !ok);
    els.stopAll.dataset.count = String(n);
    if (els.stopAllWrap) {
      const title = !hubOk() ? "JupyterHub is not answering" : n === 0 ? "No servers are running" : "";
      els.stopAllWrap.tabIndex = ok ? -1 : 0;     // a keyboard stop only while it explains why the button is off
      if (window.bootstrap && bootstrap.Tooltip) {
        const tip = bootstrap.Tooltip.getOrCreateInstance(els.stopAllWrap);
        if (title) { tip.setContent({ ".tooltip-inner": title }); tip.enable(); }
        else { tip.hide(); tip.disable(); }
      }
    }
  }

  // --- confirm with focus restore (Bootstrap does not return focus itself) ---
  function confirmKeepingFocus(opts, trigger, fallback) {
    const modalEl = document.getElementById("cc-confirm");
    const restore = () => {
      const usable = trigger && document.contains(trigger) && !trigger.matches(".disabled,[aria-disabled=true],:disabled");
      const target = usable ? trigger : (fallback && fallback());
      if (target && target.focus) target.focus({ preventScroll: true });
    };
    if (modalEl) modalEl.addEventListener("hidden.bs.modal", restore, { once: true });
    return CC.confirm(opts);
  }

  // --- actions -----------------------------------------------------------------
  async function onAction(username) {
    const s = byUser.get(username), tr = rows.get(username);
    if (!s || !tr) return;
    const btn = tr.querySelector(".js-action");
    if (!btn || isDisabled(btn)) return;
    const state = effectiveState(s), first = firstName(s);
    if (state === "running") {
      const ok = await confirmKeepingFocus({
        title: `Stop ${first}'s JupyterLab?`,
        body: `Anything not saved in ${first}'s open notebooks may be lost. Files already saved are safe, and ${first} can sign in again at any time.`,
        confirmLabel: "Stop server", danger: true,
      }, btn, () => tr.querySelector(".cc-student-link"));
      if (!ok) return;
      await act(s, "stop", "stopping", "running", { stopping: `Stopping ${first}'s JupyterLab…`, stopped: `${first}'s JupyterLab has stopped.` });
    } else if (state === "stopped" || state === "failed") {
      await act(s, "start", "starting", s.state, { starting: `Starting ${first}'s JupyterLab…`, running: `${first}'s JupyterLab is running.` });
    }
  }

  async function act(s, verb, optimisticState, from, messages) {
    setOptimistic(s.username, optimisticState, from);
    repaint(s.username);
    try {
      const r = await CC.api("POST", `servers/${encodeURIComponent(s.username)}/${verb}`);
      setOptimistic(s.username, r.state, from);
      repaint(s.username);
      if (messages[r.state]) CC.toast(messages[r.state], { kind: "success" });
      refreshSoon();
    } catch (e) {
      optimistic.delete(s.username);
      repaint(s.username);
      CC.error(e);
      refreshSoon();
    }
  }

  if (els.stopAll) {
    els.stopAll.addEventListener("click", async () => {
      if (isDisabled(els.stopAll)) return;
      const n = countRunning();
      if (!n) return;
      const ok = await confirmKeepingFocus({
        title: n === 1 ? "Stop the 1 running server?" : `Stop all ${n} running servers?`,
        body: "Every student currently working will lose anything they haven't saved. Saved files are safe. Use this at the end of class or before a backup or restart.",
        confirmLabel: n === 1 ? "Stop 1 server" : `Stop all ${n} servers`, danger: true,
      }, els.stopAll, () => document.getElementById("content"));
      if (!ok) return;
      const targets = [];
      byUser.forEach((s) => { if (!s.is_admin && effectiveState(s) === "running") { targets.push(s.username); setOptimistic(s.username, "stopping", "running"); } });
      repaintAll();
      CC.toast(`Stopping ${n} ${n === 1 ? "server" : "servers"}…`);
      try {
        const r = await CC.api("POST", "servers/stop-all", { include_admins: false });
        (r.stopped || []).forEach((u) => setOptimistic(u, "stopped", "running"));
        (r.failed || []).forEach((f) => optimistic.delete(f.username));
        repaintAll();
        if (r.failed && r.failed.length) {
          const names = r.failed.map((f) => f.username).join(", ");
          CC.toast(`${r.stopped.length} stopped. Could not stop ${names}: ${r.failed[0].error}`, { kind: "error" });
        } else {
          CC.toast(r.stopped && r.stopped.length ? "All servers stopped." : "No servers needed stopping.", { kind: "success" });
        }
      } catch (e) {
        targets.forEach((u) => optimistic.delete(u));
        repaintAll();
        CC.error(e);
      }
      refreshSoon();
    });
  }

  // --- render one status payload ---------------------------------------------------
  function render(data) {
    latest = data;
    byUser.clear();
    renderTiles(data);
    const ok = hubOk();
    if (els.hubAlert) { els.hubAlert.hidden = ok; CC.setText(els.hubDetail, ok ? "" : (data.hub && data.hub.error) || ""); }
    els.card.classList.toggle("is-paused", !ok);

    const seen = new Set();
    (data.servers || []).forEach((s) => {
      if (!s || !s.username) return;
      byUser.set(s.username, s);
      seen.add(s.username);
      let tr = rows.get(s.username);
      const fresh = !tr;
      if (fresh) { tr = makeRow(s); rows.set(s.username, tr); els.tbody.appendChild(tr); }
      patchRow(tr, s);
      if (fresh) {
        tr.hidden = !matchesSearch(tr);
        if (initialised) { tr.classList.add("is-new"); setTimeout(() => tr.classList.remove("is-new"), 1300); }
      }
    });
    rows.forEach((tr, u) => { if (!seen.has(u)) { tr.remove(); rows.delete(u); optimistic.delete(u); } });
    if (els.loading) { els.loading.remove(); els.loading = null; }
    initialised = true;

    let students = 0;
    byUser.forEach((s) => { if (!s.is_admin) students += 1; });
    const empty = students === 0;                 // only teacher accounts: nothing to watch yet
    els.card.classList.toggle("is-empty", empty);
    if (els.empty) els.empty.hidden = !empty;
    CC.setText(els.count, empty ? "" : String(students));
    resort();
    updateStopAll();
    CC.updateHealth(data.host);
    CC.setBadge("dashboard", data.students ? data.students.online : null);
  }

  // --- live indicator and polling ----------------------------------------------------
  const compactQuery = window.matchMedia ? window.matchMedia("(max-width: 575px)") : null;
  function tickLive() {
    if (!els.live) return;
    const compact = !!(compactQuery && compactQuery.matches);      // phones: leave room for the brand
    if (!lastOk) { CC.setText(els.live, compact ? "Connecting…" : (failures ? "Connecting…" : "Live · connecting…")); return; }
    const ago = agoShort(Date.now() - lastOk);
    const offline = failures >= 3;
    els.live.classList.toggle("is-offline", offline);
    els.live.title = `${offline ? "Last successful update" : "Updated"} ${ago}`;
    if (compact) CC.setText(els.live, offline ? "Offline" : "Live");
    else CC.setText(els.live, offline ? `Offline · last update ${ago}` : `Live · updated ${ago}`);
    if (els.offline && !els.offline.hidden && els.offlineSince) CC.setText(els.offlineSince, CC.ago(els.offlineSince.getAttribute("datetime")));
  }
  // The visible indicator changes every few seconds; reading each change aloud would be unbearable,
  // so screen readers hear only the transitions (base.html marks #cc-live as a polite live region).
  let announcer = null;
  function announce(text) {
    if (!announcer) {
      announcer = document.createElement("div");
      announcer.className = "visually-hidden";
      announcer.setAttribute("role", "status");
      announcer.setAttribute("aria-live", "polite");
      document.body.appendChild(announcer);
    }
    announcer.textContent = "";
    setTimeout(() => { announcer.textContent = text; }, 50);
  }
  if (els.live) els.live.setAttribute("aria-live", "off");

  function onFailure(e) {
    if (!initialised && els.loading) {           // nothing loaded yet: say so instead of spinning for half a minute
      els.loading.firstElementChild.innerHTML = '<i class="fa fa-triangle-exclamation" aria-hidden="true"></i> ';
      els.loading.firstElementChild.appendChild(document.createTextNode(
        e && e.code && e.code !== "network" ? `Couldn't load the class (${e.message}). Trying again…` : "Couldn't load the class. Trying again…"));
    }
    if (failures === 3) announce("Lost contact with the console. Keeping the last numbers and trying again.");
    if (failures >= 3 && !offlineDismissed && els.offline) {
      els.offline.hidden = false;
      if (els.offlineSince) { const iso = lastOk ? new Date(lastOk).toISOString() : ""; if (iso) els.offlineSince.setAttribute("datetime", iso); CC.setText(els.offlineSince, lastOk ? CC.ago(iso) : "before this page loaded"); }
      if (els.offlineDetail) CC.setText(els.offlineDetail, e && e.code && e.code !== "network" ? e.message : "");
    }
    tickLive();
  }
  if (els.live) { els.live.hidden = false; tickLive(); }
  setInterval(tickLive, 1000);
  setInterval(() => CC.refreshTimes(els.tbody), 30000);
  if (els.offline) els.offline.querySelector("[data-dismiss-offline]").addEventListener("click", () => { els.offline.hidden = true; offlineDismissed = true; });

  async function pollOnce(signal) {
    let data;
    try {
      data = await CC.api("GET", "status", null, { signal });
    } catch (e) {
      if (e.name === "AbortError") throw e;
      failures += 1;
      onFailure(e);
      throw e;                                   // lets CC.poll back off
    }
    if (failures >= 3) announce("Connection restored. The dashboard is live again.");
    failures = 0;
    lastOk = Date.now();
    offlineDismissed = false;
    if (els.offline) els.offline.hidden = true;
    render(data);
    tickLive();
  }

  // After a Start / Stop we want fresh numbers straight away, but CC.poll's now() aborts a request
  // that is still in flight, and that aborted run then schedules its own timer next to the new one,
  // so the page would poll twice as often for the rest of the day. A refresh asked for while a poll
  // is in flight therefore waits until that poll has settled (the optimistic row state covers the
  // few hundred milliseconds in between); the deferred call re-checks in case another poll started.
  let inFlight = 0, refreshWanted = false;
  function refreshSoon() {
    if (inFlight > 0) { refreshWanted = true; return; }
    poller.now();
  }
  const poller = CC.poll(async (signal) => {
    inFlight += 1;
    try {
      await pollOnce(signal);
    } finally {
      inFlight -= 1;
      if (refreshWanted && inFlight === 0) { refreshWanted = false; setTimeout(refreshSoon, 0); }
    }
  }, POLL_MS);
})();
