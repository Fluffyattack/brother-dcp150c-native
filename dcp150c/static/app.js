/* SPDX-License-Identifier: GPL-2.0-or-later */
"use strict";

const findElementById = (elementId) => document.getElementById(elementId);
const cartridgeIdentifiers = ["c", "m", "y", "k"];
const languageStorageKey = "dcp150c.dashboard.language";
let languagePreference = "auto";
let selectedLanguage = "en";
let numberFormatter;
let percentageFormatter;
let timeFormatter;
let pluralRules;
let latestSnapshot = null;
let connectionUnavailable = false;
let requestActive = false;
let requestQueued = false;
let manualTimer;
let lastPrinterState = "";

function translate(messageIdentifier, parameters = {}) {
  const template = dashboardLanguages[selectedLanguage].messages[messageIdentifier]
    ?? dashboardLanguages.en.messages[messageIdentifier]
    ?? dashboardLanguages.en.messages["message.status_unknown"];
  return template.replace(/\{([a-zA-Z_]+)\}/g, (placeholder, name) =>
    Object.prototype.hasOwnProperty.call(parameters, name) ? String(parameters[name]) : placeholder);
}

function matchSupportedLanguage(languageTag) {
  let candidate = String(languageTag || "").replaceAll("_", "-").toLowerCase();
  while (candidate) {
    const supported = Object.keys(dashboardLanguages).find((language) => language.toLowerCase() === candidate);
    if (supported) return supported;
    const separator = candidate.lastIndexOf("-");
    if (separator < 0) break;
    candidate = candidate.slice(0, separator);
  }
  return null;
}

function browserLanguage() {
  for (const language of navigator.languages || [navigator.language]) {
    const supported = matchSupportedLanguage(language);
    if (supported) return supported;
  }
  return "en";
}

