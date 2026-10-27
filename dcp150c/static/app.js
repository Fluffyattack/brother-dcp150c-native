/* SPDX-License-Identifier: GPL-2.0-or-later */
"use strict";

const findElementById = (elementId) => document.getElementById(elementId);
const inkNames = { c: "Cyan", m: "Magenta", y: "Yellow", k: "Black" };
let requestActive = false;
let requestQueued = false;
let manualTimer;
let lastState = "";

function formatStateLabel(value) {
  return String(value || "unknown").replaceAll("_", " ").replace(/^./, (character) => character.toUpperCase());
}

function renderInk(ink, capabilities) {
  for (const key of Object.keys(inkNames)) {
    const reading = ink?.[key] || {};
    const value = findElementById(`ink-value-${key}`);
    const progress = findElementById(`ink-progress-${key}`);
    const unknown = findElementById(`ink-unknown-${key}`);
    const state = reading.state || "unknown";
    const percentage = reading.percent;
    const known = typeof percentage === "number" && Number.isFinite(percentage) && percentage >= 0 && percentage <= 100;
    value.replaceChildren();
    value.classList.toggle("text-reading", !known && state !== "unknown");
    if (known) {
      value.append(document.createTextNode(String(Math.round(percentage))));
      const unit = document.createElement("small");
      unit.textContent = "%";
      value.append(unit);
      progress.value = percentage;
      progress.setAttribute("aria-valuetext", `${Math.round(percentage)} percent${reading.granularity === "stepped" ? ", reported level step" : ""}`);
      findElementById(`ink-state-${key}`).textContent = reading.granularity === "stepped"
        ? "Reported level step" : state === "unknown" ? "Reported by printer" : formatStateLabel(state);
    } else {
      const labels = { ok: "Detected", low: "Low", empty: "Empty", missing: "Missing", unknown: "—" };
      value.textContent = labels[state] || formatStateLabel(state);
      findElementById(`ink-state-${key}`).textContent = state === "unknown" ? "Level unavailable" : "Printer-reported status";
    }
    progress.hidden = !known;
    unknown.hidden = known;
  }
  findElementById("ink-note").textContent = capabilities?.ink_stepped
    ? "This model reports ink levels in steps: 0, 10, 25, 40, 55, 70, 85 and 100 percent. These are cartridge level bands."
    : capabilities?.ink_percent
    ? "Levels are reported by the printer. An unknown level does not mean an empty cartridge."
    : "Exact percentages are unavailable. Any low or empty states reported by the printer are shown; unknown does not mean empty.";
}

function renderActivity(cups) {
  cups = cups || {};
  const jobs = Array.isArray(cups.jobs) ? cups.jobs.slice(0, 20) : [];
  findElementById("queue-name").textContent = cups.queue || "PRINT QUEUE";
  const list = findElementById("job-list");
  list.replaceChildren();
  list.hidden = !jobs.length;
  findElementById("job-count").textContent = cups.available && cups.active !== null ? String(jobs.length) : "—";
  if (!cups.available) {
    findElementById("activity-title").textContent = "Print queue unavailable";
    findElementById("activity-detail").textContent = "Printing activity could not be read from the local print service.";
  } else if (cups.active === null) {
    findElementById("activity-title").textContent = "Queue status unknown";
    findElementById("activity-detail").textContent = "The selected print queue could not be checked.";
  } else if (cups.active || jobs.length) {
    findElementById("activity-title").textContent = jobs.length === 1 ? "1 active print job" : `${jobs.length} active print jobs`;
    findElementById("activity-detail").textContent = "Status queries wait while printing uses the USB connection.";
  } else {
    findElementById("activity-title").textContent = "No active print jobs";
    findElementById("activity-detail").textContent = cups.state === "stopped" ? "The print queue is paused." : "Your queue is clear.";
  }
  for (const job of jobs) {
    const item = document.createElement("li");
    const id = document.createElement("span");
    const detail = document.createElement("span");
    id.textContent = String(job.id || "Print job");
    detail.textContent = job.owner ? String(job.owner) : "";
    item.append(id, detail);
    list.append(item);
  }
}

function render(snapshot) {
  const state = snapshot.state || "unknown";
  const stateNames = { offline: "Printer is offline", ready: "Ready to print", printing: "Printing in progress", busy: "Printer is busy", error: "Printer needs attention", unknown: "Printer status unknown", cover_open: "Printer cover is open", paper_jam: "Paper jam", out_of_paper: "Printer needs paper", ink_empty: "An ink cartridge is empty" };
  const label = stateNames[state] || formatStateLabel(state);
  findElementById("printer-state").textContent = label;
  findElementById("printer-message").textContent = snapshot.message || "No additional status information.";
  const symbol = findElementById("status-symbol");
  symbol.className = "status-symbol";
  if (["ready", "printing", "error"].includes(state)) symbol.classList.add(state);
  if (["cover_open", "paper_jam", "out_of_paper", "ink_empty"].includes(state)) symbol.classList.add("error");
  const pollDate = snapshot.last_poll ? new Date(snapshot.last_poll) : null;
  findElementById("last-updated").textContent = pollDate && Number.isFinite(pollDate.getTime())
    ? `Updated ${pollDate.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}`
    : "No reading yet";
  findElementById("live-label").textContent = snapshot.refreshing ? "READING STATUS…" : "AUTO REFRESH · 5 SEC";
  renderInk(snapshot.ink, snapshot.capabilities);
  renderActivity(snapshot.cups);
  const notices = findElementById("notices");
  notices.replaceChildren();
  for (const warning of (Array.isArray(snapshot.warnings) ? snapshot.warnings : []).slice(0, 8)) {
    const line = document.createElement("p");
    line.textContent = String(warning);
    notices.append(line);
  }
  notices.hidden = !notices.childElementCount;
  if (state !== lastState) {
    findElementById("announcement").textContent = label;
    lastState = state;
  }
}

function connectionError() {
  findElementById("printer-state").textContent = "Dashboard connection lost";
  findElementById("printer-message").textContent = "Waiting to reconnect to the local service. The readings below may be out of date.";
  findElementById("status-symbol").className = "status-symbol error";
  findElementById("live-label").textContent = "RECONNECTING…";
  if (lastState !== "connection-error") findElementById("announcement").textContent = "Dashboard connection lost. Retrying automatically.";
  lastState = "connection-error";
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
    findElementById("refresh-label").textContent = "Refreshing…";
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
      findElementById("announcement").textContent = snapshot.refreshing ? "A fresh printer reading has been requested." : "Printer status refreshed.";
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
    findElementById("refresh-label").textContent = "Refresh now";
    if (requestQueued) {
      requestQueued = false;
      refresh(true);
    }
  }
}

findElementById("refresh").addEventListener("click", () => refresh(true));
document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
setInterval(() => { if (!document.hidden) refresh(); }, 5000);
refresh();
