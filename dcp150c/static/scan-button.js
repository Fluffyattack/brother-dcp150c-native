/* SPDX-License-Identifier: GPL-2.0-or-later */
"use strict";

// Rendering reads cached settings. Only submitting this form can save changes.
const scanButtonActions = ["image", "file", "email", "ocr"];
let scanButtonDraft = null;
let scanButtonDraftDirty = false;
let scanButtonRequestActive = false;
let scanButtonLatestSnapshot = null;
let scanButtonLocalMessage = null;
let scanButtonSelectSignature = "";
let scanButtonRequestGeneration = 0;
let scanButtonResultDocumentIdentifier = null;

function copyScanButtonConfiguration(configuration) {
  if (!configuration || !Number.isSafeInteger(configuration.revision) || configuration.revision < 0
      || typeof configuration.enabled !== "boolean" || !configuration.bindings
      || typeof configuration.bindings !== "object" || Array.isArray(configuration.bindings)) return null;
  const result = { revision: configuration.revision, enabled: configuration.enabled, bindings: {} };
  for (const action of scanButtonActions) {
    const source = configuration.bindings[action];
    if (!source || typeof source !== "object") continue;
    const binding = {};
    for (const field of action === "ocr" ? ["scan_profile_id", "ocr_profile_id"] : ["scan_profile_id"]) {
      if (typeof source[field] === "string" && source[field]) binding[field] = source[field];
    }
    if (Object.keys(binding).length) result.bindings[action] = binding;
  }
  return result;
}

function cachedScanButtonSnapshot() {
  const snapshot = controlState?.scan_button;
  const configuration = copyScanButtonConfiguration(snapshot?.configuration);
  // A state request started before a save may arrive after its newer reply.
  if (configuration && (!scanButtonLatestSnapshot
      || configuration.revision >= scanButtonLatestSnapshot.configuration.revision)) {
    scanButtonLatestSnapshot = { ...snapshot, configuration };
  }
  return scanButtonLatestSnapshot;
}

function scanButtonProfile(identifier, kind) {
  return (controlState?.profiles || []).find((profile) => profile.id === identifier && profile.kind === kind);
}

function scanButtonBindingProblem(action, binding = {}) {
  const scanIdentifier = binding.scan_profile_id;
  const textIdentifier = binding.ocr_profile_id;
  if (action === "ocr" && Boolean(scanIdentifier) !== Boolean(textIdentifier)) return "scan_button.ocr_requires_profiles";
  if ((scanIdentifier && !scanButtonProfile(scanIdentifier, "scan"))
      || (textIdentifier && !scanButtonProfile(textIdentifier, "ocr"))) return "scan_button.missing_profile_help";
  return "";
}

function scanButtonConfigurationProblem(configuration, validateMissingProfiles = true) {
  for (const action of scanButtonActions) {
    const problem = scanButtonBindingProblem(action, configuration?.bindings?.[action]);
    if (problem && (validateMissingProfiles || problem !== "scan_button.missing_profile_help")) return problem;
  }
  if (configuration?.enabled && !Object.keys(configuration.bindings).length) return "scan_button.choose_assignment";
  return "";
}

function renderScanButtonProfileChoices() {
  const profiles = (controlState?.profiles || []).map(({ id, name, kind }) => ({ id, name, kind }));
  const signature = `${selectedLanguage}:${JSON.stringify([profiles, scanButtonDraft?.bindings])}`;
  if (signature === scanButtonSelectSignature) return;
  scanButtonSelectSignature = signature;
  for (const selector of findElementById("scan-button-form").querySelectorAll("[data-scan-button-action]")) {
    const action = selector.dataset.scanButtonAction;
    const kind = selector.dataset.scanButtonKind;
    const field = kind === "ocr" ? "ocr_profile_id" : "scan_profile_id";
    const selectedIdentifier = scanButtonDraft?.bindings?.[action]?.[field] || "";
    selector.replaceChildren(new Option(controlText("scan_button.unassigned_choice"), ""));
    for (const profile of profiles.filter((entry) => entry.kind === kind)) selector.append(new Option(profile.name, profile.id));
    if (selectedIdentifier && !profiles.some((entry) => entry.id === selectedIdentifier && entry.kind === kind)) {
      const missing = new Option(controlText("scan_button.missing_profile"), selectedIdentifier);
      missing.disabled = true;
      selector.append(missing);
    }
    selector.value = selectedIdentifier;
  }
}

function scanButtonStatusTranslation(code) {
  const identifier = `scan_button.${String(code || "").replace(/^scan_button_/, "")}`;
  return dashboardLanguages.en.messages[identifier] ? identifier : "scan_button.unavailable";
}

