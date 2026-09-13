/* Students page: class list, add / bulk add, reset password, remove, print cards.
   Uses the shared helpers in console.js (window.CC). Vanilla JS, no build step. */
(function () {
  "use strict";
  const CC = window.CC;
  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

  const USERNAME_RE = /^[a-z_][a-z0-9_-]{0,31}$/;
  const RESERVED = new Set(["root", "jovyan", "nobody", "daemon", "bin", "sys", "sync", "games", "man", "lp", "mail", "news", "uucp",
    "proxy", "www-data", "backup", "list", "irc", "_apt", "admin", "administrator", "hub", "jupyterhub", "console"]);
  const WORDS = ["apple", "mango", "lemon", "melon", "grape", "peach", "berry", "tiger", "lion", "zebra", "panda", "koala", "otter",
    "eagle", "falcon", "whale", "red", "blue", "green", "amber", "coral", "ivory", "olive", "violet", "silver", "river", "ocean",
    "forest", "meadow", "island", "canyon", "summit", "valley", "harbor", "rocket", "comet", "planet", "galaxy", "meteor", "orbit",
    "lunar", "solar", "maple", "cedar", "willow", "birch", "bamboo", "lotus", "tulip", "daisy", "clover", "fern"];
  const STATES = { running: "Running", starting: "Starting…", stopping: "Stopping…", stopped: "Stopped", unknown: "Unknown" };
  const POLL_MS = 15000;

  const state = { students: [], admins: [], reveal: false, selected: new Set(), loaded: false, hubError: null };
  const el = {
    card: $("#students-card"), empty: $("#students-empty"), loading: $("#students-loading"), body: $("#students-body"),
    table: $("#students-table"), footer: $("#students-footer"), search: $("#students-search"), searchTerm: $("#search-term"),
    showPw: $("#show-passwords"), selection: $("#selection"), selCount: $("#selection-count"), selectAll: $("#select-all"),
    hubAlert: $("#hub-alert"), hubAlertText: $("#hub-alert-text"), repairAlert: $("#repair-alert"), repairText: $("#repair-alert-text"),
    subtitle: $(".cc-page-subtitle"),
  };
  let tableCtl = null;
  let poller = null;

  // ---------------------------------------------------------------- helpers
  const nameOf = (s) => s.display_name || s.username;
  const isAdmin = (s) => !!s.is_admin || state.admins.includes(s.username);
  const isSelf = (s) => s.username === CC.user;
  const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
  const findStudent = (u) => state.students.find((s) => s.username === u);
  const rowOf = (u) => el.body.querySelector(`tr[data-user="${CSS.escape(u)}"]`);

  function rand(n) {
    if (window.crypto && crypto.getRandomValues) { const a = new Uint32Array(1); crypto.getRandomValues(a); return a[0] % n; }
    return Math.floor(Math.random() * n);
  }
  function localPassword() {
    const a = WORDS[rand(WORDS.length)]; let b = a; while (b === a) b = WORDS[rand(WORDS.length)];
    return `${a}-${b}-${10 + rand(90)}`;
  }
  function localSuggest(name) {
    const parts = name.normalize("NFKD").replace(/[\u0300-\u036f]/g, "").toLowerCase().split(/\s+/).map((p) => p.replace(/[^a-z0-9_-]/g, "")).filter(Boolean);
    let first = (parts[0] || "student").slice(0, 24);
    if (!/^[a-z_]/.test(first)) first = "s" + first;
    const taken = new Set(state.students.map((s) => s.username));
    const cands = [first];
    if (parts.length > 1) { const last = parts[parts.length - 1]; cands.push((first + last[0]).slice(0, 32)); cands.push((first + last).slice(0, 32)); }
    for (const c of cands) if (!taken.has(c) && !RESERVED.has(c) && USERNAME_RE.test(c)) return c;
    let n = 2; while (taken.has(`${first.slice(0, 28)}${n}`)) n += 1;
    return `${first.slice(0, 28)}${n}`;
  }
  async function fillPassword(input) {
    try { const r = await CC.api("GET", "students/generate-password"); input.value = r.password; }
    catch (e) { input.value = localPassword(); }
    input.type = "text";
    const eye = input.parentElement.querySelector("[data-eye]"); if (eye) eye.setAttribute("aria-pressed", "true");
  }
  function printUrl(usernames) {
    const base = `${CC.prefix}/print/cards`;
    return usernames && usernames.length ? `${base}?students=${encodeURIComponent(usernames.join(","))}` : base;
  }
  function openPrint(usernames) { window.open(printUrl(usernames), "_blank", "noopener"); }
  function setBusy(modalEl, busy) {
    modalEl.toggleAttribute("data-busy", busy);
    $$("button[type=submit], [data-submit]", modalEl).forEach((b) => { b.disabled = busy; b.setAttribute("aria-busy", String(busy)); });
  }
  function fieldError(id, msg) {
    const box = document.getElementById(`${id}-error`); const input = document.getElementById(id);
    if (!box) return;
    box.hidden = !msg; box.textContent = msg || "";
    if (input) { input.classList.toggle("is-invalid", !!msg); if (msg) input.setAttribute("aria-invalid", "true"); else input.removeAttribute("aria-invalid"); }
  }
  const modal = (id) => bootstrap.Modal.getOrCreateInstance(document.getElementById(id));
  function openModal(id, onShown) {
    const m = document.getElementById(id); const trigger = document.activeElement;
    m.addEventListener("hidden.bs.modal", () => { if (trigger && document.body.contains(trigger) && typeof trigger.focus === "function") trigger.focus(); }, { once: true });
    if (onShown) m.addEventListener("shown.bs.modal", onShown, { once: true });
    modal(id).show();
  }
  $$(".modal").forEach((m) => m.addEventListener("hide.bs.modal", (e) => { if (m.hasAttribute("data-busy")) e.preventDefault(); }));

  // ---------------------------------------------------------------- table
  function repairReason(s) {
    const r = [];
    if (!s.linux_ok) r.push("no Linux account");
    if (!s.home_ok) r.push("no home folder");
    if (s.hub_ok === false) r.push("not registered with JupyterHub");
    return r.length ? `Needs repair: ${r.join(", ")}` : "";
  }
  function buildRow(s) {
    const tr = document.createElement("tr");
    tr.dataset.user = s.username;
    tr.innerHTML = `
      <td><input class="form-check-input" type="checkbox" data-select></td>
      <td data-key="name"><span class="cc-name"></span> <span class="badge text-bg-secondary cc-teacher-badge" hidden>Teacher</span><i class="fa fa-triangle-exclamation cc-warn-icon" hidden aria-hidden="true"></i><span class="visually-hidden cc-warn-text"></span></td>
      <td data-key="username"><code class="cc-mono cc-username"></code> <button type="button" class="cc-copy" data-copy=""><i class="fa fa-copy" aria-hidden="true"></i></button></td>
      <td data-key="password" class="cc-pw-cell"></td>
      <td data-key="state"></td>
      <td class="text-end"><div class="dropdown">
        <button class="btn btn-sm btn-outline-secondary" type="button" data-bs-toggle="dropdown" data-bs-popper-config='{"strategy":"fixed"}' aria-expanded="false"><i class="fa fa-ellipsis" aria-hidden="true"></i></button>
        <ul class="dropdown-menu dropdown-menu-end">
          <li><button class="dropdown-item" type="button" data-action="reset"><i class="fa fa-key fa-fw" aria-hidden="true"></i> Reset password…</button></li>
          <li><button class="dropdown-item" type="button" data-action="print"><i class="fa fa-print fa-fw" aria-hidden="true"></i> Print login card</button></li>
          <li><button class="dropdown-item" type="button" data-action="server"><i class="fa fa-play fa-fw" aria-hidden="true"></i> <span class="cc-server-label">Start server</span></button></li>
          <li><hr class="dropdown-divider"></li>
          <li><button class="dropdown-item text-danger" type="button" data-action="remove"><i class="fa fa-user-minus fa-fw" aria-hidden="true"></i> Remove student…</button></li>
        </ul></div></td>`;
    updateRow(tr, s);
    return tr;
  }
  function updateRow(tr, s) {
    const name = nameOf(s);
    tr.dataset.search = `${name} ${s.username}`.toLowerCase();
    const cb = tr.querySelector("[data-select]");
    cb.setAttribute("aria-label", `Select ${name}`); cb.checked = state.selected.has(s.username);

    const nameTd = tr.querySelector('[data-key="name"]');
    nameTd.dataset.value = name.toLowerCase();
    CC.setText(nameTd.querySelector(".cc-name"), name);
    nameTd.querySelector(".cc-teacher-badge").hidden = !isAdmin(s);
    const warn = repairReason(s); const icon = nameTd.querySelector(".cc-warn-icon");
    icon.hidden = !warn; icon.title = warn; CC.setText(nameTd.querySelector(".cc-warn-text"), warn);

    const uTd = tr.querySelector('[data-key="username"]');
    uTd.dataset.value = s.username; CC.setText(uTd.querySelector(".cc-username"), s.username);
    const copy = uTd.querySelector(".cc-copy"); copy.dataset.copy = s.username; copy.setAttribute("aria-label", `Copy username ${s.username}`); copy.title = "Copy username";

    renderPassword(tr, s);

    const stTd = tr.querySelector('[data-key="state"]');
    stTd.dataset.value = s.state; renderPill(stTd, s);

    tr.querySelector("[data-bs-toggle=dropdown]").setAttribute("aria-label", `Actions for ${name}`);
    const running = s.state === "running" || s.state === "starting";
    const serverBtn = tr.querySelector('[data-action="server"]');
    CC.setText(serverBtn.querySelector(".cc-server-label"), running ? "Stop server" : "Start server");
    serverBtn.dataset.serverAction = running ? "stop" : "start";
    serverBtn.disabled = s.state === "stopping" || s.state === "unknown" || s.hub_ok === false;
    serverBtn.querySelector(".fa").className = `fa fa-fw ${running ? "fa-stop" : "fa-play"}`;

    const rm = tr.querySelector('[data-action="remove"]');
    const blocked = isSelf(s) ? "You cannot remove the account you are signed in with." : isAdmin(s) ? "Teacher accounts are set in compose.yaml" : "";
    rm.disabled = !!blocked; rm.title = blocked; rm.closest("li").title = blocked;
  }
  function renderPassword(tr, s) {
    const td = tr.querySelector(".cc-pw-cell");
    if (state.reveal && typeof s.password === "string") {
      if (td.dataset.mode !== "shown") {
        td.innerHTML = '<code class="cc-mono cc-pw"></code> <button type="button" class="cc-copy" data-copy="" aria-label="Copy password" title="Copy password"><i class="fa fa-copy" aria-hidden="true"></i></button>';
        td.dataset.mode = "shown";
      }
      CC.setText(td.querySelector(".cc-pw"), s.password);
      td.querySelector(".cc-copy").dataset.copy = s.password;
    } else if (td.dataset.mode !== "hidden") {
      td.innerHTML = '<span class="cc-secret" aria-label="Password hidden">••••••••</span>';
      td.dataset.mode = "hidden";
    }
  }
  function renderPill(td, s) {
    const st = STATES[s.state] ? s.state : "unknown";
    let pill = td.querySelector(".cc-pill");
    if (!pill) { pill = document.createElement("span"); pill.innerHTML = '<i class="fa fa-circle" aria-hidden="true"></i><span class="cc-pill__text"></span>'; td.appendChild(pill); }
    pill.className = `cc-pill cc-pill--${st}`;
    pill.querySelector(".fa").className = `fa fa-circle${st === "starting" || st === "stopping" ? " cc-pulse" : ""}`;
    CC.setText(pill.querySelector(".cc-pill__text"), STATES[st]);
    pill.title = st === "unknown" ? "JupyterHub is not answering" : s.hub_ok === false ? "Not registered with JupyterHub yet — use Repair" : "";
  }
  function render() {
    const known = new Map(); $$("tr[data-user]", el.body).forEach((tr) => known.set(tr.dataset.user, tr));
    const seen = new Set();
    state.students.forEach((s) => { seen.add(s.username); const tr = known.get(s.username); if (tr) updateRow(tr, s); else el.body.appendChild(buildRow(s)); });
    known.forEach((tr, u) => { if (!seen.has(u)) { tr.remove(); state.selected.delete(u); } });
    [...state.selected].forEach((u) => { if (!seen.has(u)) state.selected.delete(u); });
    // Teacher accounts are roster entries too, so "no students yet" means no non-teacher rows (the table still lists the teachers).
    const has = state.students.length > 0;
    const learners = state.students.filter((s) => !isAdmin(s)).length;
    el.loading.hidden = true; el.card.hidden = !has; el.empty.hidden = learners > 0;
    if (!tableCtl) tableCtl = CC.table(el.table); else tableCtl.apply();
    const online = state.students.filter((s) => s.state === "running").length;
    const teachers = state.students.filter(isAdmin).length;
    CC.setText(el.subtitle, `${plural(state.students.length, "account")} · ${state.hubError ? "status unknown" : `${online} online`}`);
    CC.setText(el.footer, `${plural(state.students.length - teachers, "student")} · ${plural(teachers, "teacher account")} · statuses refresh every ${POLL_MS / 1000} s`);
    updateSelection(); updateAlerts();
  }
  function updateAlerts() {
    el.hubAlert.hidden = !state.hubError; CC.setText(el.hubAlertText, state.hubError || "");
    const needs = state.hubError ? [] : state.students.filter((s) => repairReason(s));
    el.repairAlert.hidden = needs.length === 0;
    if (needs.length) CC.setText(el.repairText, `${plural(needs.length, "account")} need${needs.length === 1 ? "s" : ""} repair (${needs.slice(0, 3).map(nameOf).join(", ")}${needs.length > 3 ? ", …" : ""}). Repair re-creates missing Linux accounts and Hub registrations; it never deletes anything.`);
  }

  // ---------------------------------------------------------------- loading
  async function load(signal) {
    const data = await CC.api("GET", `students?reveal=${state.reveal ? 1 : 0}`, null, { signal });
    state.students = data.students || []; state.admins = data.admins || []; state.hubError = data.hub_error || null; state.loaded = true;
    render();
  }
  function showLoadError(e) {
    el.loading.innerHTML = "";
    const p = document.createElement("p"); p.className = "cc-muted mb-2"; p.textContent = `The class list could not be loaded: ${e.message}`;
    const b = document.createElement("button"); b.type = "button"; b.className = "btn btn-outline-secondary btn-sm"; b.textContent = "Try again"; b.addEventListener("click", refresh);
    el.loading.append(p, b);
  }
  const refresh = () => { if (poller) poller.now(); };

  // ---------------------------------------------------------------- toolbar
  el.search.addEventListener("input", () => CC.setText(el.searchTerm, el.search.value));
  el.showPw.addEventListener("change", () => {
    state.reveal = el.showPw.checked;
    if (!state.reveal) { state.students.forEach((s) => { delete s.password; }); render(); }
    refresh();
  });
  el.body.addEventListener("change", (e) => {
    const cb = e.target.closest("[data-select]"); if (!cb) return;
    const u = cb.closest("tr").dataset.user;
    if (cb.checked) state.selected.add(u); else state.selected.delete(u);
    updateSelection();
  });
  el.selectAll.addEventListener("change", () => {
    $$("tr[data-user]", el.body).forEach((tr) => { if (tr.hidden) return; const cb = tr.querySelector("[data-select]"); cb.checked = el.selectAll.checked; if (cb.checked) state.selected.add(tr.dataset.user); else state.selected.delete(tr.dataset.user); });
    updateSelection();
  });
  function updateSelection() {
    const n = state.selected.size;
    el.selection.hidden = n === 0; CC.setText(el.selCount, `${n} selected`);
    const visible = $$("tr[data-user]", el.body).filter((tr) => !tr.hidden);
    el.selectAll.checked = visible.length > 0 && visible.every((tr) => state.selected.has(tr.dataset.user));
    el.selectAll.indeterminate = n > 0 && !el.selectAll.checked;
  }
  $("#btn-print-selected").addEventListener("click", () => openPrint([...state.selected]));
  $("#btn-clear-selection").addEventListener("click", () => { state.selected.clear(); $$("[data-select]", el.body).forEach((cb) => { cb.checked = false; }); updateSelection(); });

  // ---------------------------------------------------------------- row actions
  el.body.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-action]"); if (!btn || btn.disabled) return;
    const s = findStudent(btn.closest("tr").dataset.user); if (!s) return;
    if (btn.dataset.action === "reset") openReset(s);
    else if (btn.dataset.action === "print") openPrint([s.username]);
    else if (btn.dataset.action === "server") toggleServer(s, btn.dataset.serverAction);
    else if (btn.dataset.action === "remove") openRemove(s);
  });
  async function toggleServer(s, action) {
    try {
      await CC.api("POST", `servers/${encodeURIComponent(s.username)}/${action}`, {});
      CC.toast(action === "stop" ? `Stopping ${nameOf(s)}'s JupyterLab…` : `Starting ${nameOf(s)}'s JupyterLab…`, { kind: "info" });
      refresh();
    } catch (e) {
      if (e.status === 404) CC.toast("Server controls are not available yet on this console version.", { kind: "info" });
      else CC.error(e);
    }
  }

  // ---------------------------------------------------------------- add student
  const add = { modalEl: $("#modal-add"), form: $("#form-add"), name: $("#add-name"), username: $("#add-username"), password: $("#add-password"),
    useSuggestion: $("#add-use-suggestion"), status: $("#add-username-status"), another: $("#add-another"), touched: false, suggestion: "", timer: null, seq: 0 };
  CC.passwordField($("#add-password-field"));
  function setStatus(text, ok) { add.status.textContent = text; add.status.className = `cc-field-status${text ? (ok ? " is-ok" : " is-bad") : ""}`; }
  function validateUsername() {
    const v = add.username.value.trim().toLowerCase();
    if (!v) return setStatus("");
    if (RESERVED.has(v)) return setStatus(`${v} is reserved`, false);
    if (!USERNAME_RE.test(v)) return setStatus("Lowercase letters, numbers, - and _ only, starting with a letter (max 32).", false);
    if (findStudent(v)) return setStatus("Already taken", false);
    setStatus("Available", true);
  }
  function resetAddForm() {
    add.form.reset(); add.touched = false; add.suggestion = ""; add.useSuggestion.hidden = true; setStatus("");
    ["add-name", "add-username", "add-password"].forEach((id) => fieldError(id, ""));
    fillPassword(add.password);
  }
  function openAdd() { resetAddForm(); openModal("modal-add", () => add.name.focus()); }
  add.name.addEventListener("input", () => { clearTimeout(add.timer); add.timer = setTimeout(suggestNow, 250); });
  async function suggestNow() {
    const name = add.name.value.trim(); const seq = ++add.seq; let suggestion = "";
    if (name) { try { suggestion = (await CC.api("GET", `students/suggest-username?name=${encodeURIComponent(name)}`)).username; } catch (e) { suggestion = localSuggest(name); } }
    if (seq !== add.seq) return;
    add.suggestion = suggestion;
    if (!add.touched) { add.username.value = suggestion; validateUsername(); }
    else add.useSuggestion.hidden = !suggestion || suggestion === add.username.value;
  }
  add.username.addEventListener("input", () => { add.touched = add.username.value !== add.suggestion; add.useSuggestion.hidden = !add.touched || !add.suggestion; fieldError("add-username", ""); validateUsername(); });
  add.useSuggestion.addEventListener("click", (e) => { e.preventDefault(); add.username.value = add.suggestion; add.touched = false; add.useSuggestion.hidden = true; validateUsername(); add.username.focus(); });
  add.form.addEventListener("submit", async (e) => {
    e.preventDefault();
    ["add-name", "add-username", "add-password"].forEach((id) => fieldError(id, ""));
    const body = { display_name: add.name.value.trim(), username: add.username.value.trim(), password: add.password.value };
    if (!body.display_name) { fieldError("add-name", "Enter the student's name."); add.name.focus(); return; }
    setBusy(add.modalEl, true);
    try {
      const r = await CC.api("POST", "students", body);
      const s = r.student; if (!state.reveal) delete s.password;
      state.students = [s, ...state.students.filter((x) => x.username !== s.username)];
      render();
      const tr = rowOf(s.username); if (tr) { tr.classList.add("is-new"); tr.scrollIntoView({ block: "nearest" }); }
      (r.warnings || []).forEach((w) => CC.toast(w, { kind: "info", sticky: true }));
      CC.toast(`${nameOf(s)} added.`, { kind: "success", action: { label: "Print card", onClick: () => openPrint([s.username]) } });
      setBusy(add.modalEl, false);
      if (add.another.checked) { resetAddForm(); add.another.checked = true; add.name.focus(); }
      else modal("modal-add").hide();
    } catch (err) {
      if (err.status === 409 || err.code === "invalid_username") { setStatus(""); fieldError("add-username", err.message); add.username.focus(); }
      else if (err.code === "invalid_password") { fieldError("add-password", err.message); add.password.focus(); }
      else if (err.code === "invalid_name") { fieldError("add-name", err.message); add.name.focus(); }
      else CC.error(err);
    } finally { setBusy(add.modalEl, false); }
  });

  // ---------------------------------------------------------------- bulk add
  const bulk = { modalEl: $("#modal-bulk"), text: $("#bulk-text"), file: $("#bulk-file"), error: $("#bulk-error"), summary: $("#bulk-summary"),
    tbody: $("#bulk-preview tbody"), conflict: $("#bulk-conflict"), back: $("#bulk-back"), close: $("#bulk-close"), recheck: $("#bulk-recheck"),
    next: $("#bulk-next"), print: $("#bulk-print"), printLabel: $("#bulk-print-label"), doneTitle: $("#bulk-done-title"), doneText: $("#bulk-done-text"),
    doneErrors: $("#bulk-done-errors"), step: 1, rows: [] };
  function bulkStep(n) {
    bulk.step = n;
    $$("[data-bulk-step]", bulk.modalEl).forEach((d) => { d.hidden = Number(d.dataset.bulkStep) !== n; });
    $$(".cc-steps li", bulk.modalEl).forEach((li) => { const k = Number(li.dataset.step); li.classList.toggle("is-active", k === n); li.classList.toggle("is-done", k < n); });
    bulk.back.hidden = n !== 2; bulk.recheck.hidden = n !== 2; bulk.next.hidden = n === 3;
    bulk.close.textContent = n === 3 ? "Close" : "Cancel";
    if (n === 1) { bulk.next.disabled = false; bulk.next.textContent = "Check list"; }
    if (n === 2) updateBulkButton();
  }
  function showBulkError(msg) { bulk.error.textContent = msg; bulk.error.hidden = false; }
  function openBulk() {
    bulk.text.value = ""; bulk.file.value = ""; bulk.error.hidden = true; bulk.rows = []; bulk.tbody.innerHTML = ""; $("#bulk-skip").checked = true;
    bulkStep(1); openModal("modal-bulk", () => bulk.text.focus());
  }
  // A chosen file wins over pasted text; typing again after choosing a file drops the file.
  bulk.file.addEventListener("change", () => { bulk.error.hidden = true; });
  bulk.text.addEventListener("input", () => { bulk.error.hidden = true; if (bulk.file.files && bulk.file.files.length) bulk.file.value = ""; });
  bulk.next.addEventListener("click", () => { if (bulk.step === 1) preview(); else if (bulk.step === 2) commit(); });
  bulk.back.addEventListener("click", () => bulkStep(1));
  bulk.recheck.addEventListener("click", () => preview(rowsToText()));
  $$('input[name="on_conflict"]').forEach((r) => r.addEventListener("change", updateBulkButton));
  function rowsToText() { return bulk.rows.map((r) => `${(r.display_name || "").replace(/,/g, " ")}, ${r.username || ""}, ${r.password || ""}`).join("\n"); }
  async function preview(textOverride) {
    bulk.error.hidden = true;
    let body;
    if (textOverride !== undefined) body = { text: textOverride };
    else if (bulk.file.files && bulk.file.files[0]) {
      const f = bulk.file.files[0];
      if (f.size > 1024 * 1024) { showBulkError("That file is larger than 1 MB. A class list should be a small text file."); return; }
      body = new FormData(); body.append("file", f, f.name);
    } else {
      const t = bulk.text.value.trim();
      if (!t) { showBulkError("Paste at least one name, or choose a file."); bulk.text.focus(); return; }
      body = { text: t };
    }
    setBusy(bulk.modalEl, true);
    try { const r = await CC.api("POST", "students/bulk/preview", body); bulk.rows = r.rows || []; renderPreview(); bulkStep(2); }
    catch (e) { if (bulk.step === 1) showBulkError(e.message); else CC.error(e); }
    finally { setBusy(bulk.modalEl, false); if (bulk.step === 2) updateBulkButton(); }
  }
  function previewCell(row, i, key, editable, mono) {
    const td = document.createElement("td");
    if (editable) {
      const input = document.createElement("input");
      input.className = `form-control form-control-sm${mono ? " cc-mono" : ""}`; input.value = row[key] || ""; input.autocomplete = "off"; input.spellcheck = false;
      input.setAttribute("aria-label", `${key.replace("_", " ")} for line ${row.line}`);
      input.addEventListener("input", () => { bulk.rows[i][key] = input.value; });
      td.appendChild(input);
    } else {
      const node = document.createElement(mono ? "code" : "span"); if (mono) node.className = "cc-mono"; node.textContent = row[key] || ""; td.appendChild(node);
    }
    return td;
  }
  function renderPreview() {
    bulk.tbody.innerHTML = "";
    bulk.rows.forEach((row, i) => {
      const tr = document.createElement("tr");
      tr.className = row.result === "exists" ? "is-muted" : row.result === "invalid" ? "is-invalid-row" : "";
      const editable = row.result === "invalid";
      tr.append(previewCell(row, i, "display_name", editable, false), previewCell(row, i, "username", editable, true), previewCell(row, i, "password", editable, true));
      const res = document.createElement("td"); res.className = `cc-result cc-result--${row.result}`;
      res.textContent = row.result === "new" ? "New" : row.result === "exists" ? `Already exists — ${$("#bulk-reset").checked ? "password will be reset" : "skipped"}` : `Fix: ${row.reason}`;
      if (row.result === "exists" && row.reason) res.title = row.reason;
      tr.appendChild(res);
      const rmTd = document.createElement("td"); rmTd.className = "text-end";
      const rm = document.createElement("button"); rm.type = "button"; rm.className = "btn btn-sm btn-link text-danger p-0"; rm.title = "Remove this line";
      rm.setAttribute("aria-label", `Remove line ${row.line}`); rm.innerHTML = '<i class="fa fa-xmark" aria-hidden="true"></i>';
      rm.addEventListener("click", () => { bulk.rows.splice(i, 1); renderPreview(); });
      rmTd.appendChild(rm); tr.appendChild(rmTd);
      bulk.tbody.appendChild(tr);
    });
    const c = { new: 0, exists: 0, invalid: 0 }; bulk.rows.forEach((r) => { c[r.result] = (c[r.result] || 0) + 1; });
    bulk.summary.innerHTML = "";
    const chip = (text, kind) => { const s = document.createElement("span"); s.className = `cc-chip${kind ? ` cc-chip--${kind}` : ""}`; s.textContent = text; bulk.summary.appendChild(s); };
    chip(`${c.new} new`, "new"); if (c.exists) chip(`${c.exists} already exist${c.exists === 1 ? "s" : ""}`); if (c.invalid) chip(`${c.invalid} need${c.invalid === 1 ? "s" : ""} a fix`, "fix");
    bulk.conflict.hidden = !c.exists;
    updateBulkButton();
  }
  function updateBulkButton() {
    if (bulk.step !== 2) return;
    const invalid = bulk.rows.filter((r) => r.result === "invalid").length;
    const n = bulk.rows.filter((r) => r.result === "new").length;
    const upd = $("#bulk-reset").checked ? bulk.rows.filter((r) => r.result === "exists").length : 0;
    $$("#bulk-preview .cc-result--exists").forEach((td) => { td.textContent = `Already exists — ${upd ? "password will be reset" : "skipped"}`; });
    bulk.next.disabled = invalid > 0 || n + upd === 0;
    bulk.next.textContent = invalid ? "Fix the marked lines, then Check again" : n + upd ? `Add ${plural(n, "student")}${upd ? ` and update ${upd}` : ""}` : "Nothing to add";
  }
  async function commit() {
    const on_conflict = $("#bulk-reset").checked ? "reset_password" : "skip";
    const rows = bulk.rows.filter((r) => r.result !== "invalid").map((r) => ({ display_name: r.display_name, username: r.username, password: r.password, line: r.line }));
    setBusy(bulk.modalEl, true);
    try {
      const r = await CC.api("POST", "students/bulk", { rows, on_conflict });
      const n = r.added.length, u = r.updated.length;
      bulk.doneTitle.textContent = n ? `${plural(n, "student")} added.` : u ? `${plural(u, "password")} updated.` : "Nothing was added.";
      const bits = [];
      if (n && u) bits.push(`${u} updated`);
      if (r.skipped.length) bits.push(`${r.skipped.length} skipped (already existed)`);
      if (r.errors.length) bits.push(`${r.errors.length} could not be added`);
      if (r.hub_failures.length) bits.push(`${r.hub_failures.length} not yet registered with JupyterHub — use Repair`);
      bulk.doneText.textContent = bits.join(" · ");
      bulk.doneErrors.innerHTML = "";
      r.errors.forEach((e) => { const li = document.createElement("li"); li.textContent = `${e.line ? `Line ${e.line}: ` : ""}${e.username ? `${e.username} — ` : ""}${e.reason}`; bulk.doneErrors.appendChild(li); });
      bulk.doneErrors.hidden = !r.errors.length;
      const printable = r.added.concat(r.updated);
      bulk.print.hidden = !printable.length; bulk.print.href = printUrl(printable); bulk.printLabel.textContent = `Print login cards for these ${printable.length}`;
      bulkStep(3); refresh();
    } catch (e) { CC.error(e); }
    finally { setBusy(bulk.modalEl, false); }
  }

  // ---------------------------------------------------------------- reset password
  const reset = { modalEl: $("#modal-reset"), form: $("#form-reset"), title: $("#modal-reset-title"), pw: $("#reset-password"), note: $("#reset-note"), user: null };
  CC.passwordField($("#reset-password-field"));
  function openReset(s) {
    reset.user = s.username; reset.title.textContent = `New password for ${nameOf(s)}`;
    reset.note.textContent = `Takes effect immediately. If ${nameOf(s)} is signed in right now they stay signed in until they log out.`;
    fieldError("reset-password", ""); reset.pw.value = ""; fillPassword(reset.pw);
    openModal("modal-reset", () => reset.pw.focus());
  }
  reset.form.addEventListener("submit", async (e) => {
    e.preventDefault(); fieldError("reset-password", "");
    const s = findStudent(reset.user);
    setBusy(reset.modalEl, true);
    try {
      const r = await CC.api("POST", `students/${encodeURIComponent(reset.user)}/password`, { password: reset.pw.value || null });
      if (s && state.reveal) { s.password = r.password; render(); }
      setBusy(reset.modalEl, false);
      modal("modal-reset").hide();
      CC.toast(`Password updated for ${s ? nameOf(s) : r.username}.`, { kind: "success", action: { label: "Print card", onClick: () => openPrint([r.username]) } });
    } catch (err) {
      if (err.code === "invalid_password") { fieldError("reset-password", err.message); reset.pw.focus(); } else CC.error(err);
    } finally { setBusy(reset.modalEl, false); }
  });

  // ---------------------------------------------------------------- remove student
  const remove = { modalEl: $("#modal-remove"), title: $("#modal-remove-title"), text: $("#remove-text"), files: $("#remove-files"), submit: $("#remove-submit"), cancel: $("#remove-cancel"), user: null };
  const removeMode = () => ($('input[name="remove_home"]:checked') || {}).value || "archive";
  function updateRemoveButton() {
    const mode = removeMode();
    remove.submit.innerHTML = `<i class="fa fa-user-minus" aria-hidden="true"></i> ${mode === "delete" ? "Delete files and remove student" : "Remove student"}`;
    remove.files.hidden = mode !== "archive";
  }
  $$('input[name="remove_home"]').forEach((r) => r.addEventListener("change", updateRemoveButton));
  function openRemove(s) {
    remove.user = s.username; const n = nameOf(s);
    remove.title.textContent = `Remove ${n}?`; remove.text.textContent = `${n} will no longer be able to sign in.`;
    $("#remove-archive").checked = true; updateRemoveButton();
    openModal("modal-remove", () => remove.cancel.focus());
  }
  remove.submit.addEventListener("click", async () => {
    const u = remove.user; const s = findStudent(u); const n = s ? nameOf(s) : u; const home = removeMode();
    setBusy(remove.modalEl, true);
    try {
      const r = await CC.api("DELETE", `students/${encodeURIComponent(u)}?home=${home}`);
      setBusy(remove.modalEl, false);
      modal("modal-remove").hide();
      const tr = rowOf(u);
      const drop = () => { state.students = state.students.filter((x) => x.username !== u); state.selected.delete(u); render(); };
      if (tr) { tr.classList.add("is-leaving"); setTimeout(drop, 300); } else drop();
      const msg = r.home === "archived" ? `${n} removed. Files archived (${r.archived_to}).` : r.home === "deleted" ? `${n} removed. Files deleted.` : `${n} removed. Files kept on the server.`;
      CC.toast(msg, { kind: "success" });
    } catch (err) {
      if (err.code === "server_stopping") CC.toast(`${err.message}`, { kind: "error" }); else CC.error(err);
    } finally { setBusy(remove.modalEl, false); }
  });

  // ---------------------------------------------------------------- repair
  async function runRepair(btn) {
    if (btn) btn.disabled = true;
    try {
      const r = await CC.api("POST", "students/repair", {});
      const bits = [];
      if (r.linux_created.length) bits.push(`${plural(r.linux_created.length, "Linux account")} created`);
      if (r.hub_created.length) bits.push(`${r.hub_created.length} registered with JupyterHub`);
      bits.push(`${plural(r.passwords_reapplied, "password")} re-applied`);
      if (r.hub_orphans.length) bits.push(`known to JupyterHub but not in the class list: ${r.hub_orphans.join(", ")}`);
      if (r.linux_orphans.length) bits.push(`on the server but not in the class list: ${r.linux_orphans.join(", ")}`);
      CC.toast(`Repair finished: ${bits.join("; ")}.`, { kind: r.errors.length ? "info" : "success", sticky: true });
      r.errors.forEach((msg) => CC.toast(msg, { kind: "error" }));
      refresh();
    } catch (e) { CC.error(e); }
    finally { if (btn) btn.disabled = false; }
  }
  $("#btn-repair").addEventListener("click", () => runRepair($("#btn-repair")));
  $("#btn-repair-menu").addEventListener("click", () => runRepair($("#btn-repair-menu")));

  // ---------------------------------------------------------------- import users.txt (the seed file on the server)
  async function importUsersTxt() {
    const body = document.createElement("div");
    body.innerHTML = '<p class="mb-2">Accounts listed in the server\'s <code>users.txt</code> that are not in the class list yet will be added with the passwords from that file. Accounts that already exist are left as they are.</p>'
      + '<div class="form-check"><input class="form-check-input" type="checkbox" id="import-reset">'
      + '<label class="form-check-label" for="import-reset">Also reset the passwords of accounts that already exist to the ones in users.txt</label></div>';
    const ok = await CC.confirm({ title: "Import users.txt?", body, confirmLabel: "Import", danger: false });
    if (!ok) return;
    const on_conflict = body.querySelector("#import-reset").checked ? "reset_password" : "skip";
    try {
      const r = await CC.api("POST", "students/import-users-txt", { on_conflict });
      const n = r.added.length, u = r.updated.length;
      const bits = [];
      if (n) bits.push(`${plural(n, "account")} added`);
      if (u) bits.push(`${plural(u, "password")} updated`);
      if (r.skipped.length) bits.push(`${r.skipped.length} already existed`);
      if (r.errors.length) bits.push(`${plural(r.errors.length, "line")} could not be used`);
      if (r.hub_failures.length) bits.push(`${r.hub_failures.length} not yet registered with JupyterHub — use Repair`);
      const printable = r.added.concat(r.updated);
      CC.toast(`Import of ${r.file || "users.txt"} finished: ${bits.length ? bits.join(" · ") : "nothing to do"}.`,
        { kind: r.errors.length ? "info" : "success", sticky: true, action: printable.length ? { label: "Print cards", onClick: () => openPrint(printable) } : null });
      r.errors.slice(0, 5).forEach((e) => CC.toast(`${e.line ? `Line ${e.line}: ` : ""}${e.username ? `${e.username} — ` : ""}${e.reason}`, { kind: "error" }));
      refresh();
    } catch (e) {
      if (e.code === "users_txt_missing") CC.toast(e.message, { kind: "info" }); else CC.error(e);
    }
  }
  $("#btn-import").addEventListener("click", importUsersTxt);

  // ---------------------------------------------------------------- openers + start
  document.addEventListener("click", (e) => {
    const b = e.target.closest("[data-open]"); if (!b) return;
    if (b.dataset.open === "add") openAdd(); else if (b.dataset.open === "bulk") openBulk();
  });
  poller = CC.poll(async (signal) => {
    try { await load(signal); }
    catch (e) { if (e.name !== "AbortError" && !state.loaded) showLoadError(e); throw e; }
  }, POLL_MS);
})();
