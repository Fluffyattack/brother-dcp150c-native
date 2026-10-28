/* SPDX-License-Identifier: GPL-2.0-or-later */
"use strict";

let selectedPhysicalScanArea = null;
let currentScanPreview = null;
let scanAreaDrag = null;
let scanAreaRenderSignature = "";
const scanAreaFields = ["left", "top", "width", "height"];

function scanAreaFromSelection(frame, selection) {
  return {
    left: frame.left + selection.left * frame.width,
    top: frame.top + selection.top * frame.height,
    width: selection.width * frame.width,
    height: selection.height * frame.height
  };
}

function scanSelectionFromArea(frame, area) {
  return { left: (area.left - frame.left) / frame.width, top: (area.top - frame.top) / frame.height,
    width: area.width / frame.width, height: area.height / frame.height };
}

function scanAreaWithinGlass(area, glass) {
  return Boolean(area && glass && scanAreaFields.every((key) => Number.isFinite(area[key]))
    && area.left >= 0 && area.top >= 0 && area.width >= 1 && area.height >= 1
    && area.left + area.width <= glass.width + 0.01 && area.top + area.height <= glass.height + 0.01);
}

function scanAreaWithDefaults(area, glass) {
  if (!area && !glass) return null;
  const defaults = { left: 0, top: 0, width: glass ? Math.min(210, glass.width) : NaN,
    height: glass ? Math.min(297, glass.height) : NaN };
  return Object.fromEntries(scanAreaFields.map((key) => [key, Number(area?.[key] ?? defaults[key])]));
}

function currentScanGlass() {
  const area = controlState?.capabilities?.scanner?.area;
  if (area?.width > 0 && area?.height > 0) return area;
  const frame = currentScanPreview?.scan?.area;
  return frame ? { width: frame.left + frame.width, height: frame.top + frame.height } : null;
}

function scanAreaIsValid() {
  const glass = currentScanGlass();
  if (!glass && !selectedPhysicalScanArea) return false;
  return scanAreaWithinGlass(selectedPhysicalScanArea, glass);
}

function describeScanArea(area) {
  const formatter = new Intl.NumberFormat(selectedLanguage, { maximumFractionDigits: 1 });
  return controlText("scan_area.summary", Object.fromEntries(scanAreaFields.map((key) => [key, formatter.format(area[key])])));
}

function settingsWithScanArea(form, settings) {
  if (form.id === "copy-form") {
    delete settings.use_scan_area;
    if (findElementById("copy-use-scan-area").checked && selectedPhysicalScanArea) settings.area = { ...selectedPhysicalScanArea };
    return settings;
  }
  if (form.id !== "scan-form") return settings;
  for (const key of scanAreaFields) delete settings[`scan_area_${key}`];
  if (selectedPhysicalScanArea) settings.area = { ...selectedPhysicalScanArea };
  return settings;
}

function applyScanAreaSettings(area) {
  const glass = currentScanGlass();
  selectedPhysicalScanArea = scanAreaWithDefaults(area, glass);
  scanAreaRenderSignature = "";
  updateScanAreaFields();
  renderScanArea();
}

function clearScanAreaViews() {
  selectedPhysicalScanArea = currentScanPreview = scanAreaDrag = null;
  scanAreaRenderSignature = "";
  findElementById("scan-area-image").removeAttribute("src");
  findElementById("scan-area-canvas").hidden = true;
  for (const identifier of ["scan-area-preview-note", "scan-area-summary", "scan-area-error", "copy-scan-area-note"]) findElementById(identifier).textContent = "";
  findElementById("copy-use-scan-area").checked = false;
  for (const key of scanAreaFields) findElementById("scan-form").elements[`scan_area_${key}`].value = "";
}

function updateScanAreaFields() {
  const form = findElementById("scan-form");
  for (const key of scanAreaFields) form.elements[`scan_area_${key}`].value = selectedPhysicalScanArea && Number.isFinite(selectedPhysicalScanArea[key])
    ? Math.round(selectedPhysicalScanArea[key] * 1000) / 1000 : "";
}

function drawScanAreaSelection() {
  const frame = currentScanPreview?.scan?.area;
  const valid = scanAreaIsValid();
  const error = findElementById("scan-area-error");
  error.hidden = valid || !selectedPhysicalScanArea;
  error.textContent = valid ? "" : controlText("scan_area.invalid");
  findElementById("scan-area-summary").textContent = selectedPhysicalScanArea && valid ? describeScanArea(selectedPhysicalScanArea) : "";
  findElementById("copy-scan-area-note").textContent = selectedPhysicalScanArea && valid ? describeScanArea(selectedPhysicalScanArea) : controlText("scan_area.copy_help");
  if (!frame || !selectedPhysicalScanArea) return;
  const selection = scanSelectionFromArea(frame, selectedPhysicalScanArea);
  const left = Math.max(0, Math.min(1, selection.left));
  const top = Math.max(0, Math.min(1, selection.top));
  const right = Math.max(left, Math.min(1, selection.left + selection.width));
  const bottom = Math.max(top, Math.min(1, selection.top + selection.height));
  const rectangle = findElementById("scan-area-selection");
  for (const [key, value] of Object.entries({ x: left, y: top, width: right - left, height: bottom - top })) rectangle.setAttribute(key, String(value));
  findElementById("scan-area-shade").setAttribute("d", `M0 0H1V1H0Z M${left} ${top}H${right}V${bottom}H${left}Z`);
}