function renderScanButtonLastResult(snapshot) {
  const action = scanButtonActions.includes(snapshot?.last_action) ? snapshot.last_action : null;
  findElementById("scan-button-last-action").textContent = action
    ? controlText("scan_button.last_action", { action: controlText(`scan_button.action.${action}`) }) : "";
  const checked = snapshot?.last_checked_at ? new Date(snapshot.last_checked_at) : null;
  const timestamp = findElementById("scan-button-last-checked");
  const validTime = checked && Number.isFinite(checked.getTime());
  timestamp.hidden = !validTime;
  timestamp.textContent = validTime ? controlText("scan_button.last_checked", {
    time: new Intl.DateTimeFormat(selectedLanguage, { dateStyle: "short", timeStyle: "short" }).format(checked)
  }) : "";
  if (validTime) timestamp.dateTime = checked.toISOString();
  else timestamp.removeAttribute("datetime");
  const job = (controlState?.jobs || []).find((entry) => entry.id === snapshot?.last_job_id);
  const identifier = job?.result_document_id || job?.result?.document_id;
  const record = identifier ? (controlState?.documents || []).find((document) => document.id === identifier) : null;
  scanButtonResultDocumentIdentifier = record?.id || null;
  findElementById("scan-button-open-result").hidden = !record;
  findElementById("scan-button-open-result").disabled = !record;
  const jobState = job && dashboardLanguages.en.messages[`jobs.state.${job.state}`] ? controlText(`jobs.state.${job.state}`) : "";
  findElementById("scan-button-last-job-state").textContent = jobState ? controlText("scan_button.last_job", { state: jobState }) : "";
}

function renderScanButtonSettings() {
  if (!controlSession?.authenticated) { clearScanButtonViews(); return; }
  const snapshot = cachedScanButtonSnapshot();
  const configuration = snapshot?.configuration || null;
  if (!scanButtonDraft || (!scanButtonDraftDirty && !scanButtonRequestActive
      && JSON.stringify(scanButtonDraft) !== JSON.stringify(configuration))) {
    scanButtonDraft = copyScanButtonConfiguration(configuration);
  }
  const conflict = Boolean(scanButtonDraft && configuration && scanButtonDraft.revision !== configuration.revision);
  const problem = scanButtonConfigurationProblem(scanButtonDraft);
  const saveProblem = scanButtonConfigurationProblem(scanButtonDraft, scanButtonDraft?.enabled === true);
  findElementById("scan-button-enabled").checked = scanButtonDraft?.enabled === true;
  findElementById("scan-button-status").textContent = controlText(scanButtonStatusTranslation(snapshot?.status_code));
  findElementById("scan-button-hardware-note").hidden = snapshot?.hardware_verified === true;
  renderScanButtonProfileChoices();
  for (const action of scanButtonActions) {
    findElementById(`scan-button-${action}-note`).textContent = controlText(scanButtonBindingProblem(action, scanButtonDraft?.bindings?.[action]) || `scan_button.help.${action}`);
  }
  for (const input of findElementById("scan-button-form").querySelectorAll("input, select")) {
    input.disabled = !configuration || scanButtonRequestActive;
  }
  findElementById("scan-button-save").disabled = !configuration || scanButtonRequestActive || !scanButtonDraftDirty || Boolean(saveProblem) || conflict;
  findElementById("scan-button-reload").disabled = !configuration || scanButtonRequestActive;
  findElementById("scan-button-reload").hidden = !scanButtonDraftDirty && !conflict;
  const message = conflict ? { identifier: "scan_button.conflict", error: true }
    : problem ? { identifier: problem, error: true }
    : scanButtonLocalMessage || (scanButtonDraftDirty ? { identifier: "scan_button.unsaved", error: false } : null);
  const note = findElementById("scan-button-save-note");
  note.textContent = message ? controlText(message.identifier) : "";
  note.classList.toggle("scan-button-error", Boolean(message?.error));
  renderScanButtonLastResult(snapshot);
}

function updateScanButtonDraft(event) {
  if (!scanButtonDraft || scanButtonRequestActive) return;
  const input = event.target;
  if (input.id === "scan-button-enabled") scanButtonDraft.enabled = input.checked;
  else if (scanButtonActions.includes(input.dataset.scanButtonAction)) {
    const action = input.dataset.scanButtonAction;
    const field = input.dataset.scanButtonKind === "ocr" ? "ocr_profile_id" : "scan_profile_id";
    const binding = { ...scanButtonDraft.bindings[action] };
    if (input.value) binding[field] = input.value;
    else delete binding[field];
    if (Object.keys(binding).length) scanButtonDraft.bindings[action] = binding;
    else delete scanButtonDraft.bindings[action];
  } else return;
  scanButtonDraftDirty = true;
  scanButtonLocalMessage = null;
  renderScanButtonSettings();
}

