/* SPDX-License-Identifier: GPL-2.0-or-later */
"use strict";

// Mutations happen only in explicit button/form handlers. Polling reads cached state.
let controlSession = null;
let controlState = null;
let selectedControlView = "overview";
let pendingControlView = null;
let selectedDocumentIdentifier = null;
let documentSelection = [];
let controlRefreshActive = false;
let lastDocumentRenderKey = "";
let confirmationAction = null;
let lastControlCollections = {};
let lastControlFeedback = null;
const controlViews = ["overview", "print", "scan", "documents", "copy", "profiles", "settings"];
const pendingRequestStorageKey = "dcp150c.control-center.pending-requests";
const pendingControlRequests = readPendingControlRequests();
const terminalJobStates = ["succeeded", "failed", "cancelled", "interrupted", "submitted"];

function controlText(identifier, parameters = {}) {
  return translate(identifier, parameters);
}

function showControlFeedback(identifier, parameters = {}, error = false) {
  lastControlFeedback = { identifier, parameters, error };
  const feedback = findElementById("control-feedback");
  feedback.textContent = controlText(identifier, parameters);
  feedback.classList.toggle("error", error);
  feedback.hidden = false;
}

function errorTranslation(error) {
  const code = String(error?.code || "request_failed");
  const identifier = `error.${code}`;
  return dashboardLanguages.en.messages[identifier] ? identifier : "error.request_failed";
}

function canonicalRequestValue(value) {
  if (Array.isArray(value)) return value.map(canonicalRequestValue);
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.keys(value).sort().map((key) => [key, canonicalRequestValue(value[key])]));
  }
  return value;
}

function newRequestKey() {
  const randomBytes = crypto.getRandomValues(new Uint8Array(16));
  return Array.from(randomBytes, (value) => value.toString(16).padStart(2, "0")).join("");
}

function readPendingControlRequests() {
  const pending = new Map();
  try {
    const entries = JSON.parse(sessionStorage.getItem(pendingRequestStorageKey) || "[]");
    if (Array.isArray(entries)) for (const entry of entries.slice(0, 128)) {
      if (Array.isArray(entry) && entry.length === 2 && typeof entry[0] === "string" && entry[0].length <= 16384 && /^[a-f0-9]{32}$/.test(entry[1])) pending.set(entry[0], entry[1]);
    }
  } catch (_) {
    // In-memory protection still applies when the browser blocks session storage.
  }
  return pending;
}

function savePendingControlRequests() {
  try {
    const encoded = JSON.stringify([...pendingControlRequests]);
    sessionStorage.setItem(pendingRequestStorageKey, encoded);
    return sessionStorage.getItem(pendingRequestStorageKey) === encoded;
  } catch (_) { return false; }
}

function needsDurableRequestKey(method, path, body) {
  if (method !== "POST" || body instanceof Blob) return false;
  return ["/api/print", "/api/copy", "/api/scan", "/api/scanner/detect", "/api/scanner/preview", "/api/documents/merge"].includes(path)
    || /^\/api\/documents\/[a-zA-Z0-9_-]+\/(?:ocr|transform)$/.test(path)
    || path === "/api/scan-sessions" || /^\/api\/scan-sessions\/[a-f0-9]{32}\/(?:pages|finish)$/.test(path);
}

async function controlRequest(path, options = {}) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), options.timeout || 30000);
  const headers = new Headers(options.headers || {});
  const method = options.method || "GET";
  let body = options.body;
  // These identities contain only job settings and opaque document identifiers.
  // Passwords and raw upload data are excluded by the explicit job-route list.
  const requestIdentity = needsDurableRequestKey(method, path, body)
    ? JSON.stringify([method, path, canonicalRequestValue(body || {})]) : null;
  if (requestIdentity) {
    if (!pendingControlRequests.has(requestIdentity)) {
      if (pendingControlRequests.size >= 128) {
        clearTimeout(timeout);
        const error = new Error("Too many unresolved operations");
        error.code = "pending_requests_full";
        throw error;
      }
      pendingControlRequests.set(requestIdentity, newRequestKey());
    }
    if (!savePendingControlRequests()) {
      clearTimeout(timeout);
      const error = new Error("Pending operation could not be remembered");
      error.code = "request_storage_unavailable";
      throw error;
    }
    headers.set("Idempotency-Key", pendingControlRequests.get(requestIdentity));
  }
  if (body !== undefined && !(body instanceof Blob)) {
    headers.set("Content-Type", "application/json");
    body = JSON.stringify(body);
  }
  if (method !== "GET" && method !== "HEAD" && controlSession?.csrf_token) {
    headers.set("X-CSRF-Token", controlSession.csrf_token);
    if (!headers.has("Idempotency-Key")) headers.set("Idempotency-Key", newRequestKey());
  }
  try {
    const response = await fetch(path, { method, headers, body, credentials: "same-origin", cache: "no-store", signal: controller.signal });
    let result;
    try { result = await response.json(); } catch (_) { result = {}; }
    if (!response.ok) {
      // Keep the key even for authentication/validation errors: this may be a
      // retry of an earlier accepted job whose response was lost.
      if (response.status === 401 && path !== "/api/login") {
        if (selectedControlView !== "overview") pendingControlView = selectedControlView;
        controlSession = { ...(controlSession || {}), authenticated: false };
        controlState = null;
        clearPrivateViews();
        // Expired cookies also invalidate their anti-forgery token. Obtain a
        // fresh anonymous session before showing the sign-in form again.
        try {
          const freshSession = await fetch("/api/session", { method: "GET", credentials: "same-origin", cache: "no-store", headers: new Headers(), signal: controller.signal });
          if (freshSession.ok) controlSession = await freshSession.json();
        } catch (_) { /* Signing in will obtain a fresh session again. */ }
        renderControlAccess();
      }
      const failure = new Error("Control Center request failed");
      failure.code = result.code || result.message_code || (typeof result.error === "string" ? result.error : result.error?.code) || result.error_code || (response.status === 401 ? "authentication_required" : "request_failed");
      throw failure;
    }
    if (requestIdentity && (typeof result.job?.id === "string" || typeof result.scan_session?.id === "string")) {
      pendingControlRequests.delete(requestIdentity);
      savePendingControlRequests();
    }
    return result;
  } catch (error) {
    if (error.name === "AbortError") {
      const timeoutError = new Error("The response did not arrive before the request timeout");
      timeoutError.code = "request_timeout";
      throw timeoutError;
    }
    throw error;
  } finally {
    clearTimeout(timeout);
  }
}

