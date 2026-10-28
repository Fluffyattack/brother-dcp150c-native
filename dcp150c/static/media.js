/* SPDX-License-Identifier: GPL-2.0-or-later */
"use strict";

// Only explicit button handlers request card access. Rendering uses cached state.
const memoryCardPageSize = 200;
const maximumMemoryCardImportBytes = 20 * 1024 * 1024;
let memoryCardRequestActive = false;
let lastMemoryCardListSignature = "";
let lastMemoryCardResultSignature = "";

function memoryCardOperationPending() {
  return memoryCardRequestActive || (controlState?.jobs || []).some((job) =>
    ["media_probe", "media_browse", "media_import"].includes(job.kind) && ["queued", "running"].includes(job.state));
}

function memoryCardDisplayState() {
  const capabilities = controlState?.capabilities?.media || {};
  const listing = controlState?.media_listing || null;
  const authenticated = Boolean(controlSession?.authenticated);
  const readable = authenticated && capabilities.available === true && capabilities.read_only === true;
  const matchingListing = readable && listing?.read_only === true
    && typeof capabilities.volume_id === "string" && capabilities.volume_id.length > 0
    && listing.volume_id === capabilities.volume_id && typeof listing.directory_id === "string";
  const documentsAvailable = controlState?.capabilities?.documents?.available === true;
  return { capabilities, listing, authenticated, readable, matchingListing, documentsAvailable, busy: memoryCardOperationPending() };
}

function memoryCardStatusMessage(state) {
  if (state.busy) return "memory_card.reading";
  if (state.capabilities.available === true && state.capabilities.read_only !== true) return "memory_card.read_only_required";
  if (state.matchingListing) return "memory_card.ready";
  if (state.readable) return "memory_card.open_folder";
  const reason = state.capabilities.reason_code || "media_not_checked";
  const translations = {
    media_not_checked: "memory_card.not_checked",
    media_not_found: "memory_card.not_found",
    media_no_card: "memory_card.no_card",
    media_unsupported: "memory_card.unsupported",
    media_permission_denied: "memory_card.permission_denied",
    media_access_denied: "memory_card.permission_denied",
    media_selection_required: "memory_card.selection_required",
    media_directory_limit: "memory_card.directory_limit",
    media_lock_unavailable: "memory_card.lock_unavailable",
    media_in_use: "memory_card.in_use",
    media_busy: "memory_card.device_busy",
    media_changed: "memory_card.changed",
    media_stale: "memory_card.changed",
    media_corrupt: "memory_card.corrupt",
    media_software_missing: "memory_card.not_configured"
  };
  if (translations[reason]) return translations[reason];
  if (dashboardLanguages.en.messages[`error.${reason}`]) return `error.${reason}`;
  return state.capabilities.configured === false ? "memory_card.not_configured" : "memory_card.unavailable";
}

function memoryCardFileEligibility(entry) {
  const size = entry?.size;
  const supportedName = typeof entry?.name === "string" && /\.(?:jpe?g|png|pdf)$/i.test(entry.name);
  if (entry?.supported === false || !supportedName) return { available: false, message: "memory_card.unsupported_file" };
  if (!Number.isSafeInteger(size) || size < 0) return { available: false, message: "memory_card.unavailable_file" };
  if (size > maximumMemoryCardImportBytes) return { available: false, message: "memory_card.file_too_large" };
  if (!size) return { available: false, message: "memory_card.empty_file" };
  return { available: true, message: "" };
}

function setMemoryCardText(identifier, text) {
  const element = findElementById(identifier);
  if (element.textContent !== text) element.textContent = text;
}

async function startMemoryCardJob(path, values, button) {
  if (memoryCardOperationPending() || !controlSession?.authenticated) return;
  memoryCardRequestActive = true;
  try {
    const operation = startDocumentJob(path, values, button);
    renderMediaReader();
    await operation;
  } finally {
    memoryCardRequestActive = false;
    renderMediaReader();
  }
}

function requestMemoryCardDirectory(identifier, offset, button, expectedVolume) {
  const state = memoryCardDisplayState();
  if (!state.readable || state.capabilities.volume_id !== expectedVolume || typeof identifier !== "string") {
    showControlFeedback("memory_card.changed", {}, true);
    return;
  }
  startMemoryCardJob(`/api/media/directories/${encodeURIComponent(identifier)}`, { offset, limit: memoryCardPageSize }, button);
}

function requestMemoryCardImport(entry, button, expectedVolume, expectedDirectory) {
  const state = memoryCardDisplayState();
  if (!state.matchingListing || state.capabilities.volume_id !== expectedVolume || state.listing.directory_id !== expectedDirectory) {
    showControlFeedback("memory_card.changed", {}, true);
    return;
  }
  if (!state.documentsAvailable) {
    showControlFeedback("error.document_dependencies", {}, true);
    return;
  }
  const eligibility = memoryCardFileEligibility(entry);
  if (!eligibility.available) {
    showControlFeedback(eligibility.message, {}, true);
    return;
  }
  startMemoryCardJob(`/api/media/files/${encodeURIComponent(entry.id)}/import`, {}, button);
}

