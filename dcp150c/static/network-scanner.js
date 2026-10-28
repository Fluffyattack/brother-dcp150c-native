/* SPDX-License-Identifier: GPL-2.0-or-later */
"use strict";

// Status rendering uses cached state. Only explicit form actions change sharing.
let networkScannerDraft = null;
let networkScannerDraftDirty = false;
let networkScannerLatestSnapshot = null;
let networkScannerRequestActive = false;
let networkScannerRequestGeneration = 0;
let networkScannerInterfaceSignature = "";
let networkScannerLocalMessage = null;

function copyNetworkScannerConfiguration(configuration) {
  if (!configuration || !Number.isSafeInteger(configuration.revision) || configuration.revision < 0
      || typeof configuration.enabled !== "boolean" || typeof configuration.interface !== "string"
      || !Number.isSafeInteger(configuration.port)) return null;
  return { revision: configuration.revision, enabled: configuration.enabled,
    interface: configuration.interface, port: configuration.port };
}

function networkScannerDraftFrom(configuration) {
  const copied = copyNetworkScannerConfiguration(configuration);
  return copied ? { ...copied, port: String(copied.port) } : null;
}

function cachedNetworkScannerSnapshot() {
  const snapshot = controlState?.network_scanner;
  const configuration = copyNetworkScannerConfiguration(snapshot?.configuration);
  // A late state response must not roll back a successfully saved revision.
  if (configuration && (!networkScannerLatestSnapshot
      || configuration.revision >= networkScannerLatestSnapshot.configuration.revision)) {
    networkScannerLatestSnapshot = { ...snapshot, configuration };
  }
  return networkScannerLatestSnapshot;
}

function networkScannerInterfaces(snapshot) {
  return Array.isArray(snapshot?.interfaces) ? snapshot.interfaces.filter((entry) =>
    entry && typeof entry.name === "string" && entry.name.length > 0).map((entry) => ({
    name: entry.name, addresses: Array.isArray(entry.addresses) ? entry.addresses.filter((address) => typeof address === "string") : []
  })) : [];
}

function networkScannerSettingsProblem(values, snapshot) {
  if (!/^\d+$/.test(String(values?.port || "")) || !Number.isSafeInteger(Number(values.port))
      || Number(values.port) < 1024 || Number(values.port) > 65535) return "network_scanner.invalid_port";
  // An unavailable helper or removed interface must never prevent switching off.
  if (!values?.enabled) return "";
  if (snapshot?.available !== true) return "network_scanner.missing";
  const interfaces = networkScannerInterfaces(snapshot);
  if (!interfaces.length) return "network_scanner.no_interfaces";
  if (!interfaces.some((entry) => entry.name === values.interface)) return "network_scanner.choose_interface";
  return "";
}

function networkScannerStatusTranslation(code) {
  const identifier = `network_scanner.${String(code || "").replace(/^network_scanner_/, "")}`;
  return dashboardLanguages.en.messages[identifier] ? identifier : "network_scanner.unavailable";
}

function renderNetworkScannerInterfaces(snapshot) {
  const interfaces = networkScannerInterfaces(snapshot);
  const selector = findElementById("network-scanner-interface");
  const selected = networkScannerDraft?.interface || "";
  const signature = `${selectedLanguage}:${JSON.stringify([interfaces, selected])}`;
  if (signature !== networkScannerInterfaceSignature) {
    networkScannerInterfaceSignature = signature;
    selector.replaceChildren(new Option(controlText(interfaces.length ? "network_scanner.choose_interface_label" : "network_scanner.no_interfaces_label"), ""));
    for (const entry of interfaces) selector.append(new Option(entry.name, entry.name));
    if (selected && !interfaces.some((entry) => entry.name === selected)) {
      const missing = new Option(controlText("network_scanner.missing_interface", { name: selected }), selected);
      missing.disabled = true;
      selector.append(missing);
    }
    selector.value = selected;
  }
  const current = interfaces.find((entry) => entry.name === selected);
  const missing = selected && !current;
  findElementById("network-scanner-interface-note").textContent = controlText(missing ? "network_scanner.interface_removed"
    : interfaces.length ? "network_scanner.interface_help" : "network_scanner.no_interfaces");
  findElementById("network-scanner-addresses").textContent = current?.addresses.length
    ? controlText("network_scanner.addresses", { addresses: current.addresses.join(" · ") }) : "";
}