async function signInControlCenter(password) {
  // A sign-in form can remain open longer than its anonymous session.
  controlSession = await controlRequest("/api/session");
  controlSession = await controlRequest("/api/login", { method: "POST", body: { password } });
  return controlSession;
}

async function performControlAction(button, action) {
  if (button?.disabled) return;
  if (button) {
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
  }
  try { await action(); }
  catch (error) { showControlFeedback(errorTranslation(error), {}, true); }
  finally {
    if (button) {
      button.disabled = false;
      button.removeAttribute("aria-busy");
    }
    updateScannerControls();
    if (typeof renderScanSessions === "function") renderScanSessions(true);
  }
}

function showControlView(name, focus = false) {
  if (!controlViews.includes(name)) name = "overview";
  if (name !== "overview" && !controlSession?.authenticated) {
    pendingControlView = name;
    name = "overview";
    findElementById("access-password").focus();
  }
  selectedControlView = name;
  for (const viewName of controlViews) {
    const active = viewName === name;
    const tab = findElementById(`tab-${viewName}`);
    tab.setAttribute("aria-selected", String(active));
    tab.tabIndex = active ? 0 : -1;
    findElementById(`view-${viewName}`).hidden = !active;
    if (active && focus) tab.focus();
  }
  findElementById("refresh").hidden = name !== "overview";
  findElementById("control-jobs").hidden = !controlSession?.authenticated || !(controlState?.jobs?.length);
}

function clearPrivateViews() {
  selectedDocumentIdentifier = null;
  documentSelection = [];
  lastDocumentRenderKey = "";
  lastControlCollections = {};
  for (const identifier of ["document-list", "profile-list", "control-job-list", "component-list", "selected-documents"]) {
    findElementById(identifier).replaceChildren();
  }
  for (const identifier of ["print-preview", "document-detail-preview", "scan-latest"]) {
    const container = findElementById(identifier);
    container.replaceChildren();
    delete container.dataset.record;
    delete container.dataset.previewFailed;
  }
  for (const identifier of ["document-detail-title", "document-detail-meta", "print-document-detail", "document-count", "login-message"]) findElementById(identifier).textContent = "";
  for (const identifier of ["rename-document-form", "transform-document-form", "profile-form"]) findElementById(identifier).reset();
  resetProfileEditor();
  delete findElementById("login-message").dataset.messageIdentifier;
  findElementById("control-feedback").textContent = "";
  findElementById("control-feedback").hidden = true;
  lastControlFeedback = null;
  findElementById("document-detail").hidden = true;
  findElementById("print-document").replaceChildren();
  findElementById("control-jobs").hidden = true;
  if (findElementById("action-confirmation").open) findElementById("action-confirmation").close();
  confirmationAction = null;
  if (typeof clearScanSessionViews === "function") clearScanSessionViews();
  if (typeof clearScanAreaViews === "function") clearScanAreaViews();
  showControlView("overview");
}

function renderControlAccess() {
  const authenticated = Boolean(controlSession?.authenticated);
  const accessPanel = findElementById("access-panel");
  accessPanel.hidden = authenticated;
  const configured = (controlSession?.configured ?? controlSession?.authentication_configured) !== false;
  findElementById("login-form").hidden = !configured;
  findElementById("access-description").textContent = controlText(configured ? "auth.description" : "auth.not_configured");
  if (!authenticated) {
    showControlView("overview");
  } else {
    const target = pendingControlView || selectedControlView;
    pendingControlView = null;
    showControlView(target);
  }
}

async function refreshControlCenter() {
  if (!controlSession?.authenticated || controlRefreshActive) return;
  controlRefreshActive = true;
  try {
    const state = await controlRequest("/api/control-center");
    if (!state || !Array.isArray(state.documents) || !Array.isArray(state.jobs)) throw new Error("Invalid Control Center state");
    controlState = state;
    renderControlCenter();
  } catch (error) {
    if (error.code !== "authentication_required") showControlFeedback(errorTranslation(error), {}, true);
  } finally { controlRefreshActive = false; }
}

