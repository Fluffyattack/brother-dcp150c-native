/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Exercise the actual browser request code with delayed fetch responses.
 * No HTTP service, printer queue or scanner is contacted by these checks.
 */
"use strict";

const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const vm = require("node:vm");
const { webcrypto } = require("node:crypto");

const translations = readFileSync(join(__dirname, "../dcp150c/static/translations.js"), "utf8");
const dashboardPrefix = readFileSync(join(__dirname, "../dcp150c/static/app.js"), "utf8")
  .split("function matchSupportedLanguage")[0];

const script = readFileSync(join(__dirname, "../dcp150c/static/control-center.js"), "utf8")
  .split("// Keyboard navigation follows")[0];

function createBrowser(storage, fetchImplementation, storageBlocked = false) {
  const elements = {};
  const element = (identifier) => elements[identifier] ||= { textContent: "", hidden: false, dataset: {}, classList: { toggle() {} } };
  const context = vm.createContext({
    document: { getElementById: element },
    Option: function Option(text, value) { this.text = text; this.label = text; this.value = value; },
    Headers, Response, Blob, AbortController, setTimeout, clearTimeout,
    crypto: webcrypto,
    sessionStorage: {
      getItem(key) { return storage.get(key) || null; },
      setItem(key, value) {
        if (storageBlocked) throw new Error("Website storage is blocked");
        storage.set(key, value);
      }
    },
    fetch: fetchImplementation,
  });
  vm.runInContext(translations, context);
  vm.runInContext(dashboardPrefix, context);
  vm.runInContext(script, context);
  vm.runInContext('controlSession = { authenticated: true, csrf_token: "test-session-token" }; clearPrivateViews = () => {}; renderControlAccess = () => {};', context);
  const evaluate = (expression) => vm.runInContext(expression, context);
  evaluate.elements = elements;
  return evaluate;
}