function renderNetworkScannerSettings() {
  if (!controlSession?.authenticated) { clearNetworkScannerViews(); return; }
  const snapshot = cachedNetworkScannerSnapshot();
  const configuration = snapshot?.configuration || null;
  if (!networkScannerDraft || (!networkScannerDraftDirty && !networkScannerRequestActive
      && JSON.stringify(networkScannerDraft) !== JSON.stringify(networkScannerDraftFrom(configuration)))) {
    networkScannerDraft = networkScannerDraftFrom(configuration);
  }
  const conflict = Boolean(configuration && networkScannerDraft && configuration.revision !== networkScannerDraft.revision);
  const problem = networkScannerDraft ? networkScannerSettingsProblem(networkScannerDraft, snapshot) : "";
  const enabled = findElementById("network-scanner-enabled");
  const port = findElementById("network-scanner-port");
  enabled.checked = networkScannerDraft?.enabled === true;
  const portText = networkScannerDraft?.port || "";
  if (port.value !== portText) port.value = portText;
  findElementById("network-scanner-status").textContent = controlText(networkScannerStatusTranslation(snapshot?.status_code));
  findElementById("network-scanner-hardware-note").hidden = snapshot?.hardware_verified === true;
  const active = ["metadata", "session"].includes(snapshot?.active_operation) ? snapshot.active_operation : null;
  findElementById("network-scanner-active-operation").textContent = active ? controlText(`network_scanner.active.${active}`) : "";
  renderNetworkScannerInterfaces(snapshot);
  for (const input of findElementById("network-scanner-form").querySelectorAll("input, select")) {
    input.disabled = !configuration || networkScannerRequestActive;
  }
  findElementById("network-scanner-save").disabled = !configuration || networkScannerRequestActive
    || !networkScannerDraftDirty || Boolean(problem) || conflict;
  findElementById("network-scanner-reload").disabled = !configuration || networkScannerRequestActive;
  findElementById("network-scanner-reload").hidden = !networkScannerDraftDirty && !conflict;
  const changing = ["network_scanner_starting", "network_scanner_stopping"].includes(snapshot?.status_code);
  findElementById("network-scanner-restart").disabled = !configuration?.enabled || snapshot?.available !== true
    || networkScannerRequestActive || networkScannerDraftDirty || conflict || changing || Boolean(active)
    || snapshot?.status_code === "network_scanner_busy";
  const message = conflict ? { identifier: "network_scanner.conflict", error: true }
    : problem ? { identifier: problem, error: true }
    : networkScannerLocalMessage || (networkScannerDraftDirty ? { identifier: "network_scanner.unsaved", error: false } : null);
  const note = findElementById("network-scanner-save-note");
  note.textContent = message ? controlText(message.identifier) : "";
  note.classList.toggle("network-scanner-error", Boolean(message?.error));
}

function updateNetworkScannerDraft(event) {
  if (!networkScannerDraft || networkScannerRequestActive) return;
  const input = event.target;
  if (input.id === "network-scanner-enabled") networkScannerDraft.enabled = input.checked;
  else if (input.id === "network-scanner-interface") networkScannerDraft.interface = input.value;
  else if (input.id === "network-scanner-port") networkScannerDraft.port = input.value;
  else return;
  networkScannerDraftDirty = true;
  networkScannerLocalMessage = null;
  renderNetworkScannerSettings();
}

function networkScannerSettingsValues() {
  return networkScannerDraft ? { revision: networkScannerDraft.revision, enabled: networkScannerDraft.enabled,
    interface: networkScannerDraft.interface, port: Number(networkScannerDraft.port) } : null;
}

