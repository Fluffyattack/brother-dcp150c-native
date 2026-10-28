/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Exercise the real UI module and request helper in a simulated browser.
 * All HTTP replies are constructed in memory. No service, scanner, executable
 * or network interface is contacted or started by this verification.
 */
"use strict";

const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const { webcrypto } = require("node:crypto");
const vm = require("node:vm");

const staticDirectory = join(__dirname, "../dcp150c/static");
const translations = readFileSync(join(staticDirectory, "translations.js"), "utf8");
const dashboardPrefix = readFileSync(join(staticDirectory, "app.js"), "utf8").split("function matchSupportedLanguage")[0];
const controlSource = readFileSync(join(staticDirectory, "control-center.js"), "utf8");
const controlPrefix = controlSource.split("// Keyboard navigation follows")[0];
const sharingSource = readFileSync(join(staticDirectory, "network-scanner.js"), "utf8");
const pageSource = readFileSync(join(staticDirectory, "index.html"), "utf8");
const copy = (value) => JSON.parse(JSON.stringify(value));

class BrowserElement {
  constructor(identifier) {
    this.id = identifier;
    this.value = "";
    this.checked = false;
    this.disabled = false;
    this.hidden = false;
    this.textContent = "";
    this.children = [];
    this.listeners = {};
    this.dataset = {};
    this.attributes = {};
    this.classList = { toggle: (name, value) => { this.attributes[name] = value; } };
  }
  replaceChildren(...children) { this.children = children; this.textContent = ""; }
  append(...children) { this.children.push(...children); }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  setAttribute(name, value) { this.attributes[name] = value; }
  removeAttribute(name) { delete this.attributes[name]; }
  querySelectorAll() { return []; }
  showModal() { this.open = true; }
  close() { this.open = false; }
}

class BrowserOption {
  constructor(text, value) { this.textContent = text; this.value = value; this.disabled = false; }
}

