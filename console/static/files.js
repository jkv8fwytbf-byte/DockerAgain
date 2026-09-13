/* Handouts & Submissions page. Uses the shared helpers in console.js (window.CC). */
(function () {
  "use strict";
  const CC = window.CC;
  const $ = (sel, root = document) => root.querySelector(sel);
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  };
  const icon = (name) => { const i = el("i", `fa ${name}`); i.setAttribute("aria-hidden", "true"); return i; };
  const query = (path) => (path ? `?path=${encodeURIComponent(path)}` : "");
  const join = (dir, name) => (dir ? `${dir}/${name}` : name);

  const ICONS = {
    ipynb: "fa-book", py: "fa-file-code", js: "fa-file-code", json: "fa-file-code", html: "fa-file-code", r: "fa-file-code",
    csv: "fa-table", tsv: "fa-table", xlsx: "fa-file-excel", xls: "fa-file-excel", pdf: "fa-file-pdf",
    zip: "fa-file-zipper", gz: "fa-file-zipper", tgz: "fa-file-zipper", tar: "fa-file-zipper", "7z": "fa-file-zipper",
    md: "fa-file-lines", txt: "fa-file-lines", rst: "fa-file-lines", doc: "fa-file-word", docx: "fa-file-word",
    ppt: "fa-file-powerpoint", pptx: "fa-file-powerpoint", png: "fa-file-image", jpg: "fa-file-image", jpeg: "fa-file-image",
    gif: "fa-file-image", svg: "fa-file-image", webp: "fa-file-image", mp4: "fa-file-video", mov: "fa-file-video",
    mp3: "fa-file-audio", wav: "fa-file-audio", npy: "fa-database", npz: "fa-database", h5: "fa-database", parquet: "fa-database",
  };
  const ext = (name) => { const i = name.lastIndexOf("."); return i > 0 ? name.slice(i + 1).toLowerCase() : ""; };
  const iconFor = (e) => (e.type === "dir" ? "fa-folder" : e.type === "symlink" ? "fa-link" : e.type === "other" ? "fa-file-circle-question" : ICONS[ext(e.name)] || "fa-file");

  const state = { path: "", maxBytes: 0, entries: [], loaded: false };
  const handoutsTable = CC.table($("#handouts-table"));
  const submissionsTable = CC.table($("#submissions-table"));

  // ===== Handouts: listing ==================================================
  async function loadHandouts(path = state.path, { signal } = {}) {
    let data;
    try {
      data = await CC.api("GET", `handouts${query(path)}`, null, { signal });
    } catch (e) {
      if (e.name === "AbortError") return;
      if (e.status === 404 && path) { CC.toast("That folder is gone; showing Handouts.", { kind: "info" }); return loadHandouts("", { signal }); }
      showError("#handouts-error", e);
      throw e;
    }
    hideError("#handouts-error");
    renderHandouts(data);
  }

  function renderHandouts(data) {
    state.path = data.path; state.maxBytes = data.max_upload_bytes || 0; state.entries = data.entries; state.loaded = true;
    CC.setText($("#handouts-max"), state.maxBytes ? CC.bytes(state.maxBytes) : "…");
    renderCrumbs();
    const tbody = $("#handouts-table tbody");
    tbody.replaceChildren();
    data.entries.forEach((e) => tbody.appendChild(handoutRow(e)));
    const count = data.entries.length;
    const files = data.entries.filter((e) => e.type === "file").length;
    const dirs = data.entries.filter((e) => e.type === "dir").length;
    CC.setText($("#handouts-count"), count ? `(${[files ? `${files} file${files === 1 ? "" : "s"}` : "", dirs ? `${dirs} folder${dirs === 1 ? "" : "s"}` : ""].filter(Boolean).join(", ")})` : "");
    $("#handouts-wrap").hidden = !count;
    $("#handouts-toolbar").hidden = count < 8;
    $("#handouts-empty").hidden = !!count;
    CC.setText($("#handouts-empty-title"), state.path ? "This folder is empty" : "No handouts yet");
    CC.setText($("#handouts-empty-text"), state.path ? "Drop files here to add them to this folder." : "Drop notebooks, datasets or PDFs here and every student sees them instantly.");
    CC.refreshTimes(tbody);
    handoutsTable.apply();
  }

  function renderCrumbs() {
    const nav = $("#handouts-crumbs");
    nav.replaceChildren();
    const parts = state.path ? state.path.split("/") : [];
    const crumb = (label, target, current) => {
      if (current) { const s = el("span", "cc-crumbs__here", label); s.setAttribute("aria-current", "location"); return s; }
      const b = el("button", "cc-crumbs__link", label); b.type = "button";
      b.addEventListener("click", () => loadHandouts(target));
      return b;
    };
    nav.appendChild(icon("fa-folder-open cc-crumbs__icon"));
    nav.appendChild(crumb("Handouts", "", parts.length === 0));
    parts.forEach((p, i) => {
      nav.appendChild(el("span", "cc-crumbs__sep", "/"));
      nav.appendChild(crumb(p, parts.slice(0, i + 1).join("/"), i === parts.length - 1));
    });
  }

  function handoutRow(e) {
    const rel = join(state.path, e.name);
    const tr = el("tr");
    tr.dataset.search = e.name;
    if (e.type !== "file" && e.type !== "dir") tr.classList.add("cc-row--muted");

    const tdName = el("td");
    // "d-"/"f-" keeps folders first; a non-numeric prefix so CC.table compares the names, not parseFloat()
    tdName.dataset.key = "name"; tdName.dataset.value = (e.type === "dir" ? "d-" : "f-") + e.name.toLowerCase();
    const cell = el("div", "cc-file-cell");
    cell.appendChild(icon(`${iconFor(e)} cc-file-icon cc-file-icon--${e.type} cc-file-icon--${ext(e.name) || "none"}`));
    if (e.type === "dir") {
      const b = el("button", "btn btn-link p-0 cc-file-name cc-file-name--dir", e.name);
      b.type = "button"; b.setAttribute("aria-label", `Open folder ${e.name}`);
      b.addEventListener("click", () => loadHandouts(rel));
      cell.appendChild(b);
    } else {
      cell.appendChild(el("span", "cc-file-name", e.name));
      if (e.type === "symlink") cell.appendChild(el("span", "badge text-bg-light ms-2", "link"));
      if (e.type === "other") cell.appendChild(el("span", "badge text-bg-light ms-2", "special file"));
    }
    tdName.appendChild(cell);

    const tdSize = el("td", "num", e.type === "file" ? CC.bytes(e.size) : "—");
    tdSize.dataset.key = "size"; tdSize.dataset.value = e.type === "file" ? e.size : -1;

    const tdTime = el("td");
    tdTime.dataset.key = "mtime"; tdTime.dataset.value = String(Date.parse(e.mtime) || 0);
    const t = el("time"); t.setAttribute("datetime", e.mtime); tdTime.appendChild(t);

    const tdActions = el("td", "text-end text-nowrap");
    if (e.type === "file") {
      const a = el("a", "btn btn-sm btn-outline-secondary me-1");
      a.href = `${CC.prefix}/api/handouts/download${query(rel)}`;
      a.title = "Download"; a.setAttribute("aria-label", `Download ${e.name}`);
      a.appendChild(icon("fa-download"));
      tdActions.appendChild(a);
    }
    const del = el("button", "btn btn-sm btn-outline-danger");
    del.type = "button"; del.title = "Delete"; del.setAttribute("aria-label", `Delete ${e.name}`);
    del.appendChild(icon("fa-trash-can"));
    del.addEventListener("click", () => confirmDelete(e, rel));
    tdActions.appendChild(del);

    tr.append(tdName, tdSize, tdTime, tdActions);
    return tr;
  }

  async function confirmDelete(e, rel) {
    const texts = {
      dir: [`Delete the folder "${e.name}" and everything inside it?`, "Students will no longer see those files in their shared folder. Copies they already saved elsewhere are not affected.", "Delete folder"],
      file: [`Delete "${e.name}" from Handouts?`, "Students will no longer see it in their shared folder. Copies they already saved elsewhere are not affected.", "Delete file"],
      other: [`Remove "${e.name}" from Handouts?`, "Only this entry is removed; whatever it points to is not touched.", "Remove"],
    };
    const [title, body, label] = texts[e.type === "dir" ? "dir" : e.type === "file" ? "file" : "other"];
    if (!(await CC.confirm({ title, body, confirmLabel: label, danger: true }))) return;
    try {
      await CC.api("DELETE", `handouts${query(rel)}`);
      CC.toast(e.type === "dir" ? `Folder "${e.name}" deleted.` : `"${e.name}" deleted.`, { kind: "success" });
    } catch (err) {
      if (err.status === 404) CC.toast(`"${e.name}" was already gone.`, { kind: "info" }); else CC.error(err);
    }
    await loadHandouts().catch(() => {});
    $("#handouts-drop").focus();
  }

  // ===== Handouts: uploads ===================================================
  const uploads = { queue: [], active: 0, MAX: 2, done: 0, unpacked: 0, failed: 0, skipped: 0 };

  function addFiles(files, { folders = 0 } = {}) {
    const list = Array.from(files || []);
    if (folders) CC.toast(folders === 1 ? "Folders can't be uploaded here — zip the folder first, it is unpacked automatically." : "Folders can't be uploaded here — zip them first.", { kind: "error" });
    if (!list.length) return;
    list.forEach((file) => {
      const row = uploadRow(file);
      if (state.maxBytes && file.size > state.maxBytes) { rowError(row, `Too large (max ${CC.bytes(state.maxBytes)})`); return; }
      uploads.queue.push({ file, row, path: state.path, unzip: $("#opt-unzip").checked, overwrite: $("#opt-overwrite").checked });
    });
    pump();
  }

  function pump() {
    while (uploads.active < uploads.MAX && uploads.queue.length) {
      const job = uploads.queue.shift();
      uploads.active += 1;
      run(job).finally(() => { uploads.active -= 1; pump(); });
    }
    if (!uploads.active && !uploads.queue.length) finished();
  }

  let reloadTimer = null;
  const scheduleReload = () => { clearTimeout(reloadTimer); reloadTimer = setTimeout(() => loadHandouts().catch(() => {}), 250); };

  function finished() {
    const { done, unpacked, failed, skipped } = uploads;
    if (!done && !unpacked && !failed) return;
    const bits = [];
    if (done) bits.push(`${done} file${done === 1 ? "" : "s"} uploaded`);
    if (unpacked) bits.push(`${unpacked} unpacked from zip`);
    if (skipped) bits.push(`${skipped} skipped`);
    if (failed) bits.push(`${failed} failed`);
    CC.toast(bits.join(", ") + ".", { kind: failed ? "error" : "success" });
    uploads.done = uploads.unpacked = uploads.failed = uploads.skipped = 0;
    scheduleReload();
  }

  function uploadRow(file) {
    const row = el("div", "cc-upload-row"); row.setAttribute("role", "status");
    const main = el("div", "cc-upload-row__main");
    const nameLine = el("div", "cc-upload-row__name");
    nameLine.appendChild(icon(`${ICONS[ext(file.name)] || "fa-file"} cc-file-icon`));
    nameLine.appendChild(el("span", "name", file.name));
    nameLine.appendChild(el("span", "cc-muted small", CC.bytes(file.size)));
    const progress = el("div", "progress"); progress.setAttribute("role", "progressbar");
    progress.setAttribute("aria-label", `Uploading ${file.name}`); progress.setAttribute("aria-valuemin", "0"); progress.setAttribute("aria-valuemax", "100"); progress.setAttribute("aria-valuenow", "0");
    progress.appendChild(el("div", "progress-bar"));
    main.append(nameLine, progress, el("div", "cc-upload-row__msg", "Waiting…"));
    const actions = el("div", "cc-upload-row__actions");
    row.append(main, actions);
    $("#handouts-uploads").appendChild(row);
    return row;
  }
  const setProgress = (row, pct) => {
    const p = $(".progress", row); if (!p) return;
    p.setAttribute("aria-valuenow", String(Math.round(pct)));
    $(".progress-bar", row).style.width = `${pct}%`;
    CC.setText($(".cc-upload-row__msg", row), pct >= 100 ? "Saving on the server…" : `Uploading… ${Math.round(pct)} %`);
  };
  const rowButton = (row, label, onClick, cls = "btn btn-sm btn-outline-secondary") => {
    const b = el("button", cls, label); b.type = "button"; b.addEventListener("click", onClick);
    $(".cc-upload-row__actions", row).appendChild(b); return b;
  };
  const clearActions = (row) => $(".cc-upload-row__actions", row).replaceChildren();
  function rowError(row, message, retry) {
    row.classList.remove("is-done"); row.classList.add("is-error");
    const p = $(".progress", row); if (p) p.hidden = true;
    CC.setText($(".cc-upload-row__msg", row), message);
    clearActions(row);
    if (retry) rowButton(row, "Retry", retry);
    rowButton(row, "Dismiss", () => row.remove(), "btn btn-sm btn-link");
  }
  function rowDone(job, data) {
    const row = job.row;
    row.classList.add("is-done");
    const p = $(".progress", row); if (p) p.hidden = true;
    const n = data.extracted || 0, skipped = (data.skipped || []).length;
    let msg = "Uploaded";
    if (n) { const into = (data.unpacked_into || [])[0]; msg = `Unpacked ${n} file${n === 1 ? "" : "s"}${into ? ` into "${into}"` : ""}`; }
    if (skipped) msg += ` · ${skipped} entr${skipped === 1 ? "y" : "ies"} skipped (${[...new Set((data.skipped || []).map((s) => s.reason))].join(", ")})`;
    CC.setText($(".cc-upload-row__msg", row), msg);
    clearActions(row);
    uploads.done += (data.saved || []).length; uploads.unpacked += n; uploads.skipped += skipped;
    scheduleReload();
    setTimeout(() => row.remove(), skipped ? 12000 : 4000);
  }
  function rowConflict(job, data) {
    const row = job.row;
    row.classList.add("is-error");
    const p = $(".progress", row); if (p) p.hidden = true;
    const isDir = data.detail && data.detail.kind === "dir";
    CC.setText($(".cc-upload-row__msg", row), isDir ? "A folder with this name is already there." : "Already in Handouts — replace it?");
    clearActions(row);
    if (!isDir) rowButton(row, "Replace", () => { row.classList.remove("is-error"); if (p) p.hidden = false; uploads.queue.push(Object.assign(job, { overwrite: true })); pump(); }, "btn btn-sm btn-primary");
    rowButton(row, "Skip", () => row.remove(), "btn btn-sm btn-link");
  }

  function run(job) {
    return new Promise((resolve) => {
      const row = job.row;
      const xhr = new XMLHttpRequest();
      const form = new FormData();
      form.append("path", job.path);
      form.append("unzip", job.unzip ? "1" : "0");
      form.append("overwrite", job.overwrite ? "1" : "0");
      form.append("keep_zip", "0");
      form.append("files", job.file, job.file.name);
      xhr.open("POST", `${CC.prefix}/api/handouts`);
      xhr.setRequestHeader("X-Console-Request", "1");
      xhr.setRequestHeader("Accept", "application/json");
      xhr.upload.addEventListener("progress", (ev) => { if (ev.lengthComputable) setProgress(row, (ev.loaded / ev.total) * 100); });
      xhr.addEventListener("load", () => {
        let data = {};
        try { data = JSON.parse(xhr.responseText || "{}"); } catch (e) { data = {}; }
        if (xhr.status >= 200 && xhr.status < 300) rowDone(job, data);
        else if (xhr.status === 401) window.location.href = `${CC.prefix}/login?next=${encodeURIComponent(location.pathname)}`;
        else if (xhr.status === 409 && data.error === "exists") rowConflict(job, data);
        else if (xhr.status === 413) { uploads.failed += 1; rowError(row, data.message || `Too large (max ${CC.bytes(state.maxBytes)})`); }
        else { uploads.failed += 1; rowError(row, data.message || `Upload failed (${xhr.status}) — try again`, () => retry(job)); }
        resolve();
      });
      xhr.addEventListener("error", () => { uploads.failed += 1; rowError(row, "Upload failed — check the connection and try again", () => retry(job)); resolve(); });
      xhr.addEventListener("abort", () => { row.remove(); resolve(); });
      const cancel = rowButton(row, "Cancel", () => xhr.abort(), "btn btn-sm btn-link");
      cancel.setAttribute("aria-label", `Cancel upload of ${job.file.name}`);
      CC.setText($(".cc-upload-row__msg", row), "Uploading…");
      xhr.send(form);
    });
  }
  function retry(job) {
    job.row.classList.remove("is-error");
    const p = $(".progress", job.row); if (p) { p.hidden = false; setProgress(job.row, 0); }
    clearActions(job.row);
    uploads.queue.push(job); pump();
  }

  // dropzone + picker
  const drop = $("#handouts-drop"), input = $("#handouts-input"), card = $("#handouts-card");
  const openPicker = () => input.click();
  drop.addEventListener("click", openPicker);
  drop.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openPicker(); } });
  $("#handouts-upload").addEventListener("click", openPicker);
  input.addEventListener("change", () => { addFiles(input.files); input.value = ""; });
  const hasFiles = (dt) => dt && Array.from(dt.types || []).includes("Files");
  card.addEventListener("dragenter", (e) => { if (hasFiles(e.dataTransfer)) { e.preventDefault(); drop.classList.add("is-over"); } });
  card.addEventListener("dragover", (e) => { if (hasFiles(e.dataTransfer)) { e.preventDefault(); e.dataTransfer.dropEffect = "copy"; drop.classList.add("is-over"); } });
  card.addEventListener("dragleave", (e) => { if (!card.contains(e.relatedTarget)) drop.classList.remove("is-over"); });
  card.addEventListener("drop", (e) => {
    if (!hasFiles(e.dataTransfer)) return;
    e.preventDefault(); drop.classList.remove("is-over");
    const files = [];
    let folders = 0;
    const items = e.dataTransfer.items ? Array.from(e.dataTransfer.items) : [];
    if (items.length && items[0].kind !== undefined) {
      items.forEach((it) => {
        if (it.kind !== "file") return;
        const entry = it.webkitGetAsEntry ? it.webkitGetAsEntry() : null;
        if (entry && entry.isDirectory) { folders += 1; return; }
        const f = it.getAsFile(); if (f) files.push(f);
      });
    } else { files.push(...Array.from(e.dataTransfer.files || [])); }
    addFiles(files, { folders });
  });
  // a file dropped outside the card must not replace the page
  window.addEventListener("dragover", (e) => { if (hasFiles(e.dataTransfer)) e.preventDefault(); });
  window.addEventListener("drop", (e) => { if (hasFiles(e.dataTransfer)) e.preventDefault(); });

  // new folder
  const folderModalEl = $("#newfolder-modal");
  const folderModal = bootstrap.Modal.getOrCreateInstance(folderModalEl);
  let folderTrigger = null;
  const folderName = $("#newfolder-name"), folderErr = $("#newfolder-error"), folderOk = $("#newfolder-ok");
  const setFolderError = (msg) => { folderName.classList.toggle("is-invalid", !!msg); folderName.setAttribute("aria-invalid", msg ? "true" : "false"); CC.setText(folderErr, msg || ""); };
  $("#handouts-newfolder").addEventListener("click", (e) => {
    folderTrigger = e.currentTarget; folderName.value = ""; setFolderError("");
    CC.setText($("#newfolder-where"), state.path ? `Inside Handouts / ${state.path.split("/").join(" / ")}` : "Inside Handouts");
    folderModal.show();
  });
  folderModalEl.addEventListener("shown.bs.modal", () => folderName.focus());
  folderModalEl.addEventListener("hidden.bs.modal", () => { if (folderTrigger) folderTrigger.focus(); });
  $("#newfolder-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const name = folderName.value.trim();
    if (!name) return setFolderError("Give the folder a name.");
    if (/[\\/]/.test(name) || name.startsWith(".") || name === "..") return setFolderError("Folder names cannot start with a dot or contain / or \\.");
    folderOk.disabled = true; folderOk.setAttribute("aria-busy", "true");
    try {
      await CC.api("POST", "handouts/mkdir", { path: join(state.path, name) });
      folderModal.hide();
      CC.toast(`Folder "${name}" created.`, { kind: "success" });
      await loadHandouts().catch(() => {});
    } catch (err) {
      setFolderError(err.message || "Could not create the folder.");
    } finally { folderOk.disabled = false; folderOk.removeAttribute("aria-busy"); }
  });

  // ===== Submissions =========================================================
  async function loadSubmissions(signal) {
    let rows;
    try { rows = await CC.api("GET", "submissions", null, { signal }); } catch (e) { if (e.name !== "AbortError") showError("#submissions-error", e); throw e; }
    hideError("#submissions-error");
    const tbody = $("#submissions-table tbody");
    tbody.replaceChildren();
    let handedIn = 0;
    rows.forEach((s) => { if (s.file_count) handedIn += 1; tbody.appendChild(submissionRow(s)); });
    $("#submissions-wrap").hidden = !rows.length;
    $("#submissions-empty").hidden = !!rows.length;
    $("#submissions-note").hidden = !rows.length || handedIn > 0;
    $("#submissions-footer").hidden = !rows.length;
    const all = $("#submissions-all");
    all.classList.toggle("disabled", !handedIn); all.setAttribute("aria-disabled", handedIn ? "false" : "true"); all.title = handedIn ? "" : "Nothing handed in yet";
    CC.setText($("#submissions-count"), rows.length ? `(${handedIn} of ${rows.length} folders contain files)` : "");
    $("#submissions-updated").setAttribute("datetime", new Date().toISOString());
    CC.refreshTimes();
    submissionsTable.apply();
  }

  function submissionRow(s) {
    const tr = el("tr");
    tr.dataset.search = `${s.display_name || ""} ${s.username}`;
    if (!s.file_count) tr.classList.add("cc-row--muted");
    const tdWho = el("td");
    tdWho.dataset.key = "student"; tdWho.dataset.value = (s.display_name || s.username).toLowerCase();
    const who = el("div", "cc-student");
    who.appendChild(el("span", "cc-student__name", s.display_name || s.username));
    if (s.is_admin) who.appendChild(el("span", "badge text-bg-light ms-2", "teacher"));
    if (s.display_name) who.appendChild(el("span", "cc-student__user", s.username));
    if (!s.account) { const b = el("span", "badge text-bg-warning ms-2", "no account"); b.title = "No Linux account yet — open Students and use Repair"; who.appendChild(b); }
    tdWho.appendChild(who);

    // Numbers and times never wrap; the student's name is the one cell allowed to.
    const tdFiles = el("td", "num cc-nowrap", s.file_count ? (s.capped ? `${s.file_count.toLocaleString()}+` : s.file_count.toLocaleString()) : "0");
    tdFiles.dataset.key = "files"; tdFiles.dataset.value = s.file_count;
    if (s.large_files) { const w = icon("fa-triangle-exclamation ms-1 text-warning"); w.removeAttribute("aria-hidden"); w.setAttribute("role", "img"); w.setAttribute("aria-label", `${s.large_files} file(s) over 100 MB are left out of downloads`); w.title = `${s.large_files} file(s) over 100 MB are left out of downloads — use Backups for those`; tdFiles.appendChild(w); }

    const tdTime = el("td", "cc-nowrap");
    tdTime.dataset.key = "mtime"; tdTime.dataset.value = String(s.latest_mtime ? Date.parse(s.latest_mtime) || 0 : 0);
    if (s.latest_mtime) { const t = el("time"); t.setAttribute("datetime", s.latest_mtime); tdTime.appendChild(t); } else tdTime.appendChild(el("span", "cc-muted", "No files yet"));

    const tdSize = el("td", "num cc-nowrap", s.file_count ? CC.bytes(s.total_bytes) : "—");
    tdSize.dataset.key = "size"; tdSize.dataset.value = s.total_bytes;

    const tdActions = el("td", "text-end text-nowrap");
    const a = el("a", "btn btn-sm btn-outline-secondary");
    a.href = `${CC.prefix}/api/submissions/${encodeURIComponent(s.username)}/download`;
    a.dataset.zip = "1"; a.appendChild(icon("fa-download"));
    a.title = "Download zip"; a.setAttribute("aria-label", `Download ${s.display_name || s.username}'s submissions as a zip`);
    if (!s.file_count) { a.classList.add("disabled"); a.setAttribute("aria-disabled", "true"); a.title = "Nothing to download yet"; }
    tdActions.appendChild(a);

    tr.append(tdWho, tdFiles, tdTime, tdSize, tdActions);
    return tr;
  }

  document.addEventListener("click", (e) => {
    const a = e.target.closest("a[data-zip]");
    if (!a) return;
    if (a.classList.contains("disabled")) { e.preventDefault(); return; }
    CC.toast("Preparing your download…", { kind: "info" });
  });

  // ===== errors, boot ========================================================
  function showError(sel, e) { const box = $(sel); box.textContent = (e && e.message) || "Something went wrong."; box.hidden = false; }
  function hideError(sel) { $(sel).hidden = true; }

  const submissionsPoll = CC.poll(loadSubmissions, 60000);
  $("#submissions-refresh").addEventListener("click", () => { const i = $("#submissions-refresh .fa"); i.classList.add("fa-spin"); setTimeout(() => i.classList.remove("fa-spin"), 800); submissionsPoll.now(); });
  CC.poll(async (signal) => { if (!document.querySelector(".cc-upload-row") && !folderModalEl.classList.contains("show")) await loadHandouts(state.path, { signal }); }, 90000);
  setInterval(() => CC.refreshTimes(), 30000);
})();
