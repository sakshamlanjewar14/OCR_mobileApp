const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const ts = require('typescript');
const exportsForTest = {};
const code = ts.transpileModule(fs.readFileSync('src/qrPayload.ts', 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
vm.runInNewContext(code, { exports: exportsForTest, URL });
const cases = JSON.parse(fs.readFileSync('../backend/tests/fixtures/qr_payloads.json', 'utf8'));
for (const [index, fixture] of cases.entries()) test(`QR payload case ${index + 1}`, () => {
  const parsed = JSON.parse(JSON.stringify(exportsForTest.parseQrPayload(fixture.payload)));
  for (const [key, value] of Object.entries(fixture.expected)) assert.deepEqual(parsed[key], value, key);
  for (const key of fixture.absent || []) assert.equal(parsed[key], undefined, key);
});