async function performNetworkScannerRequest(path, values, button, restart = false) {
  if (networkScannerRequestActive || !controlSession?.authenticated || !controlState) return;
  const snapshot = cachedNetworkScannerSnapshot();
  const problem = restart ? (!snapshot?.configuration.enabled ? "network_scanner.disabled" : "")
    : networkScannerSettingsProblem(values, snapshot);
  if (!snapshot || values.revision !== snapshot.configuration.revision || problem) {
    networkScannerLocalMessage = { identifier: problem || "network_scanner.conflict", error: true };
    renderNetworkScannerSettings();
    return;
  }
  const generation = networkScannerRequestGeneration;
  networkScannerRequestActive = true;
  const operation = performControlAction(button, async () => {
    try {
      const result = await controlRequest(path, { method: "POST", body: values });
      if (generation !== networkScannerRequestGeneration || !controlSession?.authenticated || !controlState) return;
      const configuration = copyNetworkScannerConfiguration(result.network_scanner?.configuration);
      if (!configuration) throw Object.assign(new Error("Invalid network scanner settings response"), { code: "request_failed" });
      networkScannerLatestSnapshot = { ...result.network_scanner, configuration };
      controlState.network_scanner = networkScannerLatestSnapshot;
      networkScannerDraft = networkScannerDraftFrom(configuration);
      networkScannerDraftDirty = false;
      const identifier = restart ? "network_scanner.restart_requested" : "network_scanner.saved";
      networkScannerLocalMessage = { identifier, error: false };
      showControlFeedback(identifier);
    } catch (error) {
      if (generation !== networkScannerRequestGeneration || !controlSession?.authenticated) return;
      const identifier = error.code === "request_conflict" ? "network_scanner.conflict"
        : dashboardLanguages.en.messages[`network_scanner.${String(error.code).replace(/^network_scanner_/, "")}`]
        ? networkScannerStatusTranslation(error.code) : errorTranslation(error);
      networkScannerLocalMessage = { identifier, error: true };
      showControlFeedback(identifier, {}, true);
      if (error.code === "request_conflict") await refreshControlCenter();
    }
  });
  renderNetworkScannerSettings();
  try { await operation; }
  finally {
    if (generation === networkScannerRequestGeneration) {
      networkScannerRequestActive = false;
      renderNetworkScannerSettings();
    }
  }
}

function confirmNetworkScannerSharing(values) {
  findElementById("confirmation-title").textContent = controlText("network_scanner.confirm_title");
  findElementById("confirmation-description").textContent = controlText("network_scanner.confirm_help");
  findElementById("confirmation-submit").textContent = controlText("network_scanner.confirm_save");
  findElementById("confirmation-details").replaceChildren(
    makeElement("dt", "", controlText("network_scanner.interface")), makeElement("dd", "", values.interface),
    makeElement("dt", "", controlText("network_scanner.port")), makeElement("dd", "", String(values.port)));
  confirmationAction = () => performNetworkScannerRequest("/api/network-scanner/settings", values, findElementById("confirmation-submit"));
  findElementById("action-confirmation").showModal();
}

function clearNetworkScannerViews() {
  networkScannerRequestGeneration += 1;
  networkScannerDraft = null;
  networkScannerDraftDirty = false;
  networkScannerLatestSnapshot = null;
  networkScannerRequestActive = false;
  networkScannerLocalMessage = null;
  networkScannerInterfaceSignature = "";
  findElementById("network-scanner-enabled").checked = false;
  findElementById("network-scanner-port").value = "";
  findElementById("network-scanner-interface").replaceChildren();
  for (const input of findElementById("network-scanner-form").querySelectorAll("input, select, button")) input.disabled = true;
  for (const identifier of ["network-scanner-status", "network-scanner-interface-note", "network-scanner-addresses", "network-scanner-active-operation", "network-scanner-save-note"]) {
    findElementById(identifier).textContent = "";
  }
  findElementById("network-scanner-reload").hidden = true;
}

findElementById("network-scanner-form").addEventListener("change", updateNetworkScannerDraft);
findElementById("network-scanner-port").addEventListener("input", updateNetworkScannerDraft);
findElementById("network-scanner-form").addEventListener("submit", (event) => {
  event.preventDefault();
  if (!networkScannerDraftDirty || findElementById("network-scanner-save").disabled) return;
  const values = networkScannerSettingsValues();
  if (values.enabled) confirmNetworkScannerSharing(values);
  else performNetworkScannerRequest("/api/network-scanner/settings", values, findElementById("network-scanner-save"));
});
findElementById("network-scanner-reload").addEventListener("click", () => {
  if (networkScannerRequestActive) return;
  networkScannerDraft = networkScannerDraftFrom(cachedNetworkScannerSnapshot()?.configuration);
  networkScannerDraftDirty = false;
  networkScannerLocalMessage = null;
  renderNetworkScannerSettings();
});
findElementById("network-scanner-restart").addEventListener("click", (event) => {
  if (event.currentTarget.disabled) return;
  const configuration = cachedNetworkScannerSnapshot()?.configuration;
  if (configuration?.enabled) performNetworkScannerRequest("/api/network-scanner/restart", { revision: configuration.revision }, event.currentTarget, true);
});
document.addEventListener("dashboardlanguagechange", renderNetworkScannerSettings);
renderNetworkScannerSettings();