function createBrowser(configuration = {}, snapshotOverrides = {}, authenticated = true) {
  let serverSnapshot = {
    configuration: { revision: 0, enabled: false, interface: "", port: 8090, ...configuration },
    available: true, status_code: "network_scanner_disabled", hardware_verified: false,
    interfaces: [{ name: "wlan0", addresses: ["192.0.2.10", "2001:db8::10"] },
      { name: "eth0", addresses: ["198.51.100.20"] }],
    active_operation: null, last_exit_code: null, ...snapshotOverrides
  };
  const elements = new Map(), requests = [], sessionStorage = new Map();
  const element = (identifier) => {
    if (!elements.has(identifier)) elements.set(identifier, new BrowserElement(identifier));
    return elements.get(identifier);
  };
  const inputs = ["network-scanner-enabled", "network-scanner-interface", "network-scanner-port"].map(element);
  const buttons = ["network-scanner-save", "network-scanner-reload", "network-scanner-restart"].map(element);
  element("network-scanner-form").querySelectorAll = (selector) => selector === "input, select"
    ? inputs : selector === "input, select, button" ? [...inputs, ...buttons] : [];
  const response = (value, status = 200) => new Response(JSON.stringify(value), {
    status, headers: { "Content-Type": "application/json" }
  });
  const context = vm.createContext({
    document: { getElementById: element, createElement: (tag) => new BrowserElement(tag), addEventListener() {} },
    Option: BrowserOption, Headers, Response, Blob, AbortController, setTimeout, clearTimeout, crypto: webcrypto,
    sessionStorage: { getItem: (key) => sessionStorage.get(key) || null, setItem: (key, value) => sessionStorage.set(key, value) },
    fetch: async (path, options) => {
      const body = options.body ? JSON.parse(options.body) : null;
      requests.push({ path, method: options.method, body, csrf: options.headers.get("X-CSRF-Token") });
      if (path === "/api/session") return response({ authenticated: false, csrf_token: "fresh-anonymous-token", configured: true });
      if (path === "/api/control-center") return response({ network_scanner: copy(serverSnapshot), jobs: [], documents: [] });
      assert(["/api/network-scanner/settings", "/api/network-scanner/restart"].includes(path), "Unexpected endpoint: " + path);
      assert.equal(options.method, "POST");
      if (context.failureCode) return response({ message_code: context.failureCode }, context.failureStatus || 400);
      if (context.deferResponse) return await new Promise((resolve) => { context.releaseResponse = resolve; });
      if (body.revision !== serverSnapshot.configuration.revision) return response({ message_code: "request_conflict" }, 409);
      if (path.endsWith("/settings")) {
        serverSnapshot = { ...serverSnapshot,
          configuration: { ...body, revision: body.revision + 1 },
          status_code: body.enabled ? "network_scanner_starting" : "network_scanner_disabled" };
      } else {
        serverSnapshot = { ...serverSnapshot, status_code: "network_scanner_starting", active_operation: null };
      }
      return response({ network_scanner: copy(serverSnapshot) });
    }
  });
  vm.runInContext(translations, context);
  vm.runInContext(dashboardPrefix, context);
  vm.runInContext(controlPrefix, context);
  context.initialState = { network_scanner: copy(serverSnapshot), jobs: [], documents: [] };
  vm.runInContext(`controlState = initialState; controlSession = { authenticated: ${authenticated}, csrf_token: "fixture-session-token" };
    updateScannerControls = () => {};
    renderControlAccess = () => {};
    clearPrivateViews = () => {
      clearNetworkScannerViews();
      for (const identifier of ["confirmation-title", "confirmation-description", "confirmation-details"]) findElementById(identifier).replaceChildren();
      confirmationAction = null;
    };
    refreshControlCenter = async () => {
      controlState = await controlRequest("/api/control-center");
      renderNetworkScannerSettings();
    };`, context);
  vm.runInContext(sharingSource, context);
  const evaluate = (expression) => vm.runInContext(expression, context);
  const change = (identifier, value) => {
    const input = element(identifier);
    if (identifier === "network-scanner-enabled") input.checked = value;
    else input.value = value;
    if (identifier === "network-scanner-port") input.listeners.input({ target: input });
    else element("network-scanner-form").listeners.change({ target: input });
  };
  const submit = () => element("network-scanner-form").listeners.submit({ preventDefault() {} });
  const publish = (overrides = {}) => {
    serverSnapshot = { ...serverSnapshot, ...copy(overrides) };
    context.publishedSnapshot = copy(serverSnapshot);
    evaluate("controlState.network_scanner = publishedSnapshot; renderNetworkScannerSettings()");
  };
  return { context, element, evaluate, change, submit, publish, requests, sessionStorage, response,
    snapshot: () => copy(serverSnapshot), replaceServer: (value) => { serverSnapshot = copy(value); } };
}

const drain = () => new Promise((resolve) => setImmediate(resolve));
let completed = 0;
function passed(message) { completed += 1; console.log("PASS:", message); }

