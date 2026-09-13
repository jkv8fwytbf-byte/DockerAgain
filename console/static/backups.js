/* Backups page: list backups and archived students, create a backup and
   follow its progress (state lives on the server, so a reload resumes it). */
(function () {
  "use strict";
  if (!window.CC || CC.page !== "backups") return;

  const $ = (id) => document.getElementById(id);
  const state = { poller: null, jobId: null, tables: {}, loaded: false };

  // --- tiny DOM helper ------------------------------------------------------
  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    Object.entries(attrs || {}).forEach(([k, v]) => {
      if (v === null || v === undefined || v === false) return;
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = v;
      else if (k === "dataset") Object.assign(node.dataset, v);
      else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v === true ? "" : v);
    });
    (children || []).forEach((c) => { if (c !== null && c !== undefined) node.appendChild(typeof c === "string" ? document.createTextNode(c) : c); });
    return node;
  }
  const icon = (name) => el("i", { class: `fa ${name}`, "aria-hidden": "true" });
  const when = (iso) => {
    const d = new Date(iso);
    if (isNaN(d.getTime())) return el("span", { text: "Unknown" });
    return el("time", { datetime: iso, title: CC.ago(iso), text: d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) });
  };
  const downloadUrl = (kind, name) => `${CC.prefix}/api/${kind}/${encodeURIComponent(name)}/download`;

  // --- loading ----------------------------------------------------------------
  async function load() {
    try {
      const data = await CC.api("GET", "backups");
      state.loaded = true;
      $("bk-error").hidden = true;
      $("bk-section").hidden = false; $("ar-section").hidden = false;
      renderDisk(data.disk);
      renderBackups(data.backups || []);
      renderArchived(data.archived || []);
      renderJob(data.job);
      if (data.job && data.job.state === "running") watch(data.job.id);
      else setBusy(false);
    } catch (e) {
      if (!state.loaded) {
        // Nothing useful to show yet: the error card (at the top) replaces the two empty sections.
        $("bk-loading").hidden = true; $("ar-loading").hidden = true;
        $("bk-section").hidden = true; $("ar-section").hidden = true; $("bk-disk").hidden = true;
        $("bk-error").hidden = false; CC.setText($("bk-error-text"), e.message || "Something went wrong.");
        $("bk-retry").focus();
      } else {
        CC.error(e);
      }
    }
  }

  function setBusy(busy) {
    ["bk-create", "bk-create-empty"].forEach((id) => {
      const b = $(id); if (!b) return;
      b.disabled = busy; b.setAttribute("aria-busy", String(busy));
    });
  }

  // --- disk note --------------------------------------------------------------
  function renderDisk(disk) {
    const box = $("bk-disk"); if (!box || !disk) return;
    const text = $("bk-disk-text");
    const free = CC.bytes(disk.free || 0);
    const est = disk.estimate_bytes === null || disk.estimate_bytes === undefined ? null : CC.bytes(disk.estimate_bytes);
    $("bk-disk-tip").hidden = !!disk.warning;   // the warning already says what to do
    if (disk.warning) {
      box.className = "alert alert-warning cc-disk-note";
      box.querySelector(".cc-disk-note__icon").className = "fa fa-triangle-exclamation cc-disk-note__icon";
      CC.setText(text, disk.warning);
    } else {
      box.className = "alert alert-info cc-disk-note";
      box.querySelector(".cc-disk-note__icon").className = "fa fa-hard-drive cc-disk-note__icon";
      const about = est ? `A backup is about the size of all student files (currently ~${est}).` : "A backup is about the size of all student files.";
      const stored = disk.stored_bytes ? ` Backups and archives on this server use ${CC.bytes(disk.stored_bytes)}.` : "";
      CC.setText(text, `${free} free on this server. ${about}${stored} `);
    }
    box.hidden = false;
  }

  // --- progress card ----------------------------------------------------------
  function renderJob(job) {
    const card = $("bk-progress"), failed = $("bk-failed");
    if (!job) { card.hidden = true; failed.hidden = true; CC.setText($("bk-announce"), ""); return; }
    if (job.state === "running") {
      failed.hidden = true;
      // Announce once for screen readers; the card's own text changes every 2 s and would be too chatty.
      if (card.hidden) CC.setText($("bk-announce"), "Creating a backup. This can take a few minutes; you will be told when it is done.");
      const written = CC.bytes(job.bytes_written || 0);
      let phase = "Starting";
      if (job.phase === "home folders" && job.homes_total) phase = `Home folder ${Math.min(job.homes_done + 1, job.homes_total)} of ${job.homes_total}`;
      else if (job.phase) phase = job.phase.charAt(0).toUpperCase() + job.phase.slice(1);
      CC.setText($("bk-progress-text"), `${phase} · ${written} written`);
      const bar = $("bk-progress-bar"), inner = bar.firstElementChild;
      if (job.homes_total) {
        // Home folders are nearly all of the work; keep a little headroom for shared + settings.
        const pct = Math.max(3, Math.min(97, Math.round((job.homes_done / job.homes_total) * 92) + (job.phase === "home folders" ? 0 : 5)));
        inner.style.width = `${pct}%`; bar.setAttribute("aria-valuenow", String(pct)); bar.setAttribute("aria-valuetext", `${pct} percent`);
      } else {
        inner.style.width = "100%"; bar.removeAttribute("aria-valuenow"); bar.setAttribute("aria-valuetext", "working");
      }
      card.hidden = false;
      setBusy(true);
      return;
    }
    card.hidden = true;
    CC.setText($("bk-announce"), "");
    if (job.state === "failed") { CC.setText($("bk-failed-text"), job.error ? `${job.error}.` : ""); failed.hidden = false; }
    else failed.hidden = true;
    renderNote(job);
  }

  // What the last finished backup could not include (too big, sparse, nested too deep, a pipe...) and
  // anything the teacher must know (e.g. the roster was missing). The manifest inside the backup has the same list.
  function renderNote(job) {
    const box = $("bk-note");
    const warnings = (job && job.warnings) || [], items = (job && job.skipped_items) || [], skipped = (job && job.skipped) || 0;
    if (!job || job.state !== "done" || (!warnings.length && !skipped)) { box.hidden = true; return; }
    CC.setText($("bk-note-title"), warnings.length ? `Please read this about ${job.name}.` : `${job.name} left ${skipped === 1 ? "one item" : `${skipped} items`} out.`);
    CC.setText($("bk-note-text"), skipped
      ? " Files bigger than the limit, mostly-empty (sparse) files, folders nested very deep and special files are not copied; everything else is in the backup."
      : "");
    const wl = $("bk-note-warnings");
    wl.replaceChildren(...warnings.map((w) => el("li", { text: w })));
    wl.hidden = warnings.length === 0;
    const details = $("bk-note-details");
    $("bk-note-items").replaceChildren(...items.map((i) => el("li", {}, [el("span", { text: i.path }), el("span", { class: "cc-muted", text: ` — ${i.reason}` })])));
    const more = $("bk-note-more");
    more.hidden = skipped <= items.length;
    if (skipped > items.length) CC.setText(more, `…and ${skipped - items.length} more. manifest.json inside the backup lists these first ${items.length}; the Logs page shows up to 50.`);
    details.hidden = items.length === 0;
    box.hidden = false;
  }

  function watch(jobId) {
    if (state.poller) state.poller.stop();
    state.jobId = jobId;
    setBusy(true);
    state.poller = CC.poll(async (signal) => {
      let job;
      try {
        job = await CC.api("GET", `backups/jobs/${encodeURIComponent(jobId)}`, null, { signal });
      } catch (e) {
        if (e.status === 404) { stopWatching(); await load(); return; }   // console restarted mid-job
        throw e;
      }
      renderJob(job);
      if (job.state !== "running") { stopWatching(); finished(job); }
    }, 2000);
  }

  function stopWatching() { if (state.poller) state.poller.stop(); state.poller = null; state.jobId = null; }

  async function finished(job) {
    if (job.state === "done") {
      const left = job.skipped ? ` · ${job.skipped === 1 ? "1 item was" : `${job.skipped} items were`} left out, see the note above the list` : "";
      const warn = job.warnings && job.warnings.length ? " · please read the note above the list" : "";
      CC.toast(`Backup created: ${job.name} (${CC.bytes(job.bytes_written)})${left || warn}`, {
        kind: job.warnings && job.warnings.length ? "info" : "success", sticky: !!(job.warnings && job.warnings.length),
        action: { label: "Download", href: downloadUrl("backups", job.name) },
      });
    } else {
      CC.toast(`The backup failed: ${job.error || "unknown error"}`, { kind: "error", sticky: true });
    }
    await load();
  }

  // --- create -----------------------------------------------------------------
  async function createBackup() {
    setBusy(true);
    try {
      const r = await CC.api("POST", "backups");
      $("bk-failed").hidden = true;
      renderJob(r.job);
      if (r.job.state === "running") { focusProgress(); watch(r.job.id); } else finished(r.job);
    } catch (e) {
      if (e.status === 409 && e.detail && e.detail.job) {
        CC.toast("A backup is already being created. Its progress is shown below.", { kind: "info" });
        renderJob(e.detail.job); focusProgress(); watch(e.detail.job.id);
        return;
      }
      CC.toast(e.message || "The backup could not be started.", { kind: "error", sticky: true });
      setBusy(false);
      load();
    }
  }

  // The button that started the job is now disabled and cannot keep focus: park it on the progress card.
  function focusProgress() {
    const card = $("bk-progress");
    if (!card.hidden) card.focus({ preventScroll: true });
  }

  // --- backups table ----------------------------------------------------------
  function renderBackups(backups) {
    const tbody = $("bk-table").tBodies[0];
    tbody.replaceChildren(...backups.map((b) => el("tr", { dataset: { search: b.name, name: b.name } }, [
      el("td", { dataset: { key: "name", value: b.name } }, [icon("fa-file-zipper cc-muted me-2"), el("code", { class: "cc-mono", text: b.name })]),
      el("td", { dataset: { key: "created", value: String(Date.parse(b.created) || 0) } }, [when(b.created)]),
      el("td", { class: "num", dataset: { key: "size", value: String(b.size || 0) }, text: CC.bytes(b.size) }),
      el("td", { class: "cc-actions" }, [
        el("a", { class: "btn btn-sm btn-outline-secondary", href: downloadUrl("backups", b.name), "aria-label": `Download ${b.name}` }, [icon("fa-download"), " Download"]),
        " ",
        el("button", { type: "button", class: "btn btn-sm btn-outline-danger", "aria-label": `Delete backup ${b.name}`, onclick: () => deleteBackup(b) }, [icon("fa-trash-can"), " Delete"]),
      ]),
    ])));
    $("bk-loading").hidden = true;
    $("bk-table-wrap").hidden = backups.length === 0;
    $("bk-empty").hidden = backups.length !== 0;
    CC.setText($("bk-count"), backups.length ? `(${backups.length})` : "");
    if (!state.tables.bk) state.tables.bk = CC.table($("bk-table")); else state.tables.bk.apply();
  }

  async function deleteBackup(b) {
    const ok = await CC.confirm({
      title: `Delete backup ${b.name}?`,
      body: `This cannot be undone. Other backups and the students' current files are not affected.\nIf you have not downloaded this backup, its ${CC.bytes(b.size)} of files are gone for good.`,
      confirmLabel: "Delete backup", danger: true,
    });
    if (!ok) return;
    try {
      await CC.api("DELETE", `backups/${encodeURIComponent(b.name)}`);
      CC.toast(`Backup ${b.name} deleted.`, { kind: "success" });
    } catch (e) {
      CC.error(e);
    }
    await load();
    $("bk-title").focus();
  }

  // --- archived students -------------------------------------------------------
  function renderArchived(archived) {
    const tbody = $("ar-table").tBodies[0];
    tbody.replaceChildren(...archived.map((a) => el("tr", { dataset: { search: `${a.username} ${a.name}`, name: a.name } }, [
      el("td", { dataset: { key: "username", value: a.username } }, [icon("fa-user-clock cc-muted me-2"), el("strong", { text: a.username }), " ", el("code", { class: "cc-mono cc-muted small", text: a.name })]),
      el("td", { dataset: { key: "removed", value: String(Date.parse(a.removed) || 0) } }, [when(a.removed)]),
      el("td", { class: "num", dataset: { key: "size", value: String(a.size || 0) }, text: CC.bytes(a.size) }),
      el("td", { class: "cc-actions" }, [
        el("a", { class: "btn btn-sm btn-outline-secondary", href: downloadUrl("archive", a.name), "aria-label": `Download ${a.username}'s archive` }, [icon("fa-download"), " Download"]),
        " ",
        el("button", { type: "button", class: "btn btn-sm btn-outline-danger", "aria-label": `Delete ${a.username}'s archive`, onclick: () => deleteArchive(a) }, [icon("fa-trash-can"), " Delete"]),
      ]),
    ])));
    $("ar-loading").hidden = true;
    $("ar-table-wrap").hidden = archived.length === 0;
    $("ar-toolbar").hidden = archived.length < 2;
    $("ar-empty").hidden = archived.length !== 0;
    CC.setText($("ar-count"), archived.length ? `(${archived.length})` : "");
    if (!state.tables.ar) state.tables.ar = CC.table($("ar-table")); else state.tables.ar.apply();
  }

  async function deleteArchive(a) {
    const ok = await CC.confirm({
      title: `Delete ${a.username}'s archived files?`,
      body: `This permanently deletes everything ${a.username} had in their home folder when they were removed (${CC.bytes(a.size)}). This cannot be undone.\nIf you might need their work later, download the archive first.`,
      confirmLabel: "Delete archive", danger: true,
    });
    if (!ok) return;
    try {
      await CC.api("DELETE", `archive/${encodeURIComponent(a.name)}`);
      CC.toast(`${a.username}'s archive deleted.`, { kind: "success" });
    } catch (e) {
      CC.error(e);
    }
    await load();
    $("ar-title").focus();
  }

  // --- init ---------------------------------------------------------------------
  document.addEventListener("DOMContentLoaded", () => {
    $("bk-create").addEventListener("click", createBackup);
    $("bk-create-empty").addEventListener("click", createBackup);
    $("bk-retry").addEventListener("click", () => {
      $("bk-error").hidden = true;
      $("bk-section").hidden = false; $("ar-section").hidden = false;
      $("bk-loading").hidden = false; $("ar-loading").hidden = false;
      load();
    });
    load();
    // Backups appear in the list even when made from another tab: refresh quietly every minute,
    // but never while the teacher is in a table (the rows would be rebuilt under their focus) or in a dialog.
    CC.poll(async () => {
      if (state.poller || !state.loaded) return;
      const active = document.activeElement;
      if (active && (active.closest("#bk-table, #ar-table") || active.closest(".modal"))) return;
      if (document.querySelector(".modal.show")) return;
      await load();
    }, 60000);
  });
})();
