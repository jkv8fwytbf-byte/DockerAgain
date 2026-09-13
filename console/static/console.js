/* Teacher console — shared behaviour. Vanilla JS, no build step.
   Feature pages add their own <page>.js and use these helpers via window.CC. */
(function () {
  "use strict";
  const meta = (n) => (document.querySelector(`meta[name="${n}"]`) || {}).content || "";
  const CC = {
    prefix: meta("console-prefix") || document.body.dataset.prefix || "",
    page: meta("console-page") || document.body.dataset.page || "",
    user: meta("console-user") || "",
  };

  // --- fetch wrapper ------------------------------------------------------
  CC.api = async function (method, path, body, opts = {}) {
    const url = path.startsWith("/") ? path : `${CC.prefix}/api/${path}`;
    const headers = { "X-Console-Request": "1", Accept: "application/json" };
    let payload;
    if (body instanceof FormData) payload = body;
    else if (body !== undefined && body !== null) { headers["Content-Type"] = "application/json"; payload = JSON.stringify(body); }
    let res;
    try {
      res = await fetch(url, { method, headers, body: payload, credentials: "same-origin", signal: opts.signal });
    } catch (e) {
      if (e.name === "AbortError") throw e;
      throw Object.assign(new Error("Can't reach the server right now."), { code: "network", status: 0 });
    }
    if (res.status === 401) { window.location.href = `${CC.prefix}/login?next=${encodeURIComponent(location.pathname + location.search)}`; throw Object.assign(new Error("Please sign in again."), { code: "not_authenticated", status: 401 }); }
    const type = res.headers.get("content-type") || "";
    const data = type.includes("application/json") ? await res.json().catch(() => ({})) : await res.text();
    if (!res.ok) {
      const err = new Error((data && data.message) || `Request failed (${res.status})`);
      err.code = (data && data.error) || "http_error"; err.status = res.status; err.detail = data && data.detail;
      throw err;
    }
    return data;
  };

  // --- toasts -------------------------------------------------------------
  CC.toast = function (message, { kind = "info", action = null, sticky = false } = {}) {
    const box = document.querySelector(".cc-toasts");
    if (!box) return;
    while (box.children.length >= 3) box.firstElementChild.remove();
    const el = document.createElement("div");
    el.className = `toast is-${kind}`; el.setAttribute("role", "status");
    const icon = { success: "fa-circle-check", error: "fa-triangle-exclamation", info: "fa-circle-info" }[kind] || "fa-circle-info";
    el.innerHTML = `<div class="d-flex"><div class="toast-body"><i class="fa ${icon} me-2" aria-hidden="true"></i><span class="cc-toast-text"></span> ${action ? `<a href="#" class="cc-toast-action ms-2 fw-semibold"></a>` : ""}</div><button type="button" class="btn-close me-2 m-auto" data-bs-dismiss="toast" aria-label="Close"></button></div>`;
    el.querySelector(".cc-toast-text").textContent = message;
    if (action) { const a = el.querySelector(".cc-toast-action"); a.textContent = action.label; if (action.href) a.href = action.href; a.addEventListener("click", (e) => { if (action.onClick) { e.preventDefault(); action.onClick(); } }); }
    box.appendChild(el);
    const t = new bootstrap.Toast(el, { autohide: !sticky && kind !== "error", delay: 5000 });
    el.addEventListener("hidden.bs.toast", () => el.remove());
    t.show();
  };
  CC.error = (e) => CC.toast(e && e.message ? e.message : String(e), { kind: "error" });

  // --- confirm dialog -----------------------------------------------------
  CC.confirm = function ({ title = "Are you sure?", body = "", confirmLabel = "Confirm", danger = true } = {}) {
    const modalEl = document.getElementById("cc-confirm");
    const modal = bootstrap.Modal.getOrCreateInstance(modalEl);
    modalEl.querySelector("#cc-confirm-title").textContent = title;
    const bodyEl = modalEl.querySelector("#cc-confirm-body");
    if (body instanceof Node) { bodyEl.replaceChildren(body); } else { bodyEl.textContent = ""; body.split("\n").forEach((line, i) => { if (i) bodyEl.appendChild(document.createElement("br")); bodyEl.appendChild(document.createTextNode(line)); }); }
    const ok = modalEl.querySelector("#cc-confirm-ok");
    ok.textContent = confirmLabel; ok.className = `btn ${danger ? "btn-danger" : "btn-primary"}`;
    return new Promise((resolve) => {
      let done = false;
      const finish = (v) => { if (done) return; done = true; resolve(v); ok.removeEventListener("click", onOk); modalEl.removeEventListener("hidden.bs.modal", onHide); modal.hide(); };
      const onOk = () => finish(true); const onHide = () => finish(false);
      ok.addEventListener("click", onOk); modalEl.addEventListener("hidden.bs.modal", onHide);
      modalEl.addEventListener("shown.bs.modal", () => modalEl.querySelector("#cc-confirm-cancel").focus(), { once: true });
      modal.show();
    });
  };

  // --- polling ------------------------------------------------------------
  CC.poll = function (fn, ms) {
    let timer = null, failures = 0, ctrl = null, stopped = false;
    const run = async () => {
      if (stopped) return;
      if (document.hidden) { timer = setTimeout(run, ms); return; }
      if (ctrl) ctrl.abort();
      ctrl = new AbortController();
      try { await fn(ctrl.signal); failures = 0; } catch (e) { if (e.name !== "AbortError") failures += 1; }
      const delay = failures ? Math.min(ms * 2 ** failures, 30000) : ms;
      timer = setTimeout(run, delay);
    };
    document.addEventListener("visibilitychange", () => { if (!document.hidden && !stopped) { clearTimeout(timer); run(); } });
    run();
    return { stop() { stopped = true; clearTimeout(timer); if (ctrl) ctrl.abort(); }, failures: () => failures, now() { clearTimeout(timer); run(); } };
  };

  // --- tables: sort + search ----------------------------------------------
  CC.table = function (table) {
    if (!table) return null;
    const tbody = table.tBodies[0];
    const state = { key: table.dataset.sortKey || null, dir: table.dataset.sortDir || "asc", q: "" };
    const val = (tr, key) => { const td = tr.querySelector(`[data-key="${key}"]`); return td ? (td.dataset.value ?? td.textContent.trim()) : ""; };
    const apply = () => {
      const rows = Array.from(tbody.querySelectorAll("tr[data-search]"));
      const q = state.q.toLowerCase();
      rows.forEach((tr) => { tr.hidden = q && !tr.dataset.search.toLowerCase().includes(q); });
      if (state.key) {
        const sorted = rows.slice().sort((a, b) => { const x = val(a, state.key), y = val(b, state.key); const n = Number(x), m = Number(y); const c = (x.trim() && y.trim() && Number.isFinite(n) && Number.isFinite(m)) ? n - m : x.localeCompare(y, undefined, { numeric: true, sensitivity: "base" }); return state.dir === "asc" ? c : -c; });
        sorted.forEach((tr) => tbody.appendChild(tr));
      }
      table.querySelectorAll("th[data-sort]").forEach((th) => th.setAttribute("aria-sort", th.dataset.sort === state.key ? (state.dir === "asc" ? "ascending" : "descending") : "none"));
      const empty = table.parentElement.querySelector("[data-empty-search]");
      if (empty) empty.hidden = !(q && rows.every((r) => r.hidden));
    };
    table.querySelectorAll("th[data-sort]").forEach((th) => {
      const btn = th.querySelector("button") || th;
      btn.addEventListener("click", () => { if (state.key === th.dataset.sort) state.dir = state.dir === "asc" ? "desc" : "asc"; else { state.key = th.dataset.sort; state.dir = "asc"; } apply(); });
    });
    const search = document.querySelector(`[data-search-for="${table.id}"]`);
    if (search) { let t; search.addEventListener("input", () => { clearTimeout(t); t = setTimeout(() => { state.q = search.value; apply(); }, 150); }); }
    apply();
    return { apply, state };
  };

  // --- copy to clipboard (works on plain HTTP too) ------------------------
  CC.copy = async function (text, btn) {
    let ok = false;
    try { if (navigator.clipboard && window.isSecureContext) { await navigator.clipboard.writeText(text); ok = true; } } catch (e) { ok = false; }
    if (!ok) {
      const ta = document.createElement("textarea"); ta.value = text; ta.setAttribute("readonly", ""); ta.style.position = "fixed"; ta.style.left = "-9999px";
      document.body.appendChild(ta); ta.select(); try { ok = document.execCommand("copy"); } catch (e) { ok = false; } ta.remove();
    }
    if (btn) { const icon = btn.querySelector(".fa"); btn.classList.add("is-done"); if (icon) { icon.classList.replace("fa-copy", "fa-check"); } setTimeout(() => { btn.classList.remove("is-done"); if (icon) icon.classList.replace("fa-check", "fa-copy"); }, 1500); }
    if (!ok) CC.toast("Copy failed — select the text and copy it by hand.", { kind: "error" });
    return ok;
  };
  document.addEventListener("click", (e) => { const b = e.target.closest("button.cc-copy[data-copy]"); if (b) { e.preventDefault(); CC.copy(b.dataset.copy, b); } });

  // --- formatting ---------------------------------------------------------
  CC.bytes = function (n) { n = Number(n) || 0; const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0; while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; } return `${i ? n.toFixed(1) : n} ${u[i]}`; };
  CC.pct = (n) => `${Math.round(Number(n) || 0)} %`;
  CC.ago = function (iso) {
    if (!iso) return "Never";
    const d = new Date(iso); const s = Math.max(0, (Date.now() - d.getTime()) / 1000);
    if (s < 45) return "just now"; if (s < 3600) return `${Math.round(s / 60)} min ago`;
    if (s < 86400) return `${Math.round(s / 3600)} h ago`;
    return d.toLocaleDateString(undefined, { month: "short", day: "numeric" }) + " " + d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  };
  CC.refreshTimes = function (root = document) { root.querySelectorAll("time[datetime]").forEach((t) => { t.textContent = CC.ago(t.getAttribute("datetime")); t.title = new Date(t.getAttribute("datetime")).toLocaleString(); }); };
  CC.setText = function (el, text) { if (el && el.textContent !== String(text)) el.textContent = text; };
  CC.meterClass = (pct, warn = 75, danger = 90) => pct >= danger ? "is-danger" : pct >= warn ? "is-warn" : "";
  CC.setMeter = function (el, pct, warn, danger) { if (!el) return; el.style.width = `${Math.min(100, Math.max(0, pct))}%`; el.className = CC.meterClass(pct, warn, danger); };

  // --- sidebar health (shared by every page) ------------------------------
  CC.updateHealth = function (host) {
    if (!host) return;
    const cpu = host.cpu_pct || 0, mem = host.mem_total ? (host.mem_used / host.mem_total) * 100 : 0;
    const diskUsed = host.disk_total ? ((host.disk_total - host.disk_free) / host.disk_total) * 100 : 0;
    CC.setMeter(document.querySelector('[data-meter="cpu"]'), cpu); CC.setText(document.querySelector('[data-health="cpu"]'), CC.pct(cpu));
    CC.setMeter(document.querySelector('[data-meter="mem"]'), mem); CC.setText(document.querySelector('[data-health="mem"]'), CC.bytes(host.mem_used));
    CC.setMeter(document.querySelector('[data-meter="disk"]'), diskUsed, 85, 95); CC.setText(document.querySelector('[data-health="disk"]'), `${CC.bytes(host.disk_free)} free`);
  };
  CC.setBadge = function (id, value) { const b = document.querySelector(`[data-badge="${id}"]`); if (!b) return; if (value === null || value === undefined || value === "") { b.hidden = true; return; } b.hidden = false; CC.setText(b, value); };

  // --- password field helper ----------------------------------------------
  CC.passwordField = function (root) {
    if (!root) return;
    const input = root.querySelector("input"); const eye = root.querySelector("[data-eye]"); const regen = root.querySelector("[data-regenerate]"); const copy = root.querySelector("[data-copy-password]");
    if (eye) eye.addEventListener("click", () => { const show = input.type === "password"; input.type = show ? "text" : "password"; eye.setAttribute("aria-pressed", String(show)); });
    if (regen) regen.addEventListener("click", async () => { try { const r = await CC.api("GET", "students/generate-password"); input.value = r.password; input.type = "text"; input.dispatchEvent(new Event("input")); } catch (e) { CC.error(e); } });
    if (copy) copy.addEventListener("click", () => CC.copy(input.value, copy));
  };

  // --- init ---------------------------------------------------------------
  CC.init = function () {
    document.querySelectorAll('[data-bs-toggle="tooltip"]').forEach((el) => new bootstrap.Tooltip(el));
    CC.refreshTimes();
    if (CC.page !== "dashboard" && CC.user) {
      CC.poll(async (signal) => { const s = await CC.api("GET", "status", null, { signal }); CC.updateHealth(s.host); CC.setBadge("dashboard", s.students ? s.students.online : null); }, 30000);
    }
  };
  window.CC = CC;
  document.addEventListener("DOMContentLoaded", CC.init);
})();
