/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Reproducible browser-module checks with an isolated DOM and no network or devices. */
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const repository = require("node:path").join(__dirname, "..");
const source = fs.readFileSync(`${repository}/dcp150c/static/scan-button.js`, "utf8");
const translations = fs.readFileSync(`${repository}/dcp150c/static/translations.js`, "utf8");
const scanOne = "1".repeat(32), scanTwo = "2".repeat(32);
const textOne = "3".repeat(32), printOne = "4".repeat(32);
const originalProfiles = [
  { id: scanOne, name: "Document scan", kind: "scan" },
  { id: scanTwo, name: "Photo scan", kind: "scan" },
  { id: textOne, name: "German text", kind: "ocr" },
  { id: printOne, name: "Print preset", kind: "print" }
];
const copy = (value) => JSON.parse(JSON.stringify(value));
let checks = 0;
function passed(message) { checks += 1; console.log("PASS:", message); }

class Element {
  constructor(identifier) {
    this.id = identifier; this.children = []; this.textContent = "";
    this.value = ""; this.dataset = {}; this.listeners = {};
    this.disabled = false; this.hidden = false; this.checked = false;
    this.attributes = {};
    this.classList = { toggle: (name, value) => this.attributes[name] = value };
  }
  replaceChildren(...children) { this.children = children; this.textContent = ""; }
  append(...children) { this.children.push(...children); }
  setAttribute(name, value) { this.attributes[name] = value; }
  removeAttribute(name) { delete this.attributes[name]; }
  addEventListener(name, action) { this.listeners[name] = action; }
  showModal() { this.open = true; }
  set dateTime(value) { this.attributes.datetime = value; }
  get dateTime() { return this.attributes.datetime; }
}
class Choice {
  constructor(text, value) { this.textContent = text; this.value = value; this.disabled = false; }
}

function fixture(configuration = { revision: 0, enabled: false, bindings: {} }, authenticated = true) {
  const elements = new Map();
  const element = (identifier) => {
    if (!elements.has(identifier)) elements.set(identifier, new Element(identifier));
    return elements.get(identifier);
  };
  const selectors = ["image", "file", "email", "ocr"].map((action) => {
    const selector = element(`scan-button-${action}-profile`);
    selector.dataset = { scanButtonAction: action, scanButtonKind: "scan" };
    return selector;
  });
  const textSelector = element("scan-button-ocr-text-profile");
  textSelector.dataset = { scanButtonAction: "ocr", scanButtonKind: "ocr" };
  selectors.push(textSelector);
  const inputs = [element("scan-button-enabled"), ...selectors];
  const buttons = ["scan-button-save", "scan-button-reload", "scan-button-open-result"].map(element);
  element("scan-button-form").querySelectorAll = (selector) =>
    selector === "select" || selector === "[data-scan-button-action]" ? selectors
      : selector === "input, select" ? inputs
      : selector === "input, select, button" ? [...inputs, ...buttons] : [];
  const calls = [], feedback = [], selected = [];
  const context = vm.createContext({
    Option: Choice, findElementById: element, document: { addEventListener() {} },
    makeElement: (tag, name, text) => Object.assign(new Element(tag), { textContent: text }),
    showControlFeedback: (...values) => feedback.push(values),
    selectDocument: (identifier) => selected.push(identifier),
    controlRequest: async (path, options) => {
      calls.push({ path, method: options.method, body: copy(options.body) });
      if (context.failCode) throw Object.assign(new Error("Generated failure"), { code: context.failCode });
      if (context.defer) return await new Promise((resolve) => { context.resolveRequest = resolve; });
      return { scan_button: {
        configuration: { ...copy(options.body), revision: options.body.revision + 1 },
        status_code: options.body.enabled ? "scan_button_waiting" : "scan_button_disabled",
        hardware_verified: false, last_checked_at: null, last_action: null, last_job_id: null
      } };
    },
    refreshControlCenter: async () => { context.refreshCount += 1; },
    errorTranslation: (error) => "error." + error.code,
    performControlAction: async (button, action) => {
      if (button?.disabled) return;
      if (button) button.disabled = true;
      try { await action(); } finally { if (button) button.disabled = false; }
    },
    startDocumentJob: () => { throw Error("No job endpoint may be used"); }
  });
  vm.runInContext(translations, context);
  context.controlState = {
    scan_button: { configuration: copy(configuration),
      status_code: configuration.enabled ? "scan_button_ready" : "scan_button_disabled",
      hardware_verified: false, last_checked_at: null, last_action: null, last_job_id: null },
    profiles: copy(originalProfiles), jobs: [], documents: []
  };
  context.controlSession = { authenticated };
  context.selectedLanguage = "en";
  context.confirmationAction = null;
  context.refreshCount = 0;
  vm.runInContext(`function controlText(identifier, parameters = {}) {
    let text = dashboardLanguages[selectedLanguage].messages[identifier]
      || dashboardLanguages.en.messages[identifier] || identifier;
    for (const [key, value] of Object.entries(parameters)) text = text.replaceAll("{" + key + "}", value);
    return text;
  }`, context);
  vm.runInContext(source, context);
  const evaluate = (code) => vm.runInContext(code, context);
  const change = (identifier, value) => {
    const input = element(identifier);
    if (identifier === "scan-button-enabled") input.checked = value;
    else input.value = value;
    element("scan-button-form").listeners.change({ target: input });
  };
  const submit = () => element("scan-button-form").listeners.submit({ preventDefault() {} });
  return { context, element, selectors, calls, feedback, selected, evaluate, change, submit };
}
const drain = () => new Promise((resolve) => setImmediate(resolve));