function makeElement(tagName, className, text) {
  const element = document.createElement(tagName);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

function makeButton(text, className, handler) {
  const button = makeElement("button", className, text);
  button.type = "button";
  button.addEventListener("click", handler);
  return button;
}

function formatDocumentSize(bytes) {
  const amount = Number(bytes) || 0;
  const formatter = new Intl.NumberFormat(selectedLanguage, { maximumFractionDigits: 1 });
  return amount >= 1048576 ? `${formatter.format(amount / 1048576)} MB` : `${formatter.format(amount / 1024)} KB`;
}

function documentMetadata(documentRecord) {
  const pages = Number(documentRecord.pages) || 1;
  const pageText = controlText(pages === 1 ? "documents.page_one" : "documents.page_other", { count: numberFormatter.format(pages) });
  const created = documentRecord.created_at ? new Date(documentRecord.created_at) : null;
  const date = created && Number.isFinite(created.getTime()) ? new Intl.DateTimeFormat(selectedLanguage, { dateStyle: "medium" }).format(created) : "";
  return `${pageText} · ${formatDocumentSize(documentRecord.size || documentRecord.size_bytes)}${date ? ` · ${date}` : ""}${documentRecord.keep ? ` · ${controlText("documents.kept")}` : ""}`;
}

function documentRoute(identifier, suffix) {
  return `/api/documents/${encodeURIComponent(identifier)}/${suffix}`;
}

function renderDocumentPreview(container, record) {
  container.replaceChildren();
  delete container.dataset.previewFailed;
  if (!record) {
    container.append(makeElement("p", "", controlText("print.choose_document")));
    return;
  }
  const image = makeElement("img");
  image.alt = controlText("documents.preview_label", { name: record.name });
  image.src = documentRoute(record.id, "preview");
  image.loading = "lazy";
  image.addEventListener("error", () => {
    container.dataset.previewFailed = String(Date.now());
    container.replaceChildren(makeElement("p", "", controlText("documents.preview_unavailable")));
  }, { once: true });
  container.append(image);
}

function renderPrintDocuments() {
  const selector = findElementById("print-document");
  const previous = selector.value;
  selector.replaceChildren();
  const documents = controlState?.documents || [];
  if (!documents.length) selector.append(new Option(controlText("documents.none"), ""));
  for (const record of documents) selector.append(new Option(record.name, record.id));
  if (documents.some((record) => record.id === previous)) selector.value = previous;
  const record = documents.find((item) => item.id === selector.value);
  const key = `${record?.id || ""}:${record?.updated_at || record?.created_at || ""}:${selectedLanguage}`;
  if (findElementById("print-preview").dataset.record !== key || previewNeedsRetry(findElementById("print-preview"))) {
    renderDocumentPreview(findElementById("print-preview"), record);
    findElementById("print-preview").dataset.record = key;
  }
  findElementById("print-document-detail").textContent = record ? `${record.name} · ${documentMetadata(record)}` : "";
}

function renderDocumentSelection() {
  const documents = controlState?.documents || [];
  documentSelection = documentSelection.filter((identifier) => documents.some((record) => record.id === identifier));
  const strip = findElementById("selected-documents");
  strip.replaceChildren();
  strip.hidden = !documentSelection.length;
  documentSelection.forEach((identifier, index) => {
    const record = documents.find((item) => item.id === identifier);
    const item = makeElement("div", "selection-item");
    item.append(makeElement("span", "", `${index + 1}. ${record.name}`));
    for (const [direction, symbol, label] of [[-1, "↑", "documents.move_earlier"], [1, "↓", "documents.move_later"]]) {
      const button = makeButton(symbol, "", () => {
        const nextIndex = index + direction;
        [documentSelection[index], documentSelection[nextIndex]] = [documentSelection[nextIndex], documentSelection[index]];
        renderDocumentSelection();
      });
      button.setAttribute("aria-label", controlText(label, { name: record.name }));
      button.disabled = index + direction < 0 || index + direction >= documentSelection.length;
      item.append(button);
    }
    strip.append(item);
  });
  findElementById("merge-documents").disabled = documentSelection.length < 2;
}

function renderDocuments() {
  const query = findElementById("document-search").value.trim().toLocaleLowerCase(selectedLanguage);
  const allDocuments = controlState?.documents || [];
  const documents = allDocuments.filter((record) => String(record.name).toLocaleLowerCase(selectedLanguage).includes(query));
  const container = findElementById("document-list");
  container.replaceChildren();
  findElementById("document-count").textContent = controlText("documents.count", { count: numberFormatter.format(allDocuments.length) });
  if (!documents.length) container.append(makeElement("p", "library-empty", controlText(query ? "documents.no_match" : "documents.empty")));
  for (const record of documents) {
    const row = makeElement("div", `document-row${selectedDocumentIdentifier === record.id ? " selected" : ""}`);
    row.setAttribute("role", "listitem");
    const checkbox = makeElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = documentSelection.includes(record.id);
    checkbox.setAttribute("aria-label", controlText("documents.select", { name: record.name }));
    checkbox.addEventListener("change", () => {
      if (checkbox.checked) documentSelection.push(record.id);
      else documentSelection = documentSelection.filter((identifier) => identifier !== record.id);
      renderDocumentSelection();
    });
    const formatLabel = { "application/pdf": "PDF", "image/png": "PNG", "image/jpeg": "JPEG" }[record.media_type] || (record.kind === "pdf" ? "PDF" : controlText("documents.file"));
    const symbol = makeElement("span", "file-symbol", formatLabel);
    symbol.setAttribute("aria-hidden", "true");
    const information = makeButton("", "document-row-info", () => selectDocument(record.id));
    information.append(makeElement("span", "document-row-name", record.name), makeElement("span", "document-row-meta", documentMetadata(record)));
    row.append(checkbox, symbol, information);
    container.append(row);
  }
  renderDocumentSelection();
  if (selectedDocumentIdentifier && !allDocuments.some((record) => record.id === selectedDocumentIdentifier)) selectedDocumentIdentifier = null;
  renderSelectedDocument();
}

function selectDocument(identifier) {
  selectedDocumentIdentifier = identifier;
  lastDocumentRenderKey = "";
  showControlView("documents");
  renderDocuments();
}

function selectedDocument() {
  return controlState?.documents?.find((record) => record.id === selectedDocumentIdentifier);
}

function renderSelectedDocument() {
  const record = selectedDocument();
  const panel = findElementById("document-detail");
  panel.hidden = !record;
  panel.parentElement.classList.toggle("has-detail", Boolean(record));
  if (!record) return;
  findElementById("document-detail-title").textContent = record.name;
  findElementById("document-detail-meta").textContent = documentMetadata(record);
  const previewImage = findElementById("document-detail-preview").querySelector("img");
  if (previewImage) previewImage.alt = controlText("documents.preview_label", { name: record.name });
  findElementById("download-document").href = documentRoute(record.id, "file");
  findElementById("download-text").href = documentRoute(record.id, "text");
  findElementById("download-text").hidden = !record.has_text && !record.text_available;
  const key = `${record.id}:${record.updated_at || record.created_at || ""}`;
  // Polling must not replace an input while someone is renaming or arranging pages.
  if (key !== lastDocumentRenderKey) {
    findElementById("rename-document-form").elements.name.value = record.name;
    findElementById("keep-document").checked = Boolean(record.keep);
    findElementById("transform-document-form").elements.pages.value = Array.from({ length: Number(record.pages) || 1 }, (_, index) => index + 1).join(",");
    renderDocumentPreview(findElementById("document-detail-preview"), record);
    lastDocumentRenderKey = key;
  }
  if (previewNeedsRetry(findElementById("document-detail-preview"))) renderDocumentPreview(findElementById("document-detail-preview"), record);
  findElementById("ocr-form").querySelector("button").disabled = controlState?.capabilities?.ocr?.available === false;
}

function rerenderControlMessages() {
  if (lastControlFeedback) showControlFeedback(lastControlFeedback.identifier, lastControlFeedback.parameters, lastControlFeedback.error);
  const loginMessageIdentifier = findElementById("login-message").dataset.messageIdentifier;
  if (loginMessageIdentifier) findElementById("login-message").textContent = controlText(loginMessageIdentifier);
  if (findElementById("profile-form").dataset.profileId) findElementById("profile-editor-heading").textContent = controlText("profiles.edit");
}

function readFormSettings(form) {
  const settings = Object.fromEntries(new FormData(form));
  for (const key of ["copies", "resolution", "rotation", "retention_hours", "max_storage_mb"]) {
    if (key in settings) settings[key] = Number(settings[key]);
  }
  return typeof settingsWithScanArea === "function" ? settingsWithScanArea(form, settings) : settings;
}

function applyFormSettings(form, settings) {
  if (form.id === "copy-form" && !settings?.area) findElementById("copy-use-scan-area").checked = false;
  const aliases = { paper: "paper_size", color: "colour", pages: "page_ranges", fit: "image_fit" };
  for (const [key, value] of Object.entries(settings || {})) {
    if (key === "area" && ["scan-form", "copy-form"].includes(form.id) && typeof applyScanAreaSettings === "function") {
      applyScanAreaSettings(value);
      if (form.id === "copy-form") findElementById("copy-use-scan-area").checked = Boolean(value);
      continue;
    }
    const fieldName = form.id === "copy-form" && key === "mode" ? "colour" : aliases[key] || key;
    const field = form.elements.namedItem(fieldName);
    let fieldValue = value === null ? "" : String(value);
    if (form.id === "copy-form" && key === "mode") fieldValue = value === "color" ? "Color" : "Mono";
    if (field?.tagName === "SELECT" && ![...field.options].some((option) => option.value === fieldValue)) continue;
    if (field && field.type !== "file") field.value = fieldValue;
  }
}

function setScannerOptions(selector, values, fallbackLabel) {
  if (!Array.isArray(values) || !values.length) return;
  const previous = selector.value;
  selector.replaceChildren();
  for (const entry of values) {
    const value = typeof entry === "object" ? entry.value || entry.name : entry;
    const label = fallbackLabel ? fallbackLabel(value) : String(value);
    selector.append(new Option(label, value));
  }
  if ([...selector.options].some((option) => option.value === previous)) selector.value = previous;
}

function scannerReadinessMessage(scanner) {
  if (scanner.available === true) return "scan.available";
  if (scanner.reason_code === "scanner_not_checked") return "scan.needs_check";
  if (scanner.reason_code === "scanner_software_missing") return "scan.not_configured";
  if (scanner.reason_code) return "scan.unavailable";
  return scanner.configured === false ? "scan.not_configured" : "scan.unavailable";
}

function updateScannerControls() {
  const scanner = controlState?.capabilities?.scanner || {};
  const available = scanner.available === true;
  const identifier = scannerReadinessMessage(scanner);
  for (const noteIdentifier of ["scanner-note", "copy-scanner-note"]) {
    const note = findElementById(noteIdentifier);
    note.textContent = controlText(identifier);
    note.classList.toggle("available", available);
  }
  for (const formIdentifier of ["scan-form", "copy-form"]) {
    const form = findElementById(formIdentifier);
    form.querySelectorAll("button").forEach((button) => {
      if (button.type !== "submit" && button.id !== "scan-preview-button") return;
      const usesArea = formIdentifier === "scan-form" || findElementById("copy-use-scan-area").checked;
      const invalidArea = usesArea && button.id !== "scan-preview-button" && typeof scanAreaIsValid === "function" && !scanAreaIsValid();
      if (!button.hasAttribute("aria-busy")) button.disabled = !available || invalidArea;
    });
  }
  const scanForm = findElementById("scan-form");
  setScannerOptions(scanForm.elements.mode, scanner.modes, (mode) => controlText(mode === "gray" || mode === "Gray" || mode === "Grayscale" ? "print.grayscale" : mode === "lineart" || mode === "Lineart" ? "scan.lineart" : "print.full_colour"));
  setScannerOptions(scanForm.elements.resolution, scanner.resolutions, (resolution) => `${resolution} dpi`);
  setScannerOptions(findElementById("copy-form").elements.resolution, scanner.resolutions, (resolution) => `${resolution} dpi`);
}

function renderProfiles() {
  const profiles = controlState?.profiles || [];
  const container = findElementById("profile-list");
  container.replaceChildren();
  const scanSelector = findElementById("scan-profile");
  const previous = scanSelector.value;
  scanSelector.replaceChildren(new Option(controlText("profiles.manual"), ""));
  if (!profiles.length) container.append(makeElement("p", "library-empty", controlText("profiles.empty")));
  for (const profile of profiles) {
    if (profile.kind === "scan") scanSelector.append(new Option(profile.name, profile.id));
    const card = makeElement("article", "profile-card");
    card.append(makeElement("h3", "", profile.name));
    card.append(makeElement("p", "", controlText(profile.kind === "ocr" ? "ocr.title" : `control.${profile.kind}`)));
    const actions = makeElement("div", "button-row");
    actions.append(makeButton(controlText("profiles.apply"), "secondary-button", () => {
      const form = findElementById(`${profile.kind}-form`);
      if (form) {
        applyFormSettings(form, profile.settings);
        showControlView(profile.kind === "ocr" ? "documents" : profile.kind);
        showControlFeedback("profiles.applied", { name: profile.name });
      }
    }));
    actions.append(makeButton(controlText("profiles.edit"), "text-button", () => {
      const form = findElementById("profile-form");
      form.elements.name.value = profile.name;
      form.elements.kind.value = profile.kind;
      form.dataset.profileId = profile.id;
      const settingsForm = findElementById(`${profile.kind}-form`);
      if (settingsForm) applyFormSettings(settingsForm, profile.settings);
      findElementById("profile-editor-heading").textContent = controlText("profiles.edit");
      findElementById("profile-edit-cancel").hidden = false;
      form.elements.name.focus();
    }));
    if (!profile.builtin && !profile.read_only) actions.append(makeButton(controlText("profiles.delete"), "text-button danger-text", (event) => performControlAction(event.currentTarget, async () => {
      await controlRequest(`/api/profiles/${encodeURIComponent(profile.id)}`, { method: "DELETE" });
      showControlFeedback("profiles.deleted");
      await refreshControlCenter();
    })));
    card.append(actions);
    container.append(card);
  }
  if ([...scanSelector.options].some((option) => option.value === previous)) scanSelector.value = previous;
}

function renderControlJobs() {
  const jobs = controlState?.jobs || [];
  const container = findElementById("control-job-list");
  container.replaceChildren();
  findElementById("control-jobs").hidden = !jobs.length || !controlSession?.authenticated;
  findElementById("control-job-count").textContent = numberFormatter.format(jobs.length);
  for (const job of jobs.slice(0, 15)) {
    const row = makeElement("article", "control-job");
    const dot = makeElement("span", `job-dot ${job.state}`);
    dot.setAttribute("aria-hidden", "true");
    const information = makeElement("div");
    const kindIdentifier = `jobs.kind.${job.kind}`;
    information.append(makeElement("strong", "", controlText(dashboardLanguages.en.messages[kindIdentifier] ? kindIdentifier : "jobs.kind.processing")));
    const messageIdentifier = `job.${job.message_code || job.code || ""}`;
    const errorIdentifier = `error.${job.message_code || job.code || ""}`;
    let detail = dashboardLanguages.en.messages[messageIdentifier] ? controlText(messageIdentifier) : dashboardLanguages.en.messages[errorIdentifier] ? controlText(errorIdentifier) : "";
    if (job.cups_job_id && !dashboardLanguages.en.messages[messageIdentifier]) detail = controlText("jobs.submitted_detail", { identifier: job.cups_job_id });
    information.append(makeElement("p", "job-description", detail));
    const actions = makeElement("div", "control-job-actions");
    const stateIdentifier = `jobs.state.${job.state}`;
    actions.append(makeElement("span", "control-job-state", controlText(dashboardLanguages.en.messages[stateIdentifier] ? stateIdentifier : "jobs.state.unknown")));
    const resultIdentifier = job.result_document_id || job.result?.document_id;
    if (resultIdentifier) actions.append(makeButton(controlText("jobs.open_result"), "text-button", () => selectDocument(resultIdentifier)));
    if (!terminalJobStates.includes(job.state) || (["submitted", "interrupted", "failed"].includes(job.state) && job.cups_job_id)) actions.append(makeButton(controlText("jobs.cancel"), "secondary-button", (event) => performControlAction(event.currentTarget, async () => {
      await controlRequest(`/api/jobs/${encodeURIComponent(job.id)}/cancel`, { method: "POST", body: {} });
      await refreshControlCenter();
    })));
    row.append(dot, information, actions);
    container.append(row);
  }
}

function renderSettings() {
  const settings = controlState?.settings || {};
  const form = findElementById("settings-form");
  if (!form.contains(document.activeElement)) applyFormSettings(form, settings);
  const retention = controlText("documents.retention_hours", { hours: numberFormatter.format(settings.retention_hours || 24) });
  findElementById("document-retention-note").textContent = retention;
  findElementById("upload-retention-note").textContent = `${controlText("documents.upload_limits")} ${retention}`;
  const usage = controlState?.storage || settings.storage || {};
  findElementById("storage-usage").textContent = controlText("settings.usage", { size: formatDocumentSize(usage.used_bytes || usage.bytes || 0) });
  const capabilities = controlState?.capabilities || {};
  const list = findElementById("component-list");
  list.replaceChildren();
  for (const [key, label] of [["print", "control.print"], ["scanner", "control.scan"], ["ocr", "ocr.title"]]) {
    const entry = makeElement("li");
    const available = capabilities[key]?.available === true;
    entry.append(makeElement("span", "", controlText(label)), makeElement("span", "component-status", controlText(available ? "settings.available" : "settings.unavailable")));
    list.append(entry);
  }
}

function previewNeedsRetry(container) {
  return container.dataset.previewFailed && Date.now() - Number(container.dataset.previewFailed) >= 3000;
}

function updateSupportedPrintOptions() {
  const capabilities = controlState?.capabilities?.print || {};
  const form = findElementById("print-form");
  const signature = `${selectedLanguage}:${JSON.stringify(capabilities)}`;
  if (form.dataset.capabilities !== signature) {
    setScannerOptions(form.elements.paper_size, capabilities.papers || capabilities.paper_sizes, (paper) => dashboardLanguages.en.messages[`paper.${paper}`] ? controlText(`paper.${paper}`) : paper);
    setScannerOptions(form.elements.media, capabilities.media || Object.keys(capabilities.media_qualities || {}), (media) => controlText(`media.${media}`));
    form.dataset.capabilities = signature;
  }
  const qualities = capabilities.media_qualities?.[form.elements.media.value];
  if (qualities) setScannerOptions(form.elements.quality, qualities, (quality) => controlText(`quality.${quality}`));
}

function updateOCRLanguageOptions() {
  const languages = controlState?.capabilities?.ocr?.languages || [];
  const selector = findElementById("ocr-form").elements.languages;
  if (!languages.length) return;
  const choices = [];
  if (languages.includes("deu") && languages.includes("eng")) choices.push(["deu+eng", controlText("ocr.german_english")]);
  for (const language of languages) {
    const identifier = language === "deu" ? "ocr.german" : language === "eng" ? "ocr.english" : null;
    choices.push([language, identifier ? controlText(identifier) : language]);
  }
  const previous = selector.value;
  selector.replaceChildren(...choices.map(([value, label]) => new Option(label, value)));
  if (choices.some(([value]) => value === previous)) selector.value = previous;
}

function renderControlCenter() {
  if (!controlState) return;
  const changed = (key, value) => {
    const signature = `${selectedLanguage}:${JSON.stringify(value)}`;
    const different = lastControlCollections[key] !== signature;
    lastControlCollections[key] = signature;
    return different;
  };
  if (changed("documents", controlState.documents)) {
    renderPrintDocuments();
    renderDocuments();
  } else {
    if (previewNeedsRetry(findElementById("print-preview"))) renderPrintDocuments();
    if (previewNeedsRetry(findElementById("document-detail-preview"))) renderSelectedDocument();
  }
  if (changed("profiles", controlState.profiles)) renderProfiles();
  if (changed("jobs", controlState.jobs)) renderControlJobs();
  if (changed("settings", [controlState.settings, controlState.storage, controlState.capabilities])) renderSettings();
  if (changed("capabilities", controlState.capabilities)) {
    updateScannerControls();
    updateSupportedPrintOptions();
    updateOCRLanguageOptions();
  }
  if (typeof renderScanArea === "function") renderScanArea();
  if (typeof renderScanSessions === "function") renderScanSessions();
  const latestScanJob = controlState.jobs.find((job) => ["scan", "copy", "scan_page", "scan_preview"].includes(job.kind) && job.result_document_id);
  const latestScan = latestScanJob ? controlState.documents.find((record) => record.id === latestScanJob.result_document_id) : null;
  const latestContainer = findElementById("scan-latest");
  if (latestScan && latestContainer.dataset.record !== latestScan.id) {
    renderDocumentPreview(latestContainer, latestScan);
    latestContainer.dataset.record = latestScan.id;
  }
}

function showActionConfirmation(kind, settings, action) {
  const dialog = findElementById("action-confirmation");
  findElementById("confirmation-title").textContent = controlText(kind === "copy" ? "copy.confirm_title" : "print.confirm_title");
  findElementById("confirmation-description").textContent = controlText(kind === "copy" ? "copy.confirm_help" : "print.confirm_help");
  findElementById("confirmation-submit").textContent = controlText(kind === "copy" ? "copy.start" : "print.start");
  const details = findElementById("confirmation-details");
  details.replaceChildren();
  const record = controlState?.documents?.find((item) => item.id === settings.document_id);
  const pairs = [
    ...(record ? [["documents.document", record.name]] : []),
    ["print.copies", numberFormatter.format(settings.copies)],
    ["print.paper", dashboardLanguages.en.messages[`paper.${settings.paper_size}`] ? controlText(`paper.${settings.paper_size}`) : settings.paper_size || "A4"],
    ["print.media", controlText(`media.${settings.media || "Plain"}`)],
    ["print.colour", controlText(settings.colour === "Mono" ? "print.grayscale" : "print.full_colour")],
    ["print.quality", controlText(`quality.${settings.quality || "Normal"}`)],
    ...(kind === "print" ? [["print.pages", settings.page_ranges || controlText("print.all_pages")]] : [])
  ];
  if (settings.area && typeof describeScanArea === "function") pairs.push(["scan_area.heading", describeScanArea(settings.area)]);
  for (const [label, value] of pairs) details.append(makeElement("dt", "", controlText(label)), makeElement("dd", "", value));
  confirmationAction = action;
  dialog.showModal();
}

async function startDocumentJob(path, settings, button) {
  await performControlAction(button, async () => {
    const result = await controlRequest(path, { method: "POST", body: settings });
    showControlFeedback(result.job ? "jobs.accepted" : result.document ? "documents.saved" : result.profile ? "profiles.saved" : "settings.saved");
    if (result.document?.id) selectedDocumentIdentifier = result.document.id;
    await refreshControlCenter();
  });
}

// Keyboard navigation follows the accessible tab pattern; it never performs work.
for (const [index, viewName] of controlViews.entries()) {
  const tab = findElementById(`tab-${viewName}`);
  tab.addEventListener("click", () => showControlView(viewName));
  tab.addEventListener("keydown", (event) => {
    const direction = event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
    if (direction) {
      event.preventDefault();
      showControlView(controlViews[(index + direction + controlViews.length) % controlViews.length], true);
    } else if (event.key === "Home" || event.key === "End") {
      event.preventDefault();
      showControlView(event.key === "Home" ? controlViews[0] : controlViews[controlViews.length - 1], true);
    }
  });
}
for (const button of document.querySelectorAll("[data-view-trigger]")) button.addEventListener("click", () => showControlView(button.dataset.viewTrigger));

findElementById("login-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  performControlAction(form.querySelector("button"), async () => {
    try {
      controlSession = await signInControlCenter(form.elements.password.value);
      form.reset();
      findElementById("login-message").textContent = "";
      renderControlAccess();
      await refreshControlCenter();
    } catch (error) {
      findElementById("login-message").dataset.messageIdentifier = errorTranslation(error);
      findElementById("login-message").textContent = controlText(errorTranslation(error));
      throw error;
    }
  });
});
findElementById("logout").addEventListener("click", (event) => performControlAction(event.currentTarget, async () => {
  await controlRequest("/api/logout", { method: "POST", body: {} });
  controlSession = await controlRequest("/api/session");
  controlState = null;
  clearPrivateViews();
  renderControlAccess();
  showControlFeedback("auth.signed_out");
}));

