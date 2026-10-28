/* SPDX-License-Identifier: GPL-2.0-or-later */
"use strict";

function renderBackupSelection() {
  findElementById("backup-selection-note").textContent = controlText("backup.selection", { count: numberFormatter.format(documentSelection.length) });
  findElementById("backup-selected-documents").disabled = !documentSelection.length;
  findElementById("backup-create").disabled = !documentSelection.length && !findElementById("backup-include-settings").checked;
}

function renderBackups() {
  renderBackupSelection();
  const list = findElementById("backup-list");
  list.replaceChildren();
  for (const backup of controlState?.backups || []) {
    const card = makeElement("div", "profile-card");
    card.setAttribute("role", "listitem");
    const formatter = new Intl.DateTimeFormat(selectedLanguage, { dateStyle: "short", timeStyle: "short" });
    card.append(makeElement("strong", "", controlText("backup.prepared")),
      makeElement("p", "field-help", `${formatDocumentSize(backup.size)} · ${controlText("backup.expires", { time: formatter.format(new Date(backup.expires_at)) })}`));
    const actions = makeElement("div", "button-row");
    const link = makeElement("a", "secondary-button", controlText("backup.download"));
    link.href = `/api/backups/${encodeURIComponent(backup.id)}/file`;
    link.download = "control-center-backup.zip";
    actions.append(link, makeButton(controlText("backup.remove"), "text-button", (event) => performControlAction(event.currentTarget, async () => {
      await controlRequest(`/api/backups/${encodeURIComponent(backup.id)}`, { method: "DELETE" });
      await refreshControlCenter();
    })));
    card.append(actions);
    list.append(card);
  }
}

function clearBackupViews() {
  findElementById("backup-list").replaceChildren();
  findElementById("backup-selection-note").textContent = "";
  findElementById("backup-upload").value = "";
  findElementById("backup-restore-settings").checked = false;
}

async function backupUploadIdentity(file, restoreSettings) {
  // Retain a bounded fingerprint rather than names or document contents. The
  // server hashes the complete upload and rejects changed bytes on a retry.
  const edgeSize = 65536;
  const first = await file.slice(0, edgeSize).arrayBuffer();
  const last = await file.slice(Math.max(0, file.size - edgeSize)).arrayBuffer();
  const edges = new Uint8Array(first.byteLength + last.byteLength);
  edges.set(new Uint8Array(first));
  edges.set(new Uint8Array(last), first.byteLength);
  let fingerprint;
  if (crypto.subtle) {
    const hash = new Uint8Array(await crypto.subtle.digest("SHA-256", edges));
    fingerprint = Array.from(hash, (byte) => byte.toString(16).padStart(2, "0")).join("");
  } else {
    // Plain HTTP on a local network has no SubtleCrypto. This identifier only
    // associates retries; the server still verifies the entire file with SHA-256.
    let value = 2166136261;
    for (const byte of edges) value = Math.imul(value ^ byte, 16777619) >>> 0;
    fingerprint = value.toString(16).padStart(8, "0");
  }
  return { size: file.size, last_modified: file.lastModified,
    fingerprint, restore_settings: restoreSettings };
}

findElementById("backup-selected-documents").addEventListener("click", () => {
  showControlView("settings", true);
  renderBackupSelection();
  findElementById("backup-heading").scrollIntoView({ block: "start", behavior: "smooth" });
});
findElementById("backup-include-settings").addEventListener("change", renderBackupSelection);
findElementById("backup-create-form").addEventListener("submit", (event) => {
  event.preventDefault();
  startDocumentJob("/api/backups", { document_ids: documentSelection.slice(), include_settings: findElementById("backup-include-settings").checked }, findElementById("backup-create"));
});
findElementById("backup-restore-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const file = findElementById("backup-upload").files?.[0];
  if (!file) return;
  if (file.size > (controlState?.backup_upload_limit || 0)) { showControlFeedback("error.backup_too_large", {}, true); return; }
  const restoreSettings = findElementById("backup-restore-settings").checked;
  findElementById("confirmation-title").textContent = controlText("backup.confirm_title");
  findElementById("confirmation-description").textContent = controlText(restoreSettings ? "backup.confirm_with_settings" : "backup.confirm_documents");
  findElementById("confirmation-details").replaceChildren(makeElement("dt", "", controlText("backup.choose_file")), makeElement("dd", "", `${file.name} · ${formatDocumentSize(file.size)}`));
  findElementById("confirmation-submit").textContent = controlText("backup.restore");
  confirmationAction = () => performControlAction(findElementById("confirmation-submit"), async () => {
    showControlFeedback("backup.uploading");
    const uploadIdentity = await backupUploadIdentity(file, restoreSettings);
    await controlRequest("/api/backups/restore", { method: "POST", body: file, uploadIdentity,
      headers: { "Content-Type": "application/zip", "X-Restore-Settings": String(restoreSettings) }, timeout: 600000 });
    findElementById("backup-upload").value = "";
    showControlFeedback("jobs.accepted");
    await refreshControlCenter();
  });
  findElementById("action-confirmation").showModal();
});
if (controlState) renderBackups();