function renderMemoryCardEntries(state) {
  const container = findElementById("media-entry-list");
  const entries = state.matchingListing && Array.isArray(state.listing.entries) ? state.listing.entries : [];
  const signature = `${selectedLanguage}:${JSON.stringify([state.matchingListing, state.documentsAvailable, state.capabilities.volume_id,
    state.listing?.directory_id, state.listing?.offset, entries])}`;
  if (signature !== lastMemoryCardListSignature) {
    const restoreHeadingFocus = container.contains(document.activeElement);
    container.replaceChildren();
    lastMemoryCardListSignature = signature;
    if (!entries.length) container.append(makeElement("p", "library-empty", controlText(state.matchingListing ? "memory_card.empty_folder" : "memory_card.open_folder")));
    for (const entry of entries) {
      if (!entry || typeof entry.id !== "string" || typeof entry.name !== "string") continue;
      const directory = entry.is_directory === true;
      let eligibility = directory ? { available: true, message: "" } : memoryCardFileEligibility(entry);
      if (!directory && eligibility.available && !state.documentsAvailable) eligibility = { available: false, message: "error.document_dependencies" };
      const row = makeElement("div", "document-row media-entry");
      row.setAttribute("role", "listitem");
      const format = directory ? controlText("memory_card.folder") : (/\.pdf$/i.test(entry.name) ? "PDF" : /\.png$/i.test(entry.name) ? "PNG" : /\.jpe?g$/i.test(entry.name) ? "JPEG" : controlText("documents.file"));
      const symbol = makeElement("span", "file-symbol media-file-symbol", format);
      symbol.setAttribute("aria-hidden", "true");
      const information = makeElement("div", "media-entry-information");
      information.append(makeElement("strong", "document-row-name", entry.name));
      const detail = directory ? controlText("memory_card.folder") : formatDocumentSize(entry.size);
      information.append(makeElement("span", "document-row-meta", detail));
      if (eligibility.message) information.append(makeElement("span", "media-entry-note", controlText(eligibility.message)));
      const action = makeButton(controlText(directory ? "memory_card.open" : "memory_card.import"), "secondary-button", (event) => {
        if (directory) requestMemoryCardDirectory(entry.id, 0, event.currentTarget, state.capabilities.volume_id);
        else requestMemoryCardImport(entry, event.currentTarget, state.capabilities.volume_id, state.listing.directory_id);
      });
      action.dataset.mediaAction = "true";
      action.dataset.mediaEligible = String(eligibility.available);
      action.setAttribute("aria-label", controlText(directory ? "memory_card.open_named" : "memory_card.import_named", { name: entry.name }));
      row.append(symbol, information, action);
      container.append(row);
    }
    if (restoreHeadingFocus && selectedControlView === "media") findElementById("media-directory-name").focus({ preventScroll: true });
  }
  for (const button of container.querySelectorAll("[data-media-action]")) {
    button.disabled = !state.matchingListing || state.busy || button.dataset.mediaEligible !== "true";
  }
}

function renderMemoryCardImportResult() {
  const panel = findElementById("media-import-result");
  const job = (controlState?.jobs || []).find((entry) => entry.kind === "media_import");
  const resultIdentifier = job?.result_document_id || job?.result?.document_id;
  const record = resultIdentifier ? (controlState?.documents || []).find((document) => document.id === resultIdentifier) : null;
  const signature = `${selectedLanguage}:${JSON.stringify([job?.id, job?.state, job?.message_code, resultIdentifier, record?.name])}`;
  if (signature === lastMemoryCardResultSignature) return;
  lastMemoryCardResultSignature = signature;
  panel.replaceChildren();
  panel.hidden = !job;
  if (!job) return;
  const pending = ["queued", "running"].includes(job.state);
  panel.append(makeElement("h3", "", controlText(record ? "memory_card.imported" : pending ? "jobs.kind.media_import" : "memory_card.last_import")));
  if (record) {
    panel.append(makeElement("p", "body-copy media-result-name", record.name));
    const actions = makeElement("div", "button-row");
    const download = makeElement("a", "secondary-button", controlText("documents.download"));
    download.href = documentRoute(record.id, "file");
    download.download = "";
    actions.append(makeButton(controlText("memory_card.open_document"), "primary-button", () => selectDocument(record.id)), download,
      makeButton(controlText("memory_card.print_settings"), "secondary-button", () => {
        findElementById("print-document").value = record.id;
        renderPrintDocuments();
        showControlView("print", true);
      }));
    panel.append(actions);
  } else {
    const code = job.message_code || "";
    const message = resultIdentifier ? "jobs.result_unavailable" : dashboardLanguages.en.messages[`job.${code}`] ? `job.${code}`
      : dashboardLanguages.en.messages[`error.${code}`] ? `error.${code}` : pending ? "job.job_running" : "error.processing_failed";
    panel.append(makeElement("p", "field-help", controlText(message)));
    if (pending) panel.append(makeButton(controlText("jobs.cancel"), "secondary-button", (event) => performControlAction(event.currentTarget, async () => {
      await controlRequest(`/api/jobs/${encodeURIComponent(job.id)}/cancel`, { method: "POST", body: {} });
      await refreshControlCenter();
    })));
  }
}