for (const button of document.querySelectorAll("[data-upload-trigger]")) button.addEventListener("click", () => findElementById("document-upload").click());
findElementById("document-upload").addEventListener("change", (event) => {
  const file = event.target.files?.[0];
  if (!file) return;
  performControlAction(null, async () => {
    if (file.size > 20 * 1024 * 1024) { const error = new Error("Upload exceeds limit"); error.code = "upload_too_large"; throw error; }
    showControlFeedback("documents.uploading");
    const result = await controlRequest("/api/documents", { method: "POST", body: file, headers: { "X-Filename": encodeURIComponent(file.name), "Content-Type": file.type || "application/octet-stream" }, timeout: 90000 });
    await refreshControlCenter();
    const identifier = result.document?.id || result.document_id;
    if (identifier) { findElementById("print-document").value = identifier; renderPrintDocuments(); selectedDocumentIdentifier = identifier; }
    showControlFeedback(result.job ? "jobs.accepted" : "documents.uploaded");
    event.target.value = "";
  });
});
findElementById("print-document").addEventListener("change", renderPrintDocuments);
findElementById("print-form").elements.media.addEventListener("change", updateSupportedPrintOptions);
findElementById("print-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const settings = readFormSettings(event.currentTarget);
  if (!settings.document_id) { showControlFeedback("error.document_required", {}, true); return; }
  showActionConfirmation("print", settings, () => startDocumentJob("/api/print", settings, findElementById("confirmation-submit")));
});
findElementById("copy-form").addEventListener("submit", (event) => {
  event.preventDefault();
  if (findElementById("copy-use-scan-area").checked && typeof scanAreaIsValid === "function" && !scanAreaIsValid()) { showControlFeedback("scan_area.invalid", {}, true); return; }
  const settings = { ...readFormSettings(event.currentTarget), media: "Plain" };
  settings.mode = settings.colour === "Mono" ? "gray" : "color";
  showActionConfirmation("copy", settings, () => startDocumentJob("/api/copy", settings, findElementById("confirmation-submit")));
});
findElementById("confirmation-cancel").addEventListener("click", () => { findElementById("action-confirmation").close(); confirmationAction = null; });
findElementById("action-confirmation").addEventListener("cancel", () => { confirmationAction = null; });
findElementById("confirmation-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const action = confirmationAction;
  confirmationAction = null; // A double click or reconnect cannot submit the same action twice.
  if (action) await action();
  findElementById("action-confirmation").close();
});

