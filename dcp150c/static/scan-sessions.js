/* SPDX-License-Identifier: GPL-2.0-or-later */
"use strict";

let selectedScanSessionIdentifier = null;
let scanSessionRenderSignature = "";
let editedScanPage = null;
let appliedScanSessionIdentifier = null;

function currentScanSession() {
  return controlState?.scan_sessions?.find((session) => session.id === selectedScanSessionIdentifier) || null;
}

function clearScanSessionViews() {
  selectedScanSessionIdentifier = null;
  scanSessionRenderSignature = "";
  editedScanPage = null;
  appliedScanSessionIdentifier = null;
  for (const identifier of ["scan-session-pages", "scan-session-result", "scan-session-select", "scan-page-editor-preview"]) findElementById(identifier).replaceChildren();
  for (const identifier of ["scan-session-status", "scan-session-count"]) findElementById(identifier).textContent = "";
  findElementById("scan-session-create-form").reset();
  findElementById("scan-session-finish-form").reset();
  findElementById("scan-session-workspace").hidden = true;
  if (findElementById("scan-page-editor").open) findElementById("scan-page-editor").close();
}

function scanSessionPageCount(count) {
  return controlText(count === 1 ? "scan_session.one_page" : "scan_session.pages", { count: numberFormatter.format(count) });
}

function restoreScanSessionSettings(session, scannerAvailable) {
  // A service restart clears discovered capabilities. Wait for the explicit
  // scanner check before restoring values that may be absent from the form.
  if (scannerAvailable && appliedScanSessionIdentifier !== session.id) {
    applyFormSettings(findElementById("scan-form"), { mode: session.settings.mode, resolution: session.settings.resolution, area: session.settings.area || null });
    appliedScanSessionIdentifier = session.id;
  }
}

