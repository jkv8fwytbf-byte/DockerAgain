/* Settings page: branding form with live preview, immediate logo upload,
   dirty tracking with a sticky save bar, and the "About this server" block. */
(function () {
  "use strict";
  const form = document.getElementById("settings");
  if (!form || !window.CC) return;
  const $ = (id) => document.getElementById(id);
  const FIELDS = ["school_name", "class_name", "accent", "announcement", "class_url"];
  const ACCENTS = { orange: "#f37524", blue: "#2c7bb6", green: "#1a8f4e", purple: "#6f42c1", teal: "#0d8a8a", slate: "#495057" };
  const SVG_MESSAGE = "Please upload a PNG or JPEG (SVG cannot be shown by the login page).";
  const el = {
    school: $("school_name"), klass: $("class_name"), announcement: $("announcement"), url: $("class_url"),
    counter: $("announcement-count"), accentName: $("accent-name"), effective: $("effective-url"),
    footer: $("settings-footer"), save: $("save"), discard: $("discard"), detect: $("detect-url"),
    logoLight: $("logo-light"), logoDark: $("logo-dark"), logoFile: $("logo-file"), logoUpload: $("logo-upload"),
    logoRemove: $("logo-remove"), logoState: $("logo-state"), logoDrop: $("logo-drop"),
    preview: $("preview"), pvLogo: $("pv-logo"), pvSchool: $("pv-school"), pvClass: $("pv-class"),
    pvAnnouncement: $("pv-announcement"), pvAnnouncementText: $("pv-announcement-text"),
    copyDiag: $("copy-diagnostics"), topbarName: document.querySelector(".cc-brand__name"), topbarLogo: document.querySelector(".cc-brand__logo"),
  };

  let saved = null;            // last state confirmed by the server (the five text fields)
  let lastEdited = null;       // the field the teacher touched last; focus goes back there after a save
  let payload = null;          // last full GET/PUT payload (about, logo, limits)
  let detected = location.origin + "/";
  let saving = false, logoBusy = false;

  // --- form <-> state --------------------------------------------------------
  const accentInputs = () => Array.from(form.querySelectorAll('input[name="accent"]'));
  function readForm() {
    const checked = accentInputs().find((r) => r.checked);
    return {
      school_name: el.school.value, class_name: el.klass.value, accent: checked ? checked.value : "orange",
      announcement: el.announcement.value, class_url: el.url.value,
    };
  }
  function fillForm(v) {
    el.school.value = v.school_name || ""; el.klass.value = v.class_name || "";
    el.announcement.value = v.announcement || ""; el.url.value = v.class_url || "";
    accentInputs().forEach((r) => { r.checked = r.value === (v.accent || "orange"); });
  }
  function isDirty() { if (!saved) return false; const cur = readForm(); return FIELDS.some((k) => (cur[k] || "") !== (saved[k] || "")); }
  function updateDirty() {
    const dirty = isDirty();
    if (el.footer.hidden !== !dirty) el.footer.hidden = !dirty;
    document.body.classList.toggle("cc-has-unsaved", dirty);
  }
  function updateCounter() {
    const n = el.announcement.value.length, max = Number(el.announcement.maxLength) || 300;
    CC.setText(el.counter, `${n} / ${max}`);
    el.counter.classList.toggle("is-near", n >= max - 30 && n < max);
    el.counter.classList.toggle("is-full", n >= max);
  }
  function updatePreview() {
    const v = readForm();
    const hex = ACCENTS[v.accent] || ACCENTS.orange;
    el.preview.style.setProperty("--cc-accent", hex);
    CC.setText(el.pvSchool, v.school_name.trim() || "Classroom JupyterHub");
    const klass = v.class_name.trim(); el.pvClass.hidden = !klass; CC.setText(el.pvClass, klass);
    const ann = v.announcement.trim(); el.pvAnnouncement.hidden = !ann; CC.setText(el.pvAnnouncementText, ann);
    CC.setText(el.accentName, v.accent.charAt(0).toUpperCase() + v.accent.slice(1));
    accentInputs().forEach((r) => { const sw = r.closest(".cc-swatch"); if (sw) sw.classList.toggle("is-selected", r.checked); });
  }

  // --- validation messages ------------------------------------------------------
  function fieldInput(name) { return name === "accent" ? null : form.elements[name]; }
  function setFieldError(name, msg) {
    const input = fieldInput(name); const box = form.querySelector(`[data-error-for="${name}"]`);
    if (input) { input.classList.toggle("is-invalid", !!msg); if (msg) input.setAttribute("aria-invalid", "true"); else input.removeAttribute("aria-invalid"); }
    if (box) box.textContent = msg || "";
  }
  function clearErrors() { FIELDS.forEach((f) => setFieldError(f, "")); }
  function validateLocally() {
    const v = readForm(); const errors = {};
    const url = v.class_url.trim();
    if (url && !/^https?:\/\/\S+$/i.test(url)) errors.class_url = "must start with http:// or https://. Example: http://192.168.1.10:8000";
    if (url.length > 200) errors.class_url = "is too long — keep it under 200 characters.";
    if (v.school_name.trim().length > 80) errors.school_name = "is too long — keep it under 80 characters.";
    if (v.class_name.trim().length > 120) errors.class_name = "is too long — keep it under 120 characters.";
    if (v.announcement.trim().length > 300) errors.announcement = "is too long — keep it under 300 characters.";
    return errors;
  }
  function showErrors(errors) {
    clearErrors();
    const names = Object.keys(errors || {});
    names.forEach((n) => setFieldError(n, errors[n]));
    const first = names.map(fieldInput).find(Boolean);
    if (first) first.focus();
    return names.length > 0;
  }

  // --- apply server payload -------------------------------------------------------
  function applyLogo(logo) {
    if (!logo) return;
    const src = `${logo.url}${logo.url.includes("?") ? "&" : "?"}t=${Date.now()}`;
    [el.logoLight, el.logoDark, el.pvLogo, el.topbarLogo].forEach((img) => { if (img) img.src = src; });
    el.pvLogo.classList.toggle("is-custom", !!logo.custom);
    el.logoRemove.hidden = !logo.custom;
    CC.setText(el.logoState, logo.custom ? "Custom logo" : "Built-in logo");
    form.dataset.logoCustom = logo.custom ? "true" : "false";
  }
  function applyAbout(about) {
    if (!about) return;
    const set = (k, text) => CC.setText(form.querySelector(`[data-about="${k}"]`), text);
    set("image_version", `v${about.image_version || "dev"}`);
    set("jupyterhub", about.jupyterhub || "Not answering right now");
    set("jupyterlab", about.jupyterlab || "Unknown");
    set("python", about.python || "Unknown");
    set("addresses", (about.addresses || []).join(", ") || "None found");
    set("started", about.started ? `${new Date(about.started).toLocaleString()} (${CC.ago(about.started)})` : "Unknown");
  }
  function applyTopbar(v) {
    if (el.topbarName) CC.setText(el.topbarName, v.school_name || "Classroom");
    document.body.style.setProperty("--cc-accent", ACCENTS[v.accent] || ACCENTS.orange);
  }
  function apply(p, { keepForm = false } = {}) {
    payload = p;
    saved = {}; FIELDS.forEach((k) => { saved[k] = p[k] || ""; });
    if (!keepForm) fillForm(saved);
    applyLogo(p.logo); applyAbout(p.about);
    const maxLogo = p.limits && p.limits.max_logo_bytes;
    if (maxLogo) CC.setText(form.querySelector("[data-logo-limit]"), CC.bytes(maxLogo).replace(/\.0 /, " "));
    CC.setText(el.effective, detected);
    updateCounter(); updatePreview(); updateDirty();
  }

  // --- save / discard ---------------------------------------------------------------
  function setSaving(on) {
    // The buttons stay enabled (guarded by `saving`) so keyboard focus is not lost mid-save.
    saving = on;
    el.discard.disabled = on;
    el.save.classList.toggle("is-busy", on);
    el.save.querySelector(".spinner-border").hidden = !on;
    el.save.setAttribute("aria-busy", on ? "true" : "false");
  }
  async function save() {
    if (saving || !isDirty()) return;
    const local = validateLocally();
    if (showErrors(local)) return;
    const body = readForm();
    setSaving(true);
    try {
      const r = await CC.api("PUT", "settings", body);
      apply(r);
      applyTopbar(r);
      el.preview.classList.remove("cc-pv-flash"); void el.preview.offsetWidth; el.preview.classList.add("cc-pv-flash");
      CC.toast("Saved. Your sign-in page is up to date.", { kind: "success" });
      // The footer that held the Save button is gone now; keep keyboard focus on the page.
      if (document.activeElement === document.body || el.footer.contains(document.activeElement)) (lastEdited || el.school).focus({ preventScroll: true });
    } catch (e) {
      if (e.status === 400 && e.detail && e.detail.fields && showErrors(e.detail.fields)) CC.toast(e.message, { kind: "error" });
      else CC.error(e);
    } finally { setSaving(false); }
  }
  function discard() {
    if (!saved) return;
    fillForm(saved); clearErrors(); updateCounter(); updatePreview(); updateDirty();
    CC.toast("Changes discarded.", { kind: "info" });
    el.school.focus();
  }

  // --- logo ------------------------------------------------------------------------
  function setLogoBusy(on) {
    // Buttons stay enabled (guarded by `logoBusy`) so focus does not fall back to the page.
    logoBusy = on;
    el.logoUpload.classList.toggle("is-busy", on); el.logoRemove.classList.toggle("is-busy", on);
    el.logoUpload.querySelector(".spinner-border").hidden = !on;
    el.logoUpload.setAttribute("aria-busy", on ? "true" : "false");
    el.logoDrop.querySelectorAll(".cc-logo-chip").forEach((c) => c.classList.toggle("is-busy", on));
  }
  async function uploadLogo(file) {
    if (!file || logoBusy) return;
    const name = (file.name || "").toLowerCase();
    const max = (payload && payload.limits && payload.limits.max_logo_bytes) || 2 * 1024 * 1024;
    if (file.type === "image/svg+xml" || name.endsWith(".svg")) { CC.toast(SVG_MESSAGE, { kind: "error" }); return; }
    if (file.size > max) { CC.toast(`That image is ${CC.bytes(file.size)} — please use one smaller than ${CC.bytes(max)}.`, { kind: "error" }); return; }
    const fd = new FormData(); fd.append("file", file, file.name || "logo");
    setLogoBusy(true);
    try {
      const r = await CC.api("POST", "settings/logo", fd);
      payload = Object.assign(payload || {}, { logo: r.logo });
      applyLogo(r.logo);
      CC.toast("Logo updated. Students will see it on the sign-in page.", { kind: "success" });
    } catch (e) { CC.error(e); }
    finally { setLogoBusy(false); el.logoFile.value = ""; if (document.activeElement === document.body) el.logoUpload.focus(); }
  }
  function whenConfirmClosed() {
    // Bootstrap moves focus while the dialog fades out; wait until it is gone before restoring it.
    const modalEl = document.getElementById("cc-confirm");
    if (!modalEl || !modalEl.classList.contains("show")) return Promise.resolve();
    return new Promise((resolve) => { const t = setTimeout(resolve, 600); modalEl.addEventListener("hidden.bs.modal", () => { clearTimeout(t); resolve(); }, { once: true }); });
  }
  async function removeLogo() {
    if (logoBusy) return;
    const ok = await CC.confirm({
      title: "Remove the logo?",
      body: "The sign-in page and the top bar go back to the built-in JupyterHub logo.\nYour school name, colours and other settings stay as they are. No student files are affected.",
      confirmLabel: "Remove logo", danger: true,
    });
    await whenConfirmClosed();
    if (!ok) { el.logoRemove.focus(); return; }
    setLogoBusy(true);
    try {
      const r = await CC.api("DELETE", "settings/logo");
      payload = Object.assign(payload || {}, { logo: r.logo });
      applyLogo(r.logo);
      CC.toast("Logo removed — the built-in logo is back.", { kind: "success" });
      el.logoUpload.focus();
    } catch (e) { CC.error(e); el.logoRemove.focus(); }
    finally { setLogoBusy(false); if (document.activeElement === document.body) el.logoUpload.focus(); }
  }

  // --- diagnostics -----------------------------------------------------------------
  function diagnosticsText() {
    const a = (payload && payload.about) || {};
    const lines = [
      "Classroom JupyterHub — diagnostics",
      `Copied: ${new Date().toLocaleString()}`,
      `Classroom image: v${a.image_version || "dev"}`,
      `JupyterHub: ${a.jupyterhub || "unknown"}`,
      `JupyterLab: ${a.jupyterlab || "unknown"}`,
      `Python: ${a.python || "unknown"}`,
      `Server address(es): ${(a.addresses || []).join(", ") || "none found"}`,
      `Running since: ${a.started || "unknown"}`,
      `Class address: ${(payload && (payload.class_url || payload.class_url_effective)) || detected}`,
      `Signed in as: ${CC.user || "?"}`,
      `Console: ${location.origin}${CC.prefix}/`,
      `Browser: ${navigator.userAgent}`,
    ];
    return lines.join("\n");
  }

  // --- wiring ----------------------------------------------------------------------
  [el.school, el.klass, el.announcement, el.url].forEach((input) => {
    input.addEventListener("input", () => { lastEdited = input; setFieldError(input.name, ""); if (input === el.announcement) updateCounter(); updatePreview(); updateDirty(); });
  });
  accentInputs().forEach((r) => r.addEventListener("change", () => { lastEdited = r; setFieldError("accent", ""); updatePreview(); updateDirty(); }));
  form.addEventListener("submit", (e) => { e.preventDefault(); save(); });
  el.discard.addEventListener("click", discard);
  document.addEventListener("keydown", (e) => { if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "s") { e.preventDefault(); save(); } });
  window.addEventListener("beforeunload", (e) => { if (isDirty()) { e.preventDefault(); e.returnValue = ""; } });

  el.detect.addEventListener("click", async () => {
    el.detect.disabled = true;
    try {
      const r = await CC.api("GET", "settings/detect-url");
      detected = r.url; CC.setText(el.effective, detected);
      el.url.value = r.url.replace(/\/$/, ""); el.url.dispatchEvent(new Event("input")); el.url.focus();
      if (/^https?:\/\/(?:localhost|[^/:]*\.localhost|127\.\d+\.\d+\.\d+|0\.0\.0\.0|\[::1\])(?=[:/]|$)/i.test(r.url)) CC.toast("That address only works on this computer. Students on other computers need this computer's network address.", { kind: "info" });
    } catch (e) { CC.error(e); }
    finally { el.detect.disabled = false; }
  });

  el.logoUpload.addEventListener("click", () => el.logoFile.click());
  el.logoFile.addEventListener("change", () => uploadLogo(el.logoFile.files && el.logoFile.files[0]));
  el.logoRemove.addEventListener("click", removeLogo);
  el.logoDrop.addEventListener("click", (e) => { if (!e.target.closest("button")) el.logoFile.click(); });
  el.logoDrop.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); el.logoFile.click(); } });
  ["dragenter", "dragover"].forEach((ev) => el.logoDrop.addEventListener(ev, (e) => { e.preventDefault(); el.logoDrop.classList.add("is-over"); }));
  ["dragleave", "drop"].forEach((ev) => el.logoDrop.addEventListener(ev, (e) => { if (ev === "dragleave" && el.logoDrop.contains(e.relatedTarget)) return; el.logoDrop.classList.remove("is-over"); }));
  el.logoDrop.addEventListener("drop", (e) => { e.preventDefault(); const f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0]; uploadLogo(f); });
  document.addEventListener("dragover", (e) => { if (!el.logoDrop.contains(e.target)) e.preventDefault(); });
  document.addEventListener("drop", (e) => { if (!el.logoDrop.contains(e.target)) e.preventDefault(); });

  el.copyDiag.addEventListener("click", () => CC.copy(diagnosticsText(), el.copyDiag));

  // preview colour scheme: follows the console until the teacher picks one
  const themeRadios = Array.from(form.querySelectorAll('input[name="preview-theme"]'));
  let themePicked = false;
  const syncPreviewTheme = () => { const t = document.documentElement.getAttribute("data-bs-theme") === "dark" ? "dark" : "light"; themeRadios.forEach((r) => { r.checked = r.value === t; }); el.preview.setAttribute("data-bs-theme", t); };
  themeRadios.forEach((r) => r.addEventListener("change", () => { themePicked = true; el.preview.setAttribute("data-bs-theme", r.value); }));
  new MutationObserver(() => { if (!themePicked) syncPreviewTheme(); }).observe(document.documentElement, { attributes: true, attributeFilter: ["data-bs-theme"] });
  syncPreviewTheme();

  // --- initial load ----------------------------------------------------------------
  saved = readForm();   // server-rendered values until the API answers
  updateCounter(); updatePreview(); updateDirty();
  (async () => {
    try {
      const [s, d] = await Promise.all([CC.api("GET", "settings"), CC.api("GET", "settings/detect-url").catch(() => null)]);
      if (d && d.url) detected = d.url;
      apply(s, { keepForm: isDirty() });
    } catch (e) { CC.error(e); }
  })();
})();