findElementById("detect-scanner").addEventListener("click", (event) => startDocumentJob("/api/scanner/detect", {}, event.currentTarget));
findElementById("scan-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  if (typeof scanAreaIsValid === "function" && !scanAreaIsValid()) { showControlFeedback("scan_area.invalid", {}, true); return; }
  const settings = readFormSettings(form);
  delete settings.profile;
  startDocumentJob("/api/scan", settings, form.querySelector("[type='submit']"));
});
findElementById("scan-preview-button").addEventListener("click", (event) => {
  startDocumentJob("/api/scanner/preview", { mode: findElementById("scan-form").elements.mode.value }, event.currentTarget);
});
findElementById("scan-profile").addEventListener("change", (event) => {
  const profile = controlState?.profiles?.find((item) => item.id === event.target.value);
  if (profile) applyFormSettings(findElementById("scan-form"), profile.settings);
});

findElementById("document-search").addEventListener("input", renderDocuments);
findElementById("close-document").addEventListener("click", () => { selectedDocumentIdentifier = null; renderDocuments(); });
findElementById("print-library-document").addEventListener("click", () => {
  findElementById("print-document").value = selectedDocumentIdentifier;
  renderPrintDocuments();
  showControlView("print");
});
findElementById("rename-document-form").addEventListener("submit", (event) => {
  event.preventDefault();
  startDocumentJob(`/api/documents/${encodeURIComponent(selectedDocumentIdentifier)}`, readFormSettings(event.currentTarget), event.currentTarget.querySelector("button"));
});
findElementById("keep-document").addEventListener("change", (event) => performControlAction(event.currentTarget, async () => {
  await controlRequest(`/api/documents/${encodeURIComponent(selectedDocumentIdentifier)}`, { method: "POST", body: { keep: event.target.checked } });
  await refreshControlCenter();
}));
findElementById("delete-document").addEventListener("click", (event) => {
  const identifier = selectedDocumentIdentifier;
  const record = selectedDocument();
  if (!record) return;
  // Deletion uses the same modal, with a separate explicit confirmation.
  findElementById("confirmation-title").textContent = controlText("documents.delete_title");
  findElementById("confirmation-description").textContent = controlText("documents.delete_help", { name: record.name });
  findElementById("confirmation-details").replaceChildren();
  findElementById("confirmation-submit").textContent = controlText("documents.delete");
  confirmationAction = () => performControlAction(findElementById("confirmation-submit"), async () => {
    await controlRequest(`/api/documents/${encodeURIComponent(identifier)}`, { method: "DELETE" });
    selectedDocumentIdentifier = null;
    await refreshControlCenter();
    showControlFeedback("documents.deleted");
  });
  findElementById("action-confirmation").showModal();
});
findElementById("share-document").addEventListener("click", (event) => performControlAction(event.currentTarget, async () => {
  const record = selectedDocument();
  if (!record) return;
  if (!navigator.share || !navigator.canShare) { findElementById("download-document").click(); return; }
  const response = await fetch(documentRoute(record.id, "file"), { credentials: "same-origin", cache: "no-store" });
  if (!response.ok) throw new Error("Download failed");
  const file = new File([await response.blob()], record.name, { type: response.headers.get("Content-Type") || "application/pdf" });
  if (!navigator.canShare({ files: [file] })) { findElementById("download-document").click(); return; }
  try { await navigator.share({ files: [file], title: record.name }); }
  catch (error) { if (error.name !== "AbortError") throw error; }
}));
findElementById("transform-document-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const settings = readFormSettings(event.currentTarget);
  const pages = [];
  for (const part of String(settings.pages).split(",")) {
    const range = part.trim().split("-").map(Number);
    if (range.length === 1) pages.push(range[0]);
    else if (range.length === 2 && range[1] >= range[0] && range[1] - range[0] < 250) for (let page = range[0]; page <= range[1]; page += 1) pages.push(page);
  }
  const edges = Object.fromEntries(["left", "top", "right", "bottom"].map((edge) => [edge, Number(settings[`crop_${edge}`]) / 100]));
  const crop = { left: edges.left, top: edges.top, width: 1 - edges.left - edges.right, height: 1 - edges.top - edges.bottom };
  startDocumentJob(documentRoute(selectedDocumentIdentifier, "transform"), { pages, rotation: settings.rotation, crop }, event.currentTarget.querySelector("button"));
});
findElementById("ocr-form").addEventListener("submit", (event) => {
  event.preventDefault();
  startDocumentJob(documentRoute(selectedDocumentIdentifier, "ocr"), readFormSettings(event.currentTarget), event.currentTarget.querySelector("button"));
});
findElementById("merge-documents").addEventListener("click", (event) => startDocumentJob("/api/documents/merge", { document_ids: [...documentSelection] }, event.currentTarget));