function renderScanSessions(force = false) {
  if (!controlState || !controlSession?.authenticated) return;
  const sessions = controlState.scan_sessions || [];
  const scannerAvailable = controlState.capabilities?.scanner?.available === true;
  const signature = `${selectedLanguage}:${selectedScanSessionIdentifier}:${JSON.stringify([sessions, controlState.documents, scannerAvailable, controlState.capabilities?.ocr])}`;
  if (!force && signature === scanSessionRenderSignature) return;
  scanSessionRenderSignature = signature;
  if (!sessions.some((session) => session.id === selectedScanSessionIdentifier)) {
    selectedScanSessionIdentifier = sessions.find((session) => session.state === "open")?.id || sessions[0]?.id || null;
  }
  const selector = findElementById("scan-session-select");
  selector.replaceChildren();
  if (!sessions.length) selector.append(new Option(controlText("scan_session.none"), ""));
  for (const session of sessions) selector.append(new Option(`${session.name} · ${scanSessionPageCount(session.page_count)}`, session.id));
  selector.value = selectedScanSessionIdentifier || "";
  const session = currentScanSession();
  findElementById("scan-session-workspace").hidden = !session;
  findElementById("scan-session-count").textContent = session ? scanSessionPageCount(session.page_count) : "";
  if (!session) return;
  restoreScanSessionSettings(session, scannerAvailable);
  const busy = Boolean(session.active_job_id);
  const editable = session.state === "open" && !busy;
  const nextPage = findElementById("scan-session-next-page");
  nextPage.textContent = controlText(session.page_count ? "scan_session.next_page" : "scan_session.first_page");
  nextPage.hidden = session.state === "completed";
  if (!nextPage.hasAttribute("aria-busy")) nextPage.disabled = !editable || !scannerAvailable || session.page_count >= 25 || (typeof scanAreaIsValid === "function" && !scanAreaIsValid());
  const cancelButton = findElementById("scan-session-cancel-job");
  cancelButton.hidden = !busy;
  findElementById("scan-session-discard").disabled = busy;
  const status = session.state === "completed" ? "scan_session.completed" : busy ? "scan_session.busy" : "scan_session.ready";
  findElementById("scan-session-status").textContent = controlText(status, { count: numberFormatter.format(session.page_count) });
  const pages = findElementById("scan-session-pages");
  pages.replaceChildren();
  if (!session.pages.length) pages.append(makeElement("p", "field-help", controlText("scan_session.place_first")));
  session.pages.forEach((page, index) => {
    const card = makeElement("article", "scan-session-page");
    card.setAttribute("role", "listitem");
    card.dataset.pageIdentifier = page.id;
    card.dataset.documentIdentifier = page.document_id;
    const preview = makeElement("div", "document-preview compact-preview");
    const documentRecord = controlState.documents.find((document) => document.id === page.document_id);
    if (documentRecord) renderDocumentPreview(preview, documentRecord);
    card.append(makeElement("h4", "", controlText("scan_session.page_number", { count: numberFormatter.format(index + 1) })), preview);
    card.append(makeElement("p", "field-help", controlText(page.crop ? "scan_session.rotation_crop" : "scan_session.rotation", { degrees: numberFormatter.format(page.rotation) })));
    const actions = makeElement("div", "scan-session-page-actions");
    const moveUp = makeButton(controlText("scan_session.move_up"), "secondary-button", () => moveScanPage(index, -1));
    const moveDown = makeButton(controlText("scan_session.move_down"), "secondary-button", () => moveScanPage(index, 1));
    const edit = makeButton(controlText("scan_session.edit_page"), "secondary-button", () => openScanPageEditor(page.id));
    const remove = makeButton(controlText("scan_session.remove_page"), "text-button", () => saveScanPageOrder(session.pages.filter((entry) => entry.id !== page.id)));
    moveUp.disabled = !editable || index === 0;
    moveDown.disabled = !editable || index === session.pages.length - 1;
    edit.disabled = remove.disabled = !editable;
    actions.append(moveUp, moveDown, edit, remove);
    card.append(actions);
    pages.append(card);
  });
  const finish = findElementById("scan-session-finish-form");
  finish.hidden = session.state === "completed";
  for (const button of finish.querySelectorAll("button")) if (!button.hasAttribute("aria-busy")) button.disabled = !editable || !session.page_count;
  const ocrAvailable = controlState.capabilities?.ocr?.available === true;
  finish.elements.recognize_text.disabled = !ocrAvailable || !editable;
  if (!ocrAvailable) finish.elements.recognize_text.checked = false;
  const languageSelector = finish.elements.languages;
  const previousLanguage = languageSelector.value;
  const sourceLanguages = findElementById("ocr-form").elements.languages;
  languageSelector.replaceChildren(...Array.from(sourceLanguages.options, (option) => new Option(option.text, option.value)));
  if ([...languageSelector.options].some((option) => option.value === previousLanguage)) languageSelector.value = previousLanguage;
  languageSelector.disabled = !ocrAvailable || !editable;
  const result = findElementById("scan-session-result");
  result.replaceChildren();
  const resultDocument = controlState.documents.find((document) => document.id === session.result_document_id);
  result.hidden = !resultDocument;
  if (resultDocument) {
    const download = makeElement("a", "primary-button", controlText("documents.download"));
    download.href = `/api/documents/${encodeURIComponent(resultDocument.id)}/file`;
    download.download = "";
    result.append(download);
    if (resultDocument.has_text) {
      const text = makeElement("a", "text-button", controlText("ocr.download_text"));
      text.href = `/api/documents/${encodeURIComponent(resultDocument.id)}/text`;
      text.download = "";
      result.append(text);
    }
  }
}

async function saveScanPageOrder(pages, revision = null, sessionIdentifier = null) {
  const session = currentScanSession();
  if (!session && !sessionIdentifier) return;
  const identifier = sessionIdentifier || session.id;
  await performControlAction(null, async () => {
    await controlRequest(`/api/scan-sessions/${identifier}`, { method: "POST", body: { revision: revision ?? session.revision, pages } });
    await refreshControlCenter();
    showControlFeedback("scan_session.pages_saved");
  });
}

function moveScanPage(index, direction) {
  const pages = [...currentScanSession().pages];
  const target = index + direction;
  if (target < 0 || target >= pages.length) return;
  [pages[index], pages[target]] = [pages[target], pages[index]];
  saveScanPageOrder(pages);
}