function renderMediaReader() {
  if (!controlSession?.authenticated) { clearMediaReaderViews(); return; }
  const state = memoryCardDisplayState();
  setMemoryCardText("media-status", controlText(memoryCardStatusMessage(state)));
  findElementById("media-probe").disabled = state.busy || state.capabilities.configured === false;
  findElementById("media-hardware-note").hidden = !state.readable || state.capabilities.hardware_verified === true;
  findElementById("media-workspace").hidden = !state.readable;
  const listing = state.matchingListing ? state.listing : null;
  const rootIdentifier = state.capabilities.root_directory_id;
  const rootAvailable = state.readable && typeof rootIdentifier === "string";
  findElementById("media-root").disabled = state.busy || !rootAvailable || listing?.directory_id === rootIdentifier;
  findElementById("media-parent").disabled = state.busy || !listing?.parent_directory_id;
  setMemoryCardText("media-directory-name", listing?.name || controlText("memory_card.root"));
  const entries = Array.isArray(listing?.entries) ? listing.entries : [];
  const total = Number.isSafeInteger(listing?.entry_count) ? listing.entry_count : entries.length;
  setMemoryCardText("media-item-count", listing ? controlText(total === 1 ? "memory_card.item_one" : "memory_card.item_other", { count: numberFormatter.format(total) }) : "");
  const offset = Number.isSafeInteger(listing?.offset) && listing.offset >= 0 ? listing.offset : 0;
  const hasNext = Number.isSafeInteger(listing?.next_offset) && listing.next_offset > offset;
  findElementById("media-previous-page").disabled = state.busy || !listing || offset === 0;
  findElementById("media-next-page").disabled = state.busy || !listing || !hasNext;
  setMemoryCardText("media-page-note", entries.length ? controlText("memory_card.page_range", {
    first: numberFormatter.format(offset + 1), last: numberFormatter.format(offset + entries.length), count: numberFormatter.format(total)
  }) : "");
  renderMemoryCardEntries(state);
  renderMemoryCardImportResult();
}

function clearMediaReaderViews() {
  memoryCardRequestActive = false;
  lastMemoryCardListSignature = "";
  lastMemoryCardResultSignature = "";
  findElementById("media-entry-list").replaceChildren();
  findElementById("media-import-result").replaceChildren();
  findElementById("media-import-result").hidden = true;
  for (const identifier of ["media-status", "media-directory-name", "media-item-count", "media-page-note"]) setMemoryCardText(identifier, "");
  for (const identifier of ["media-probe", "media-root", "media-parent", "media-previous-page", "media-next-page"]) findElementById(identifier).disabled = true;
  findElementById("media-workspace").hidden = true;
  findElementById("media-hardware-note").hidden = true;
}

findElementById("media-probe").addEventListener("click", (event) => startMemoryCardJob("/api/media/probe", {}, event.currentTarget));
findElementById("media-root").addEventListener("click", (event) => {
  const state = memoryCardDisplayState();
  requestMemoryCardDirectory(state.capabilities.root_directory_id, 0, event.currentTarget, state.capabilities.volume_id);
});
findElementById("media-parent").addEventListener("click", (event) => {
  const state = memoryCardDisplayState();
  requestMemoryCardDirectory(state.listing?.parent_directory_id, 0, event.currentTarget, state.capabilities.volume_id);
});
findElementById("media-previous-page").addEventListener("click", (event) => {
  const state = memoryCardDisplayState();
  requestMemoryCardDirectory(state.listing?.directory_id, Math.max(0, (state.listing?.offset || 0) - memoryCardPageSize), event.currentTarget, state.capabilities.volume_id);
});
findElementById("media-next-page").addEventListener("click", (event) => {
  const state = memoryCardDisplayState();
  requestMemoryCardDirectory(state.listing?.directory_id, state.listing?.next_offset, event.currentTarget, state.capabilities.volume_id);
});
document.addEventListener("dashboardlanguagechange", renderMediaReader);
renderMediaReader();