function resetProfileEditor() {
  const form = findElementById("profile-form");
  form.reset();
  delete form.dataset.profileId;
  findElementById("profile-editor-heading").textContent = controlText("profiles.create");
  findElementById("profile-edit-cancel").hidden = true;
}
findElementById("profile-edit-cancel").addEventListener("click", resetProfileEditor);
findElementById("profile-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const settings = readFormSettings(event.currentTarget);
  const profileSettings = readFormSettings(findElementById(`${settings.kind}-form`));
  delete profileSettings.document_id;
  const form = event.currentTarget;
  const values = { ...settings, settings: profileSettings };
  if (form.dataset.profileId) values.id = form.dataset.profileId;
  performControlAction(form.querySelector("button"), async () => {
    await controlRequest("/api/profiles", { method: "POST", body: values });
    resetProfileEditor();
    showControlFeedback("profiles.saved");
    await refreshControlCenter();
  });
});
findElementById("settings-form").addEventListener("submit", (event) => {
  event.preventDefault();
  startDocumentJob("/api/settings", readFormSettings(event.currentTarget), event.currentTarget.querySelector("button"));
});
findElementById("import-settings").addEventListener("change", (event) => performControlAction(null, async () => {
  const file = event.target.files?.[0];
  if (!file) return;
  if (file.size > 65536) { const error = new Error("Settings too large"); error.code = "invalid_settings"; throw error; }
  let settings;
  try { settings = JSON.parse(await file.text()); }
  catch (_) { const error = new Error("Invalid settings"); error.code = "invalid_settings"; throw error; }
  await controlRequest("/api/settings/import", { method: "POST", body: settings });
  event.target.value = "";
  showControlFeedback("settings.imported");
  await refreshControlCenter();
}));

document.addEventListener("dashboardlanguagechange", () => {
  renderControlAccess();
  if (controlState) renderControlCenter();
  rerenderControlMessages();
});
document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshControlCenter(); });
setInterval(() => { if (!document.hidden) refreshControlCenter(); }, 4000);
(async () => {
  try {
    controlSession = await controlRequest("/api/session");
    renderControlAccess();
    await refreshControlCenter();
  } catch (error) {
    controlSession = { authenticated: false, authentication_configured: false };
    renderControlAccess();
    showControlFeedback(errorTranslation(error), {}, true);
  }
})();