function renderScanArea() {
  if (!controlState || !controlSession?.authenticated) return;
  const capabilities = controlState.capabilities?.scanner || {};
  const preview = controlState.documents.find((document) => document.has_preview && document.scan?.kind === "preview"
    && (!capabilities.available || document.scan.device === capabilities.device));
  if (preview?.id !== currentScanPreview?.id) {
    currentScanPreview = preview || null;
    const image = findElementById("scan-area-image");
    if (preview) image.src = `/api/documents/${encodeURIComponent(preview.id)}/preview`;
    else image.removeAttribute("src");
    findElementById("scan-area-canvas").hidden = !preview;
  }
  const glass = currentScanGlass();
  if (!selectedPhysicalScanArea && glass) {
    selectedPhysicalScanArea = { left: 0, top: 0, width: Math.min(210, glass.width), height: Math.min(297, glass.height) };
    updateScanAreaFields();
  }
  const signature = `${selectedLanguage}:${JSON.stringify([preview?.id, glass, capabilities.available, selectedPhysicalScanArea])}`;
  if (signature === scanAreaRenderSignature) return;
  scanAreaRenderSignature = signature;
  const note = findElementById("scan-area-preview-note");
  note.textContent = controlText(preview ? capabilities.available ? "scan_area.preview_ready" : "scan_area.preview_saved" : "scan_area.no_preview");
  for (const key of scanAreaFields) {
    const field = findElementById("scan-form").elements[`scan_area_${key}`];
    field.disabled = !glass;
    if (glass) field.max = String(key === "left" || key === "width" ? glass.width : glass.height);
  }
  findElementById("scan-area-full").disabled = !glass;
  drawScanAreaSelection();
  updateScannerControls();
}

function refreshScanAreaInteraction() {
  scanAreaRenderSignature = "";
  findElementById("scan-profile").value = "";
  drawScanAreaSelection();
  updateScannerControls();
  if (typeof renderScanSessions === "function") renderScanSessions(true);
}

// DOM event bindings use the same geometry helpers as keyboard input and profiles.
for (const key of scanAreaFields) findElementById("scan-form").elements[`scan_area_${key}`].addEventListener("input", () => {
  selectedPhysicalScanArea = Object.fromEntries(scanAreaFields.map((field) => [field, Number(findElementById("scan-form").elements[`scan_area_${field}`].value)]));
  refreshScanAreaInteraction();
});

const scanAreaOverlay = findElementById("scan-area-overlay");
function scanAreaPointerPosition(event) {
  const bounds = scanAreaOverlay.getBoundingClientRect();
  return { left: Math.max(0, Math.min(1, (event.clientX - bounds.left) / bounds.width)),
    top: Math.max(0, Math.min(1, (event.clientY - bounds.top) / bounds.height)) };
}
scanAreaOverlay.addEventListener("pointerdown", (event) => {
  if (!currentScanPreview || event.button > 0) return;
  scanAreaDrag = { start: scanAreaPointerPosition(event), previous: selectedPhysicalScanArea, pointer: event.pointerId };
  scanAreaOverlay.setPointerCapture(event.pointerId);
  event.preventDefault();
});
scanAreaOverlay.addEventListener("pointermove", (event) => {
  if (!scanAreaDrag || event.pointerId !== scanAreaDrag.pointer) return;
  const position = scanAreaPointerPosition(event);
  const selection = { left: Math.min(position.left, scanAreaDrag.start.left), top: Math.min(position.top, scanAreaDrag.start.top),
    width: Math.abs(position.left - scanAreaDrag.start.left), height: Math.abs(position.top - scanAreaDrag.start.top) };
  selectedPhysicalScanArea = scanAreaFromSelection(currentScanPreview.scan.area, selection);
  updateScanAreaFields();
  refreshScanAreaInteraction();
});
function finishScanAreaPointer(event) {
  if (!scanAreaDrag || event.pointerId !== scanAreaDrag.pointer) return;
  if (!scanAreaIsValid() || event.type === "pointercancel") selectedPhysicalScanArea = scanAreaDrag.previous;
  scanAreaDrag = null;
  if (scanAreaOverlay.hasPointerCapture(event.pointerId)) scanAreaOverlay.releasePointerCapture(event.pointerId);
  updateScanAreaFields();
  refreshScanAreaInteraction();
}
scanAreaOverlay.addEventListener("pointerup", finishScanAreaPointer);
scanAreaOverlay.addEventListener("pointercancel", finishScanAreaPointer);
findElementById("scan-area-full").addEventListener("click", () => {
  const glass = currentScanGlass();
  if (!glass) return;
  selectedPhysicalScanArea = { left: 0, top: 0, width: glass.width, height: glass.height };
  updateScanAreaFields();
  refreshScanAreaInteraction();
});
findElementById("scan-area-profile").addEventListener("click", () => {
  findElementById("profile-form").elements.kind.value = "scan";
  showControlView("profiles");
  findElementById("profile-form").elements.name.focus();
  showControlFeedback("scan_area.profile_help");
});
findElementById("copy-use-scan-area").addEventListener("change", updateScannerControls);
if (controlState) renderScanArea();