function openScanPageEditor(identifier) {
  const session = currentScanSession();
  const page = session?.pages.find((entry) => entry.id === identifier);
  if (!page) return;
  editedScanPage = { sessionIdentifier: session.id, revision: session.revision, pageIdentifier: identifier, pages: session.pages };
  const form = findElementById("scan-page-editor-form");
  form.elements.rotation.value = page.rotation;
  const crop = page.crop || { left: 0, top: 0, width: 1, height: 1 };
  form.elements.crop_left.value = Math.round(crop.left * 100);
  form.elements.crop_top.value = Math.round(crop.top * 100);
  form.elements.crop_right.value = Math.round((1 - crop.left - crop.width) * 100);
  form.elements.crop_bottom.value = Math.round((1 - crop.top - crop.height) * 100);
  const container = findElementById("scan-page-editor-preview");
  container.replaceChildren();
  const documentRecord = controlState.documents.find((document) => document.id === page.document_id);
  if (documentRecord) renderDocumentPreview(container, documentRecord);
  findElementById("scan-page-editor").showModal();
}

findElementById("jump-to-scan-sessions").addEventListener("click", () => {
  findElementById("scan-session-panel").scrollIntoView({ behavior: "smooth", block: "start" });
  findElementById("scan-session-panel").focus({ preventScroll: true });
});
findElementById("scan-session-select").addEventListener("change", (event) => {
  selectedScanSessionIdentifier = event.target.value || null;
  scanSessionRenderSignature = "";
  renderScanSessions();
});
findElementById("scan-session-create-form").addEventListener("submit", (event) => {
  event.preventDefault();
  performControlAction(event.currentTarget.querySelector("button"), async () => {
    const name = event.target.elements.name.value.trim() || controlText("scan_session.default_name");
    const settings = readFormSettings(findElementById("scan-form"));
    delete settings.profile;
    const result = await controlRequest("/api/scan-sessions", { method: "POST", body: { name, settings } });
    selectedScanSessionIdentifier = result.scan_session.id;
    await refreshControlCenter();
    showControlFeedback("scan_session.created");
  });
});
findElementById("scan-session-next-page").addEventListener("click", (event) => {
  const session = currentScanSession();
  if (!session) return;
  if (typeof scanAreaIsValid === "function" && !scanAreaIsValid()) { showControlFeedback("scan_area.invalid", {}, true); return; }
  const settings = readFormSettings(findElementById("scan-form"));
  delete settings.profile;
  startDocumentJob(`/api/scan-sessions/${session.id}/pages`, { revision: session.revision, settings }, event.currentTarget);
});
findElementById("scan-session-cancel-job").addEventListener("click", (event) => {
  const identifier = currentScanSession()?.active_job_id;
  if (identifier) startDocumentJob(`/api/jobs/${identifier}/cancel`, {}, event.currentTarget);
});
findElementById("scan-session-discard").addEventListener("click", (event) => {
  const identifier = currentScanSession()?.id;
  if (identifier && window.confirm(controlText("scan_session.discard_confirm"))) performControlAction(event.currentTarget, async () => {
    await controlRequest(`/api/scan-sessions/${identifier}`, { method: "DELETE" });
    selectedScanSessionIdentifier = null;
    await refreshControlCenter();
    showControlFeedback("scan_session.removed");
  });
});
findElementById("scan-session-finish-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const session = currentScanSession();
  if (!session) return;
  const settings = { revision: session.revision, ocr: event.target.elements.recognize_text.checked, languages: event.target.elements.languages.value || "deu+eng" };
  startDocumentJob(`/api/scan-sessions/${session.id}/finish`, settings, event.currentTarget.querySelector("button"));
});
findElementById("scan-page-editor-cancel").addEventListener("click", () => findElementById("scan-page-editor").close());
findElementById("scan-page-editor-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!editedScanPage) return;
  const values = readFormSettings(event.target);
  const left = values.crop_left / 100, top = values.crop_top / 100;
  const crop = [values.crop_left, values.crop_top, values.crop_right, values.crop_bottom].some((value) => Number(value))
    ? { left, top, width: 1 - left - values.crop_right / 100, height: 1 - top - values.crop_bottom / 100 } : null;
  const pages = editedScanPage.pages.map((page) => page.id === editedScanPage.pageIdentifier ? { ...page, rotation: values.rotation, crop } : page);
  await saveScanPageOrder(pages, editedScanPage.revision, editedScanPage.sessionIdentifier);
  findElementById("scan-page-editor").close();
});
if (controlState) renderScanSessions();
