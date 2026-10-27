const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const ts = require('typescript');

function setup(options) {
  const callbacks = [];
  const exports = {};
  const code = ts.transpileModule(fs.readFileSync('src/qrScanner.ts', 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
  vm.runInNewContext(code, { exports, setTimeout: (fn) => { callbacks.push(fn); return callbacks.length; }, clearTimeout: () => {} });
  const stop = exports.startQrScanLoop(options);
  return { stop, callbacks };
}

test('keeps waiting for video metadata, then decodes and stops', async () => {
  let ready = false;
  const results = [];
  const { callbacks } = setup({ ready: () => ready, fallbackScan: () => 'https://example.com', onResult: (v) => results.push(v) });
  assert.equal(callbacks.length, 1);
  ready = true;
  await callbacks.shift()();
  await new Promise(setImmediate);
  assert.deepEqual(results, ['https://example.com']);
  assert.equal(callbacks.length, 0);
});

test('native detector errors still run fallback', async () => {
  const results = [];
  setup({ ready: () => true, nativeScan: async () => { throw Error('Unsupported format'); }, fallbackScan: () => 'local QR', onResult: (v) => results.push(v) });
  await new Promise(setImmediate);
  await new Promise(setImmediate);
  assert.deepEqual(results, ['local QR']);
});

test('closing during detection ignores late results', async () => {
  let resolve;
  const results = [];
  const { stop } = setup({ ready: () => true, nativeScan: () => new Promise((r) => { resolve = r; }), fallbackScan: () => 'fallback', onResult: (v) => results.push(v) });
  stop();
  resolve('late QR');
  await new Promise(setImmediate);
  await new Promise(setImmediate);
  assert.deepEqual(results, []);
});

test('multiple results reach the selection callback without selecting the first', async () => {
  const results = [];
  setup({ ready: () => true, fallbackScan: () => ['first', 'second', 'first'], onResult: () => assert.fail('silently selected first'), onResults: values => results.push(...values) });
  await new Promise(setImmediate);
  assert.deepEqual(results, ['first', 'second']);
});

test('a hung native detector times out and falls back locally', async () => {
  const results = [];
  const { callbacks } = setup({ ready: () => true, nativeScan: () => new Promise(() => {}), fallbackScan: () => 'recovered', onResult: value => results.push(value) });
  callbacks.shift()();
  await new Promise(setImmediate);
  assert.deepEqual(results, ['recovered']);
});