async function main() {
  const storage = new Map();
  const receivedKeys = [];
  const acceptedJobs = new Map();
  let firstResponse = true;
  let forcedStatus = 200;
  const fakeFetch = async (path, options) => {
    if (path === "/api/session") return new Response(JSON.stringify({ authenticated: false, configured: true, csrf_token: "fresh-test-session-token" }));
    const key = options.headers.get("Idempotency-Key");
    receivedKeys.push({ path, key });
    if (forcedStatus !== 200) return new Response(JSON.stringify({ error: "processing_failed", message_code: "processing_failed" }), { status: forcedStatus });
    if (!acceptedJobs.has(key)) acceptedJobs.set(key, { id: "job-" + (acceptedJobs.size + 1) });
    const result = { job: acceptedJobs.get(key) };
    if (firstResponse) {
      firstResponse = false;
      // The server accepted the job, but its response arrives after the browser
      // times out. The accepted job remains in the server's durable registry.
      return new Promise((resolve, reject) => {
        const delayedResponse = setTimeout(() => resolve(new Response(JSON.stringify(result))), 100);
        options.signal.addEventListener("abort", () => {
          clearTimeout(delayedResponse);
          reject(new DOMException("The response was lost", "AbortError"));
        }, { once: true });
      });
    }
    return new Response(JSON.stringify(result));
  };

  let browser = createBrowser(storage, fakeFetch);
  await assert.rejects(browser('controlRequest("/api/print", { method: "POST", body: { document_id: "example", copies: 1 }, timeout: 10 })'), (error) => error.code === "request_timeout");
  const originalKey = receivedKeys.at(-1).key;
  assert.equal(acceptedJobs.size, 1);
  assert.match(originalKey, /^[a-f0-9]{32}$/);

  // A new context models a full page reload, keeping only sessionStorage. A
  // different object-key order still denotes the same pending operation.
  browser = createBrowser(storage, fakeFetch);
  const retry = await browser('controlRequest("/api/print", { method: "POST", body: { copies: 1, document_id: "example" } })');
  assert.equal(receivedKeys.at(-1).key, originalKey);
  assert.equal(retry.job.id, "job-1");
  assert.equal(acceptedJobs.size, 1);
  console.log("PASS: a lost print response and page reload reuse the same key and accepted job");

  await browser('controlRequest("/api/print", { method: "POST", body: { document_id: "example", copies: 1 } })');
  assert.notEqual(receivedKeys.at(-1).key, originalKey);
  assert.equal(acceptedJobs.size, 2);
  console.log("PASS: an explicitly repeated print gets a new key after a known job response");

  forcedStatus = 500;
  await assert.rejects(browser('controlRequest("/api/scan", { method: "POST", body: { resolution: 300, mode: "color" } })'));
  const uncertainKey = receivedKeys.at(-1).key;
  forcedStatus = 401;
  await assert.rejects(browser('controlRequest("/api/scan", { method: "POST", body: { mode: "color", resolution: 300 } })'));
  assert.equal(receivedKeys.at(-1).key, uncertainKey);
  forcedStatus = 200;
  await browser('controlRequest("/api/scan", { method: "POST", body: { resolution: 300, mode: "color" } })');
  assert.equal(receivedKeys.at(-1).key, uncertainKey);
  console.log("PASS: server errors and expired authentication retain uncertain operation keys");

  const countBeforeBlockedRequest = receivedKeys.length;
  const blockedBrowser = createBrowser(new Map(), fakeFetch, true);
  await assert.rejects(blockedBrowser('controlRequest("/api/copy", { method: "POST", body: { copies: 1 } })'), (error) => error.code === "request_storage_unavailable");
  assert.equal(receivedKeys.length, countBeforeBlockedRequest);
  console.log("PASS: a device operation is refused before submission when pending keys cannot be saved");

  await browser('controlRequest("/api/login", { method: "POST", body: { password: "never-store-this-password" } })');
  await browser('controlRequest("/api/documents", { method: "POST", body: new Blob(["never-store-document-contents"]) })');
  const saved = JSON.stringify([...storage.values()]);
  assert.ok(!saved.includes("never-store-this-password"));
  assert.ok(!saved.includes("never-store-document-contents"));
  console.log("PASS: passwords and raw upload data never enter pending-request storage");

  for (const [route, responseField, label] of [["/api/scan-sessions", "scan_session", "new-document"], ["/api/scan-sessions/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/pages", "job", "next-page"], ["/api/scanner/preview", "job", "scanner-preview"]]) {
    const workflowStorage = new Map(), accepted = new Map(), workflowKeys = [];
    let loseResponse = true;
    const workflowFetch = async (path, options) => {
      const key = options.headers.get("Idempotency-Key");
      workflowKeys.push(key);
      if (!accepted.has(key)) accepted.set(key, { [responseField]: { id: "a".repeat(32) + accepted.size } });
      const result = accepted.get(key);
      if (loseResponse) {
        loseResponse = false;
        return new Promise((resolve, reject) => {
          const delayed = setTimeout(() => resolve(new Response(JSON.stringify(result))), 100);
          options.signal.addEventListener("abort", () => { clearTimeout(delayed); reject(new DOMException("Lost response", "AbortError")); }, { once: true });
        });
      }
      return new Response(JSON.stringify(result));
    };
    let workflowBrowser = createBrowser(workflowStorage, workflowFetch);
    const expression = `controlRequest(${JSON.stringify(route)}, { method: "POST", body: { revision: 0 }, timeout: 10 })`;
    await assert.rejects(workflowBrowser(expression), (error) => error.code === "request_timeout");
    workflowBrowser = createBrowser(workflowStorage, workflowFetch);
    await workflowBrowser(expression);
    assert.equal(accepted.size, 1);
    assert.equal(workflowKeys[0], workflowKeys[1]);
    await workflowBrowser(expression);
    assert.equal(accepted.size, 2);
    console.log(`PASS: a lost ${label} response survives reload without duplicating work`);
  }

  const scanningSource = readFileSync(join(__dirname, "../dcp150c/static/scan-sessions.js"), "utf8");
  const scannerContext = vm.createContext({});
  vm.runInContext(scanningSource.slice(0, scanningSource.indexOf('findElementById("jump-to-scan-sessions")')), scannerContext);
  vm.runInContext('var restoredSettings = []; function findElementById() { return {}; } function applyFormSettings(form, settings) { restoredSettings.push(settings); }', scannerContext);
  vm.runInContext('var session = { id: "example", settings: { mode: "gray", resolution: 100 } }; restoreScanSessionSettings(session, false);', scannerContext);
  assert.equal(vm.runInContext('restoredSettings.length', scannerContext), 0);
  vm.runInContext('restoreScanSessionSettings(session, true); restoreScanSessionSettings(session, true);', scannerContext);
  assert.equal(vm.runInContext('restoredSettings.length', scannerContext), 1);
  assert.equal(vm.runInContext('restoredSettings[0].resolution', scannerContext), 100);
  console.log("PASS: saved scan settings survive an unavailable scanner and restore once after discovery");
  assert.equal(browser('scannerReadinessMessage({available:false,configured:false,reason_code:"scanner_not_checked"})'),"scan.needs_check");
  assert.equal(browser('scannerReadinessMessage({available:false,configured:false,reason_code:"scanner_software_missing"})'),"scan.not_configured");
  assert.equal(browser('scannerReadinessMessage({available:false,configured:false,reason_code:"scanner_not_found"})'),"scan.unavailable");
  assert.equal(browser('scannerReadinessMessage({available:true})'),"scan.available");
  console.log("PASS: an unchecked scanner is distinguished from missing software and an unavailable device");

  let anonymousSessions = 0;
  let submittedLoginToken = null;
  const loginStorage = new Map();
  const authBrowser = createBrowser(loginStorage, async (path, options) => {
    if (path === "/api/control-center") return new Response(JSON.stringify({ error: "authentication_required", message_code: "authentication_required" }), { status: 401 });
    if (path === "/api/session") {
      anonymousSessions += 1;
      return new Response(JSON.stringify({ authenticated: false, configured: true, csrf_token: "fresh-session-" + anonymousSessions }));
    }
    assert.equal(path, "/api/login");
    submittedLoginToken = options.headers.get("X-CSRF-Token");
    assert.equal(JSON.parse(options.body).password, "private-test-password");
    return new Response(JSON.stringify({ authenticated: true, configured: true, csrf_token: "signed-in-session" }));
  });
  await assert.rejects(authBrowser('controlRequest("/api/control-center")'), (error) => error.code === "authentication_required");
  assert.equal(authBrowser("controlSession.csrf_token"), "fresh-session-1");
  assert.equal(authBrowser("controlSession.authenticated"), false);
  console.log("PASS: an expired session fetches a fresh anti-forgery token before showing sign-in again");

  await authBrowser('signInControlCenter("private-test-password")');
  assert.equal(anonymousSessions, 2);
  assert.equal(submittedLoginToken, "fresh-session-2");
  assert.equal(authBrowser("controlSession.authenticated"), true);
  assert.ok(!JSON.stringify([...loginStorage.values()]).includes("private-test-password"));
  console.log("PASS: the production sign-in helper refreshes an old anonymous session and uses its new token");

  authBrowser('showControlFeedback("jobs.accepted"); selectedLanguage = "de"; rerenderControlMessages()');
  assert.equal(authBrowser.elements["control-feedback"].textContent, "Dein Auftrag wurde angenommen. Den Verlauf siehst du unten.");
  console.log("PASS: cached operation feedback is translated again when the language changes");

  const select = (initial) => ({
    value: initial, options: [],
    replaceChildren() { this.options = []; this.value = ""; },
    append(option) { this.options.push(option); if (this.options.length === 1) this.value = option.value; }
  });
  authBrowser.elements["print-form"] = { dataset: {}, elements: {
    paper_size: select("A4"), media: select("Plain"), quality: select("Normal")
  } };
  authBrowser('controlState = { capabilities: { print: { papers: ["A4"], media: ["Plain", "Glossy"], media_qualities: { Plain: ["Normal"], Glossy: ["Photo"] } } } }; selectedLanguage = "en"; updateSupportedPrintOptions()');
  assert.equal(authBrowser.elements["print-form"].elements.media.options[1].label, "Glossy photo paper");
  authBrowser('selectedLanguage = "de"; updateSupportedPrintOptions()');
  assert.equal(authBrowser.elements["print-form"].elements.media.options[1].label, "Glänzendes Fotopapier");
  console.log("PASS: paper-type choices are rebuilt with translated labels after a language change");
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