(async () => {
  let test = fixture(undefined, false);
  assert.equal(test.calls.length, 0);
  assert(test.selectors.every((selector) => selector.children.length === 0 && selector.disabled));
  test.context.controlSession.authenticated = true;
  test.evaluate("renderScanButtonSettings()");
  for (let index = 0; index < 4; index += 1) {
    assert.deepEqual(test.selectors[index].children.map((choice) => choice.value), ["", scanOne, scanTwo]);
  }
  assert.deepEqual(test.selectors[4].children.map((choice) => choice.value), ["", textOne]);
  for (let index = 0; index < 3; index += 1) test.evaluate("renderScanButtonSettings()");
  assert.equal(test.calls.length, 0);
  passed("Initial rendering, authentication and repeated cached renders make no requests; each selector offers only the correct profile type");

  test.change("scan-button-image-profile", scanOne);
  test.context.controlState.scan_button.last_checked_at = "2026-10-28T12:00:00Z";
  test.context.controlState.scan_button.status_code = "scan_button_waiting";
  test.context.selectedLanguage = "de";
  test.evaluate("renderScanButtonSettings()");
  assert.equal(test.element("scan-button-image-profile").value, scanOne);
  assert.equal(test.evaluate("scanButtonDraftDirty"), true);
  assert.equal(test.calls.length, 0);
  assert(test.element("scan-button-save-note").textContent.includes("noch nicht gespeichert"));
  passed("Unsaved selection survives status polling and a language change without saving or probing");

  test.change("scan-button-enabled", true); test.submit();
  assert.equal(test.calls.length, 0);
  assert.equal(test.element("action-confirmation").open, true);
  assert(test.element("confirmation-description").textContent.includes("späterer bewusster Druck"));
  await test.context.confirmationAction();
  assert.equal(test.calls.length, 1);
  assert.equal(test.calls[0].path, "/api/scan-button/settings");
  assert.deepEqual(test.calls[0].body, { revision: 0, enabled: true, bindings: { image: { scan_profile_id: scanOne } } });
  assert.equal(test.evaluate("scanButtonDraftDirty"), false);
  assert.equal(test.evaluate("scanButtonDraft.revision"), 1);
  passed("Activation requires the shared confirmation dialog and sends one settings POST with only assigned actions, without starting a job");

  test = fixture(); test.change("scan-button-enabled", true);
  assert.equal(test.element("scan-button-save").disabled, true);
  test.submit(); assert.equal(test.calls.length, 0);
  assert(test.element("scan-button-save-note").textContent.includes("at least one"));
  passed("Enabling an empty mapping is blocked with a readable explanation");

  test = fixture(); test.change("scan-button-ocr-profile", scanOne);
  assert.equal(test.element("scan-button-save").disabled, true);
  assert(test.element("scan-button-ocr-note").textContent.includes("both"));
  test.change("scan-button-ocr-text-profile", textOne);
  test.change("scan-button-enabled", true); test.submit();
  await test.context.confirmationAction();
  assert.deepEqual(test.calls[0].body.bindings, { ocr: { scan_profile_id: scanOne, ocr_profile_id: textOne } });
  passed("Text recognition requires a scan and OCR profile together and saves the complete pair");

  test = fixture({ revision: 5, enabled: true, bindings: { image: { scan_profile_id: scanOne } } });
  test.context.controlState.profiles = test.context.controlState.profiles.filter((profile) => profile.id !== scanOne);
  test.evaluate("renderScanButtonSettings()");
  assert.equal(test.element("scan-button-image-profile").value, scanOne);
  assert(test.element("scan-button-image-profile").children.some((choice) => choice.value === scanOne
    && choice.disabled && choice.textContent.includes("no longer available")));
  test.change("scan-button-file-profile", scanTwo);
  assert.equal(test.element("scan-button-save").disabled, true);
  test.change("scan-button-enabled", false);
  assert.equal(test.element("scan-button-save").disabled, false);
  test.submit(); await drain();
  assert.equal(test.calls.length, 1); assert.equal(test.calls[0].body.enabled, false);
  assert.equal(test.calls[0].body.bindings.image.scan_profile_id, scanOne);
  passed("Deleted profiles stay visibly selected and never fall back; reception can still be disabled with their old assignments");

  test = fixture(); test.change("scan-button-file-profile", scanOne);
  test.context.controlState.scan_button.configuration = { revision: 1, enabled: false, bindings: { file: { scan_profile_id: scanTwo } } };
  test.evaluate("renderScanButtonSettings()");
  assert.equal(test.element("scan-button-file-profile").value, scanOne);
  assert.equal(test.element("scan-button-save").disabled, true);
  assert(test.element("scan-button-save-note").textContent.includes("another window"));
  test.element("scan-button-reload").listeners.click();
  assert.equal(test.element("scan-button-file-profile").value, scanTwo);
  assert.equal(test.evaluate("scanButtonDraftDirty"), false);
  assert.equal(test.calls.length, 0);
  passed("A changed server revision preserves the draft and requires explicit loading of the newer saved assignments");

  test = fixture(); test.change("scan-button-file-profile", scanOne); test.submit(); await drain();
  assert.equal(test.evaluate("scanButtonDraft.revision"), 1);
  test.context.controlState.scan_button = { configuration: { revision: 0, enabled: false, bindings: {} }, status_code: "scan_button_disabled" };
  test.evaluate("renderScanButtonSettings()");
  assert.equal(test.evaluate("scanButtonDraft.revision"), 1);
  assert.equal(test.element("scan-button-file-profile").value, scanOne);
  assert.equal(test.calls.length, 1);
  passed("A stale state response arriving after a successful save cannot roll back the new revision or profile selection");

  test = fixture(); test.change("scan-button-image-profile", scanOne);
  test.change("scan-button-enabled", true); test.submit();
  test.context.controlState.profiles = [];
  await test.context.confirmationAction();
  assert.equal(test.calls.length, 0);
  assert.equal(test.element("scan-button-save").disabled, true);
  passed("A profile removed while the confirmation dialog is open is revalidated before any POST");

  test = fixture(undefined, false); test.context.controlSession.authenticated = true;
  test.context.controlState.jobs = [{ id: "job", state: "succeeded", result_document_id: "document" }];
  test.context.controlState.documents = [{ id: "document", name: "Generated result" }];
  test.context.controlState.scan_button.last_job_id = "job";
  test.context.controlState.scan_button.last_action = "image";
  test.context.controlState.scan_button.last_checked_at = "2026-10-28T12:00:00Z";
  test.evaluate("renderScanButtonSettings()");
  assert.equal(test.element("scan-button-open-result").hidden, false);
  assert.equal(test.element("scan-button-open-result").disabled, false);
  test.element("scan-button-open-result").listeners.click();
  assert.deepEqual(test.selected, ["document"]);
  test.context.controlState.documents = []; test.evaluate("renderScanButtonSettings()");
  assert.equal(test.element("scan-button-open-result").hidden, true);
  test.context.controlSession.authenticated = false; test.evaluate("clearScanButtonViews()");
  assert(test.selectors.every((selector) => selector.children.length === 0 && selector.disabled));
  for (const identifier of ["scan-button-status", "scan-button-save-note", "scan-button-last-action", "scan-button-last-checked", "scan-button-last-job-state"]) {
    assert.equal(test.element(identifier).textContent, "");
  }
  assert.equal(test.evaluate("scanButtonResultDocumentIdentifier"), null);
  assert.equal(test.element("scan-button-last-checked").dateTime, undefined);
  passed("Existing last results open Documents, removed results have no dead button and logout clears choices, names, timestamps and identifiers");

  test = fixture(); test.change("scan-button-image-profile", scanOne); test.context.defer = true;
  const pending = test.evaluate('saveScanButtonSettings(copyScanButtonConfiguration(scanButtonDraft), findElementById("scan-button-save"))');
  assert.equal(test.calls.length, 1);
  test.context.controlSession.authenticated = false; test.evaluate("clearScanButtonViews()");
  test.context.resolveRequest({ scan_button: { configuration: { revision: 1, enabled: false,
    bindings: { image: { scan_profile_id: scanOne } } }, status_code: "scan_button_disabled" } });
  await pending;
  assert.equal(test.evaluate("scanButtonDraft"), null);
  assert.equal(test.evaluate("scanButtonLatestSnapshot"), null);
  assert.equal(test.feedback.length, 0);
  passed("A save response arriving after logout cannot restore private form state or report a stale success");

  test = fixture(); test.change("scan-button-image-profile", scanOne);
  test.context.failCode = "request_conflict"; test.submit(); await drain();
  assert.equal(test.calls.length, 1); assert.equal(test.context.refreshCount, 1);
  assert.equal(test.evaluate("scanButtonDraftDirty"), true);
  assert.equal(test.element("scan-button-image-profile").value, scanOne);
  assert(test.element("scan-button-save-note").textContent.includes("another window"));
  passed("A server-side revision conflict refreshes metadata once, keeps the draft and never retries a settings POST automatically");

  const codes = ["disabled", "waiting", "ready", "busy", "running", "unassigned", "status_invalid",
    "not_found", "selection_required", "access_denied", "device_changed", "lock_unavailable", "timeout", "status_failed", "invalid_response"];
  const identifiers = [...fs.readFileSync(`${repository}/dcp150c/static/index.html`, "utf8")
    .matchAll(/data-translation(?:-[a-z]+)?="([^"]+)"/g)].map((match) => match[1]);
  for (const language of ["de", "en"]) {
    test.context.selectedLanguage = language;
    for (const code of codes) {
      assert.notEqual(test.evaluate(`scanButtonStatusTranslation('scan_button_${code}')`), "scan_button.unavailable");
    }
    for (const identifier of identifiers) {
      assert(test.evaluate(`Boolean(dashboardLanguages.${language}.messages[${JSON.stringify(identifier)}])`), identifier);
    }
  }
  passed("All declared button status codes and every HTML translation binding exist in both German and English");
  assert.equal(checks, 13);
  console.log(`Completed ${checks} focused VM workflow checks; no network or device access was possible.`);
})().catch((error) => { console.error(error); process.exitCode = 1; });