async function saveScanButtonSettings(values, button) {
  if (scanButtonRequestActive || !controlSession?.authenticated || !controlState) return;
  const snapshot = cachedScanButtonSnapshot();
  const problem = scanButtonConfigurationProblem(values, values.enabled);
  if (!snapshot || values.revision !== snapshot.configuration.revision || problem) {
    scanButtonLocalMessage = { identifier: problem || "scan_button.conflict", error: true };
    renderScanButtonSettings();
    return;
  }
  const generation = scanButtonRequestGeneration;
  scanButtonRequestActive = true;
  const operation = performControlAction(button, async () => {
    try {
      const result = await controlRequest("/api/scan-button/settings", { method: "POST", body: values });
      if (generation !== scanButtonRequestGeneration || !controlSession?.authenticated || !controlState) return;
      const configuration = copyScanButtonConfiguration(result.scan_button?.configuration);
      if (!configuration) throw Object.assign(new Error("Invalid scan button settings response"), { code: "request_failed" });
      scanButtonLatestSnapshot = { ...result.scan_button, configuration };
      controlState.scan_button = scanButtonLatestSnapshot;
      scanButtonDraft = copyScanButtonConfiguration(configuration);
      scanButtonDraftDirty = false;
      scanButtonLocalMessage = { identifier: "scan_button.saved", error: false };
      showControlFeedback("scan_button.saved");
    } catch (error) {
      if (generation !== scanButtonRequestGeneration || !controlSession?.authenticated) return;
      const identifier = error.code === "request_conflict" ? "scan_button.conflict"
        : error.code === "invalid_profile" ? "scan_button.missing_profile_help" : errorTranslation(error);
      scanButtonLocalMessage = { identifier, error: true };
      showControlFeedback(identifier, {}, true);
      if (["request_conflict", "invalid_profile"].includes(error.code)) await refreshControlCenter();
    }
  });
  renderScanButtonSettings();
  try { await operation; }
  finally {
    if (generation === scanButtonRequestGeneration) {
      scanButtonRequestActive = false;
      renderScanButtonSettings();
    }
  }
}

function confirmScanButtonActivation(values) {
  findElementById("confirmation-title").textContent = controlText("scan_button.confirm_title");
  findElementById("confirmation-description").textContent = controlText("scan_button.confirm_help");
  findElementById("confirmation-submit").textContent = controlText("scan_button.confirm_save");
  const details = findElementById("confirmation-details");
  details.replaceChildren();
  for (const action of scanButtonActions) {
    const binding = values.bindings[action];
    if (!binding) continue;
    const names = [scanButtonProfile(binding.scan_profile_id, "scan")?.name,
      ...(action === "ocr" ? [scanButtonProfile(binding.ocr_profile_id, "ocr")?.name] : [])].filter(Boolean);
    details.append(makeElement("dt", "", controlText(`scan_button.action.${action}`)), makeElement("dd", "", names.join(" · ")));
  }
  confirmationAction = () => saveScanButtonSettings(values, findElementById("confirmation-submit"));
  findElementById("action-confirmation").showModal();
}

function clearScanButtonViews() {
  scanButtonRequestGeneration += 1;
  scanButtonDraft = null;
  scanButtonDraftDirty = false;
  scanButtonRequestActive = false;
  scanButtonLatestSnapshot = null;
  scanButtonLocalMessage = null;
  scanButtonSelectSignature = "";
  scanButtonResultDocumentIdentifier = null;
  findElementById("scan-button-enabled").checked = false;
  for (const selector of findElementById("scan-button-form").querySelectorAll("select")) selector.replaceChildren();
  for (const input of findElementById("scan-button-form").querySelectorAll("input, select, button")) input.disabled = true;
  for (const identifier of ["scan-button-status", "scan-button-save-note", "scan-button-last-action", "scan-button-last-checked", "scan-button-last-job-state"]) findElementById(identifier).textContent = "";
  for (const action of scanButtonActions) findElementById(`scan-button-${action}-note`).textContent = "";
  findElementById("scan-button-last-checked").removeAttribute("datetime");
  findElementById("scan-button-last-checked").hidden = true;
  findElementById("scan-button-reload").hidden = true;
  findElementById("scan-button-open-result").hidden = true;
}

findElementById("scan-button-form").addEventListener("change", updateScanButtonDraft);
findElementById("scan-button-form").addEventListener("submit", (event) => {
  event.preventDefault();
  if (!scanButtonDraftDirty || findElementById("scan-button-save").disabled) return;
  const values = copyScanButtonConfiguration(scanButtonDraft);
  if (values.enabled) confirmScanButtonActivation(values);
  else saveScanButtonSettings(values, findElementById("scan-button-save"));
});
findElementById("scan-button-reload").addEventListener("click", () => {
  if (scanButtonRequestActive) return;
  scanButtonDraft = copyScanButtonConfiguration(cachedScanButtonSnapshot()?.configuration);
  scanButtonDraftDirty = false;
  scanButtonLocalMessage = null;
  renderScanButtonSettings();
});
findElementById("scan-button-open-result").addEventListener("click", () => {
  if (scanButtonResultDocumentIdentifier) selectDocument(scanButtonResultDocumentIdentifier);
});
document.addEventListener("dashboardlanguagechange", renderScanButtonSettings);
renderScanButtonSettings();
