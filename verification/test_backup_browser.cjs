/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Check the actual bounded retry fingerprint on HTTPS and plain local HTTP. */
"use strict";
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const { webcrypto } = require("node:crypto");
const vm = require("node:vm");
const source = readFileSync(join(__dirname, "../dcp150c/static/backups.js"), "utf8");
const functions = source.slice(0, source.indexOf('findElementById("backup-selected-documents").addEventListener'));

async function fingerprint(crypto, bytes, restore = false) {
  const reads = [];
  const blob = new Blob([bytes]);
  const file = { size: blob.size, lastModified: 123,
    slice(start, end) { const result = blob.slice(start, end); reads.push(result.size); return result; } };
  const context = vm.createContext({ crypto, file, Uint8Array });
  vm.runInContext(functions, context);
  const value = await vm.runInContext(`backupUploadIdentity(file, ${restore})`, context);
  return { value: JSON.parse(JSON.stringify(value)), reads };
}

(async () => {
  const bytes = new Uint8Array(4 * 1024 * 1024).fill(17);
  const first = await fingerprint(webcrypto, bytes);
  const repeated = await fingerprint(webcrypto, bytes);
  assert.deepEqual(first.value, repeated.value);
  assert.equal(first.value.fingerprint.length, 64);
  assert.deepEqual(first.reads, [65536, 65536]);
  console.log("PASS: HTTPS retry identity is stable and reads at most two 64-KiB edges");

  const localCrypto = { getRandomValues: webcrypto.getRandomValues.bind(webcrypto) };
  const local = await fingerprint(localCrypto, bytes);
  assert.deepEqual(local.value, (await fingerprint(localCrypto, bytes)).value);
  bytes[0] = 18;
  assert.notEqual(local.value.fingerprint, (await fingerprint(localCrypto, bytes)).value.fingerprint);
  assert.equal(local.value.fingerprint.length, 8);
  console.log("PASS: plain local HTTP can retry backup uploads without SubtleCrypto");

  const documentsOnly = (await fingerprint(webcrypto, bytes)).value;
  const withSettings = (await fingerprint(webcrypto, bytes, true)).value;
  assert.equal(documentsOnly.fingerprint, withSettings.fingerprint);
  assert.notDeepEqual(documentsOnly, withSettings);
  assert.deepEqual(Object.keys(withSettings).sort(), ["fingerprint", "last_modified", "restore_settings", "size"]);
  console.log("PASS: preference choices identify different actions and fingerprints contain no names or file contents");
})().catch(error => { console.error(error); process.exitCode = 1; });