function applyLanguage(preference, remember = false) {
  languagePreference = preference === "auto" ? "auto" : matchSupportedLanguage(preference) || "auto";
  selectedLanguage = languagePreference === "auto" ? browserLanguage() : languagePreference;
  numberFormatter = new Intl.NumberFormat(selectedLanguage, { maximumFractionDigits: 0 });
  percentageFormatter = new Intl.NumberFormat(selectedLanguage, { style: "percent", maximumFractionDigits: 0 });
  timeFormatter = new Intl.DateTimeFormat(selectedLanguage, { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  pluralRules = new Intl.PluralRules(selectedLanguage);
  document.documentElement.lang = selectedLanguage;
  document.documentElement.dir = dashboardLanguages[selectedLanguage].direction || "ltr";

  for (const [attribute, destination] of [
    ["data-translation", null],
    ["data-translation-label", "aria-label"],
    ["data-translation-content", "content"],
    ["data-translation-placeholder", "placeholder"]
  ]) {
    for (const element of document.querySelectorAll(`[${attribute}]`)) {
      const text = translate(element.getAttribute(attribute), { seconds: numberFormatter.format(5) });
      if (destination) element.setAttribute(destination, text);
      else element.textContent = text;
    }
  }

  const languageSelector = findElementById("language");
  languageSelector.replaceChildren();
  languageSelector.append(new Option(translate("language.automatic"), "auto"));
  for (const [language, definition] of Object.entries(dashboardLanguages)) {
    const option = new Option(definition.name, language);
    option.lang = language;
    languageSelector.append(option);
  }
  languageSelector.value = languagePreference;
  for (const cartridge of cartridgeIdentifiers) {
    findElementById(`ink-progress-${cartridge}`).setAttribute("aria-label",
      translate("ink.level_label", { colour: translate(`ink.${cartridge}`) }));
  }

  document.dispatchEvent(new CustomEvent("dashboardlanguagechange"));

  // Switching language only renders cached data; it never starts a USB query.
  const showConnectionError = connectionUnavailable;
  lastPrinterState = "";
  if (latestSnapshot) render(latestSnapshot);
  if (showConnectionError) connectionError();
  findElementById("refresh-label").textContent = translate(
    findElementById("refresh").disabled ? "refresh.working" : "refresh.button");
  if (remember) {
    try {
      localStorage.setItem(languageStorageKey, languagePreference);
    } catch (_) {
      // The selector still works when browser storage is disabled.
    }
  }
}

function renderInk(ink, capabilities) {
  for (const cartridge of cartridgeIdentifiers) {
    const reading = ink?.[cartridge] || {};
    const value = findElementById(`ink-value-${cartridge}`);
    const progress = findElementById(`ink-progress-${cartridge}`);
    const unknown = findElementById(`ink-unknown-${cartridge}`);
    const state = reading.state || "unknown";
    const percentage = reading.percent;
    const known = typeof percentage === "number" && Number.isFinite(percentage) && percentage >= 0 && percentage <= 100;
    value.replaceChildren();
    value.classList.toggle("text-reading", !known && state !== "unknown");
    if (known) {
      for (const part of percentageFormatter.formatToParts(percentage / 100)) {
        if (part.type === "percentSign") {
          const unit = document.createElement("small");
          unit.textContent = part.value;
          value.append(unit);
        } else {
          value.append(document.createTextNode(part.value));
        }
      }
      progress.value = percentage;
      const percentageLabel = percentageFormatter.format(percentage / 100);
      progress.setAttribute("aria-valuetext", reading.granularity === "stepped"
        ? translate("ink.percentage_label", { percentage: percentageLabel }) : percentageLabel);
      findElementById(`ink-state-${cartridge}`).textContent = translate(
        reading.granularity === "stepped" ? "ink.reported_step" : "ink.reported");
    } else {
      const stateIdentifiers = { ok: "detected", low: "low", empty: "empty", missing: "missing" };
      value.textContent = state === "unknown" ? "—" : translate(`ink.${stateIdentifiers[state] || "unknown"}`);
      progress.removeAttribute("aria-valuetext");
      findElementById(`ink-state-${cartridge}`).textContent = translate(
        state === "unknown" ? "ink.unavailable" : "ink.status_reported");
    }
    progress.hidden = !known;
    unknown.hidden = known;
  }
  findElementById("ink-note").textContent = translate(capabilities?.ink_stepped
    ? "ink.steps_note" : capabilities?.ink_percent ? "ink.percent_note" : "ink.unavailable_note");
}

function renderActivity(cups) {
  cups = cups || {};
  const jobs = Array.isArray(cups.jobs) ? cups.jobs.slice(0, 20) : [];
  findElementById("queue-name").textContent = cups.queue || translate("activity.queue");
  const list = findElementById("job-list");
  list.replaceChildren();
  list.hidden = !jobs.length;
  findElementById("job-count").textContent = cups.available && cups.active !== null
    ? numberFormatter.format(jobs.length) : "—";
  let title;
  let detail;
  if (!cups.available) {
    title = translate("activity.unavailable");
    detail = translate("activity.unavailable_detail");
  } else if (cups.active === null) {
    title = translate("activity.unknown");
    detail = translate("activity.unknown_detail");
  } else if (cups.active || jobs.length) {
    const pluralCategory = pluralRules.select(jobs.length);
    const messageIdentifier = `activity.active_jobs.${pluralCategory}`;
    const available = dashboardLanguages[selectedLanguage].messages[messageIdentifier]
      ?? dashboardLanguages.en.messages[messageIdentifier];
    title = translate(available ? messageIdentifier : "activity.active_jobs.other", { count: numberFormatter.format(jobs.length) });
    detail = translate("activity.usb_in_use");
  } else {
    title = translate("activity.no_jobs");
    detail = translate(cups.state === "stopped" ? "activity.paused" : "activity.clear");
  }
  findElementById("activity-title").textContent = title;
  findElementById("activity-detail").textContent = detail;
  for (const job of jobs) {
    const item = document.createElement("li");
    const identifier = document.createElement("span");
    const owner = document.createElement("span");
    identifier.textContent = String(job.id || translate("activity.job"));
    owner.textContent = job.owner ? String(job.owner) : "";
    item.append(identifier, owner);
    list.append(item);
  }
}

function render(snapshot) {
  latestSnapshot = snapshot;
  connectionUnavailable = false;
  const state = snapshot.state || "unknown";
  const stateIdentifier = `status.${state}`;
  const label = translate(dashboardLanguages.en.messages[stateIdentifier] ? stateIdentifier : "status.unknown");
  findElementById("printer-state").textContent = label;
  const defaultMessageCodes = {
    ready: "printer_ready", offline: "printer_offline", printing: "printing_active",
    busy: "usb_connection_busy", error: "printer_error", cover_open: "cover_open",
    paper_jam: "paper_jam", out_of_paper: "paper_empty", ink_empty: "ink_empty"
  };
  const messageIdentifier = `message.${snapshot.message_code || defaultMessageCodes[state] || "status_unknown"}`;
  findElementById("printer-message").textContent = translate(
    dashboardLanguages.en.messages[messageIdentifier] ? messageIdentifier : "message.status_unknown",
    snapshot.message_parameters || {});
  const symbol = findElementById("status-symbol");
  symbol.className = "status-symbol";
  if (["ready", "printing", "error"].includes(state)) symbol.classList.add(state);
  if (["cover_open", "paper_jam", "out_of_paper", "ink_empty"].includes(state)) symbol.classList.add("error");
  const pollDate = snapshot.last_poll ? new Date(snapshot.last_poll) : null;
  findElementById("last-updated").textContent = pollDate && Number.isFinite(pollDate.getTime())
    ? translate("status.updated", { time: timeFormatter.format(pollDate) }) : translate("status.no_reading");
  findElementById("live-label").textContent = translate(snapshot.refreshing ? "status.reading" : "status.automatic",
    { seconds: numberFormatter.format(5) });
  renderInk(snapshot.ink, snapshot.capabilities);
  renderActivity(snapshot.cups);
  const notices = findElementById("notices");
  notices.replaceChildren();
  const warnings = Array.isArray(snapshot.warnings) ? snapshot.warnings : [];
  const warningDetails = Array.isArray(snapshot.warning_details) ? snapshot.warning_details : [];
  for (let index = 0; index < Math.min(warnings.length, 8); index += 1) {
    const warning = warningDetails[index];
    const identifier = `warning.${warning?.code || "status_detail"}`;
    const line = document.createElement("p");
    line.textContent = translate(dashboardLanguages.en.messages[identifier] ? identifier : "warning.status_detail",
      warning?.parameters || {});
    notices.append(line);
  }
  notices.hidden = !notices.childElementCount;
  if (state !== lastPrinterState) {
    findElementById("announcement").textContent = label;
    lastPrinterState = state;
  }
}

function connectionError() {
  connectionUnavailable = true;
  findElementById("printer-state").textContent = translate("status.connection_lost");
  findElementById("printer-message").textContent = translate("status.connection_lost_detail");
  findElementById("status-symbol").className = "status-symbol error";
  findElementById("live-label").textContent = translate("status.reconnecting");
  if (lastPrinterState !== "connection-error") {
    findElementById("announcement").textContent = translate("status.connection_retry");
  }
  lastPrinterState = "connection-error";
}

async function refresh(manual = false) {
  if (requestActive) {
    requestQueued = requestQueued || manual;
    return;
  }
  requestActive = true;
  const button = findElementById("refresh");
  if (manual) {
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    findElementById("refresh-label").textContent = translate("refresh.working");
  }
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 8000);
  try {
    const response = await fetch(manual ? "/api/ink?refresh=1" : "/api/ink", { cache: "no-store", signal: controller.signal });
    if (!response.ok) throw new Error("Status request failed");
    const snapshot = await response.json();
    if (!snapshot || typeof snapshot !== "object" || Array.isArray(snapshot)) throw new Error("Invalid status data");
    render(snapshot);
    if (manual) {
      findElementById("announcement").textContent = translate(snapshot.refreshing ? "refresh.requested" : "refresh.completed");
      clearTimeout(manualTimer);
      manualTimer = setTimeout(() => refresh(), 1200);
    }
  } catch (_) {
    connectionError();
  } finally {
    clearTimeout(timeout);
    requestActive = false;
    button.disabled = false;
    button.removeAttribute("aria-busy");
    findElementById("refresh-label").textContent = translate("refresh.button");
    if (requestQueued) {
      requestQueued = false;
      refresh(true);
    }
  }
}

try {
  const saved = localStorage.getItem(languageStorageKey);
  if (saved) languagePreference = saved;
} catch (_) {
  // Automatic browser-language detection also works without storage access.
}
applyLanguage(languagePreference);
findElementById("language").addEventListener("change", (event) => applyLanguage(event.target.value, true));
window.addEventListener("languagechange", () => {
  if (languagePreference === "auto") applyLanguage("auto");
});
findElementById("refresh").addEventListener("click", () => refresh(true));
document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
setInterval(() => { if (!document.hidden) refresh(); }, 5000);
refresh();