async function main() {
  let browser = createBrowser({}, {}, false);
  assert.equal(browser.element("network-scanner-interface").children.length, 0);
  browser.evaluate("controlSession.authenticated = true; renderNetworkScannerSettings()");
  assert.deepEqual(browser.element("network-scanner-interface").children.map((choice) => choice.value), ["", "wlan0", "eth0"]);
  assert.equal(browser.element("network-scanner-port").value, "8090");
  assert.equal(browser.element("network-scanner-enabled").checked, false);
  for (const language of ["de", "en"]) {
    browser.evaluate(`selectedLanguage = '${language}'; renderNetworkScannerSettings()`);
  }
  assert.equal(browser.requests.length, 0);
  passed("Startup, login rendering and language changes use cached state, retain the off-by-default port 8090 configuration and expose only known connections");

  browser.change("network-scanner-interface", "wlan0");
  browser.change("network-scanner-port", "80");
  browser.publish({ status_code: "network_scanner_starting" });
  assert.equal(browser.element("network-scanner-port").value, "80");
  assert.equal(browser.element("network-scanner-interface").value, "wlan0");
  assert.equal(browser.element("network-scanner-save").disabled, true);
  assert.equal(browser.requests.length, 0);
  passed("An unfinished port and unsaved interface choice survive status polling without being overwritten or submitted");

  for (const invalid of ["", "1023", "65536", "8090.5", "eight"]) {
    browser.change("network-scanner-port", invalid);
    assert.equal(browser.element("network-scanner-save").disabled, true);
  }
  for (const valid of ["1024", "65535", "8090"]) {
    browser.change("network-scanner-port", valid);
    assert.equal(browser.element("network-scanner-save").disabled, false);
  }
  passed("Port validation accepts both supported boundaries and rejects empty, fractional, nonnumeric and out-of-range values");

  browser.change("network-scanner-enabled", true);
  browser.submit();
  assert.equal(browser.requests.length, 0);
  assert.equal(browser.element("action-confirmation").open, true);
  assert(browser.element("confirmation-description").textContent.includes("without your Dashboard password"));
  assert(browser.element("confirmation-description").textContent.includes("rather than Documents"));
  browser.element("action-confirmation").close();
  browser.evaluate("confirmationAction = null");
  assert.equal(browser.requests.length, 0);
  browser.submit();
  await browser.evaluate("confirmationAction()");
  assert.equal(browser.requests.length, 1);
  assert.deepEqual(browser.requests[0], { path: "/api/network-scanner/settings", method: "POST",
    body: { revision: 0, enabled: true, interface: "wlan0", port: 8090 }, csrf: "fixture-session-token" });
  assert.equal(browser.sessionStorage.size, 0);
  passed("Enabling requires the existing confirmation, explains password-free LAN access and app-owned files, and saves exactly one protected settings request without a scan job");

  browser = createBrowser({}, { available: false, status_code: "network_scanner_missing", interfaces: [] });
  browser.change("network-scanner-enabled", true);
  assert.equal(browser.element("network-scanner-save").disabled, true);
  assert(browser.element("network-scanner-save-note").textContent.includes("not ready"));
  browser.publish({ available: true });
  assert(browser.element("network-scanner-save-note").textContent.includes("No suitable home network"));
  assert.equal(browser.element("network-scanner-save").disabled, true);
  browser.publish({ interfaces: [{ name: "wlan0", addresses: ["192.0.2.10"] }] });
  browser.change("network-scanner-interface", "unlisted0");
  assert.equal(browser.element("network-scanner-save").disabled, true);
  assert(browser.element("network-scanner-interface").children.some((option) => option.value === "unlisted0" && option.disabled));
  assert.equal(browser.requests.length, 0);
  passed("A missing installation, empty LAN list and unknown connection block activation with explanatory feedback and no invented fallback connection");

  browser = createBrowser({ revision: 4, enabled: true, interface: "removed0" },
    { available: false, interfaces: [], status_code: "network_scanner_missing" });
  assert.equal(browser.element("network-scanner-interface").value, "removed0");
  assert(browser.element("network-scanner-interface").children.some((option) => option.value === "removed0" && option.disabled));
  browser.change("network-scanner-enabled", false);
  assert.equal(browser.element("network-scanner-save").disabled, false);
  browser.submit(); await drain();
  assert.equal(browser.requests.length, 1);
  assert.deepEqual(browser.requests[0].body, { revision: 4, enabled: false, interface: "removed0", port: 8090 });
  passed("Sharing can be switched off while its installed helper and saved interface are both missing, without changing the stored interface silently");

  browser = createBrowser();
  browser.change("network-scanner-interface", "wlan0");
  browser.change("network-scanner-port", "8091");
  browser.publish({ configuration: { revision: 1, enabled: false, interface: "eth0", port: 9000 } });
  assert.equal(browser.element("network-scanner-interface").value, "wlan0");
  assert.equal(browser.element("network-scanner-port").value, "8091");
  assert.equal(browser.element("network-scanner-save").disabled, true);
  assert(browser.element("network-scanner-save-note").textContent.includes("another window"));
  browser.element("network-scanner-reload").listeners.click();
  assert.equal(browser.element("network-scanner-interface").value, "eth0");
  assert.equal(browser.element("network-scanner-port").value, "9000");
  assert.equal(browser.requests.length, 0);
  passed("A new server revision preserves the draft and replaces it only after explicitly loading the saved settings");

  browser = createBrowser();
  browser.change("network-scanner-interface", "wlan0");
  browser.change("network-scanner-port", "8091");
  browser.replaceServer({ ...browser.snapshot(), configuration: { revision: 2, enabled: false, interface: "eth0", port: 9000 } });
  browser.submit(); await drain();
  assert.deepEqual(browser.requests.map((request) => request.path), ["/api/network-scanner/settings", "/api/control-center"]);
  assert.equal(browser.element("network-scanner-interface").value, "wlan0");
  assert.equal(browser.element("network-scanner-port").value, "8091");
  assert.equal(browser.evaluate("networkScannerDraftDirty"), true);
  assert.equal(browser.element("network-scanner-save").disabled, true);
  passed("The actual request helper handles HTTP 409 by refreshing metadata once, retaining the draft and never repeating the settings POST automatically");

  browser = createBrowser();
  browser.change("network-scanner-interface", "wlan0"); browser.submit(); await drain();
  assert.equal(browser.evaluate("networkScannerDraft.revision"), 1);
  browser.publish({ configuration: { revision: 0, enabled: false, interface: "", port: 8090 } });
  assert.equal(browser.evaluate("networkScannerDraft.revision"), 1);
  assert.equal(browser.element("network-scanner-interface").value, "wlan0");
  assert.equal(browser.requests.length, 1);
  passed("A state response older than a successful save cannot roll back its configuration revision or selected connection");

  browser = createBrowser({ revision: 3, enabled: true, interface: "wlan0", port: 9000 }, { status_code: "network_scanner_failed" });
  assert.equal(browser.element("network-scanner-save").disabled, true);
  assert.equal(browser.element("network-scanner-restart").disabled, false);
  browser.element("network-scanner-restart").listeners.click({ currentTarget: browser.element("network-scanner-restart") });
  await drain();
  assert.equal(browser.requests.length, 1);
  assert.deepEqual(browser.requests[0].body, { revision: 3 });
  assert.equal(browser.requests[0].path, "/api/network-scanner/restart");
  assert.equal(browser.element("network-scanner-port").value, "9000");
  assert.equal(browser.evaluate("networkScannerDraftDirty"), false);
  for (let iteration = 0; iteration < 3; iteration += 1) browser.evaluate("renderNetworkScannerSettings()");
  assert.equal(browser.requests.length, 1);
  passed("A clean enabled configuration can restart after failure using only its saved revision, without resaving settings, resetting the port or automatically retrying");

  browser = createBrowser({ enabled: true, interface: "wlan0" }, { status_code: "network_scanner_ready" });
  for (const active of ["metadata", "session"]) {
    browser.publish({ active_operation: active });
    assert.equal(browser.element("network-scanner-restart").disabled, true);
    assert(browser.element("network-scanner-active-operation").textContent.length > 0);
  }
  browser.change("network-scanner-enabled", false);
  assert.equal(browser.element("network-scanner-save").disabled, false);
  assert.equal(browser.element("network-scanner-restart").disabled, true);
  browser.submit(); await drain();
  assert.equal(browser.requests[0].path, "/api/network-scanner/settings");
  assert.equal(browser.requests[0].body.enabled, false);
  passed("Active metadata and scan sessions explain why restart is unavailable while still allowing explicit deactivation");

  browser = createBrowser({ enabled: true, interface: "wlan0" }, { status_code: "network_scanner_failed" });
  browser.change("network-scanner-port", "8091");
  assert.equal(browser.element("network-scanner-restart").disabled, true);
  browser.element("network-scanner-reload").listeners.click();
  assert.equal(browser.element("network-scanner-restart").disabled, false);
  browser.publish({ available: false });
  assert.equal(browser.element("network-scanner-restart").disabled, true);
  browser.publish({ available: true, configuration: { revision: 1, enabled: false, interface: "wlan0", port: 8090 } });
  assert.equal(browser.element("network-scanner-restart").disabled, true);
  assert.equal(browser.requests.length, 0);
  passed("Restart does not bypass unsaved choices, an unavailable installation or an explicitly disabled configuration");

  browser = createBrowser();
  browser.change("network-scanner-interface", "wlan0");
  browser.context.deferResponse = true;
  const pending = browser.evaluate('performNetworkScannerRequest("/api/network-scanner/settings", networkScannerSettingsValues(), findElementById("network-scanner-save"))');
  assert.equal(browser.requests.length, 1);
  browser.evaluate("controlSession.authenticated = false; clearPrivateViews()");
  browser.context.releaseResponse(browser.response({ network_scanner: { ...browser.snapshot(),
    configuration: { revision: 1, enabled: false, interface: "wlan0", port: 8090 } } }));
  await pending;
  assert.equal(browser.evaluate("networkScannerLatestSnapshot"), null);
  assert.equal(browser.evaluate("networkScannerDraft"), null);
  assert.equal(browser.element("network-scanner-interface").children.length, 0);
  assert.equal(browser.element("network-scanner-port").value, "");
  for (const identifier of ["network-scanner-status", "network-scanner-addresses", "network-scanner-interface-note", "network-scanner-active-operation", "network-scanner-save-note"]) {
    assert.equal(browser.element(identifier).textContent, "");
  }
  passed("Logout clears private network addresses, interface choices, port and state; a late save reply cannot restore them");

  browser = createBrowser(); browser.change("network-scanner-interface", "wlan0");
  browser.context.failureStatus = 401; browser.context.failureCode = "authentication_required";
  browser.submit(); await drain();
  assert.equal(browser.evaluate("controlSession.authenticated"), false);
  assert.equal(browser.element("network-scanner-interface").children.length, 0);
  assert.equal(browser.element("network-scanner-port").value, "");
  assert.equal(browser.requests.at(-1).path, "/api/session");
  passed("Expired authentication uses the actual request helper's private-view cleanup and obtains a fresh anonymous session without replaying sharing changes");

  browser = createBrowser();
  const statusCodes = ["disabled", "missing", "starting", "ready", "busy", "failed", "timed_out", "network_changed", "stopping"];
  const translationIdentifiers = [...pageSource.matchAll(/data-translation(?:-[a-z]+)?="([^"]+)"/g)].map((match) => match[1]);
  for (const language of ["de", "en"]) {
    browser.evaluate(`selectedLanguage = '${language}'`);
    for (const code of statusCodes) {
      assert.notEqual(browser.evaluate(`networkScannerStatusTranslation('network_scanner_${code}')`), "network_scanner.unavailable");
    }
    for (const identifier of translationIdentifiers) {
      assert(browser.evaluate(`Boolean(dashboardLanguages.${language}.messages[${JSON.stringify(identifier)}])`), identifier);
    }
  }
  assert.equal((pageSource.match(/role="tab"/g) || []).length, 8);
  assert(controlSource.includes('typeof clearNetworkScannerViews === "function"'));
  assert(controlSource.includes('typeof renderNetworkScannerSettings === "function"'));
  assert.equal(browser.requests.length, 0);
  passed("All declared status messages and HTML bindings exist in German and English, the shared lifecycle hooks are present and the dashboard keeps exactly eight tabs");
  console.log(`Completed ${completed} network scanner UI workflow checks without network or device access.`);
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
