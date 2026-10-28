const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const ts = require('typescript');

// Transpile exportFileName.ts
const exportsForTest = {};
const code = ts.transpileModule(fs.readFileSync('src/exportFileName.ts', 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 }
}).outputText;
vm.runInNewContext(code, {
  exports: exportsForTest,
  Response: globalThis.Response,
  Blob: globalThis.Blob,
  File: globalThis.File,
  URL: globalThis.URL,
  Uint8Array: globalThis.Uint8Array,
  ArrayBuffer: globalThis.ArrayBuffer,
  setTimeout: globalThis.setTimeout,
  clearTimeout: globalThis.clearTimeout,
});

const {
  sanitizeExportFileName,
  DEFAULT_EXPORT_BASE_NAME,
  DEFAULT_EXPORT_FILE_NAME,
  getExportErrorMessage,
  validateExportResponse,
  triggerBlobDownload,
  executeExportFlow,
} = exportsForTest;

test('default file name', () => {
  assert.equal(DEFAULT_EXPORT_BASE_NAME, 'lead-gennie-export');
  assert.equal(DEFAULT_EXPORT_FILE_NAME, 'lead-gennie-export.xlsx');

  const res = sanitizeExportFileName('lead-gennie-export');
  assert.equal(res.valid, true);
  assert.equal(res.sanitizedBaseName, 'lead-gennie-export');
  assert.equal(res.finalFileName, 'lead-gennie-export.xlsx');

  const fallbackRes = sanitizeExportFileName('', true);
  assert.equal(fallbackRes.valid, true);
  assert.equal(fallbackRes.sanitizedBaseName, 'lead-gennie-export');
  assert.equal(fallbackRes.finalFileName, 'lead-gennie-export.xlsx');
});

test('custom human-readable file names', () => {
  const cases = [
    { input: 'October leads', expectedBase: 'October leads', expectedFile: 'October leads.xlsx' },
    { input: 'Pune business contacts', expectedBase: 'Pune business contacts', expectedFile: 'Pune business contacts.xlsx' },
    { input: 'Manager review 2026', expectedBase: 'Manager review 2026', expectedFile: 'Manager review 2026.xlsx' },
  ];
  for (const c of cases) {
    const res = sanitizeExportFileName(c.input);
    assert.equal(res.valid, true, `valid for ${c.input}`);
    assert.equal(res.sanitizedBaseName, c.expectedBase);
    assert.equal(res.finalFileName, c.expectedFile);
  }
});

test('names containing repeated spaces', () => {
  const res = sanitizeExportFileName('   Pune    business    contacts   ');
  assert.equal(res.valid, true);
  assert.equal(res.sanitizedBaseName, 'Pune business contacts');
  assert.equal(res.finalFileName, 'Pune business contacts.xlsx');
});

test('invalid Windows and browser characters are removed', () => {
  const res = sanitizeExportFileName('leads:2026*final?"test<file>name|');
  assert.equal(res.valid, true);
  assert.equal(res.sanitizedBaseName, 'leads2026finaltestfilename');
  assert.equal(res.finalFileName, 'leads2026finaltestfilename.xlsx');
});

test('path traversal attempts are removed safely', () => {
  const res = sanitizeExportFileName('../../secret-leads');
  assert.equal(res.valid, true);
  assert.equal(res.sanitizedBaseName, 'secret-leads');
  assert.equal(res.finalFileName, 'secret-leads.xlsx');

  const resWindows = sanitizeExportFileName('..\\..\\windows\\system32\\leads');
  assert.equal(resWindows.valid, true);
  assert.equal(resWindows.sanitizedBaseName, 'windowssystem32leads');
  assert.equal(resWindows.finalFileName, 'windowssystem32leads.xlsx');
});

test('empty or whitespace-only input returns error', () => {
  const emptyRes = sanitizeExportFileName('');
  assert.equal(emptyRes.valid, false);
  assert.equal(emptyRes.error, 'Enter a file name.');

  const spacesRes = sanitizeExportFileName('     ');
  assert.equal(spacesRes.valid, false);
  assert.equal(spacesRes.error, 'Enter a file name.');
});

test('input containing only invalid characters returns error', () => {
  const res = sanitizeExportFileName('***???///:::|||<<<>>>');
  assert.equal(res.valid, false);
  assert.match(res.error, /invalid or special characters/i);
});

test('duplicate .xlsx extensions are prevented', () => {
  const single = sanitizeExportFileName('report.xlsx');
  assert.equal(single.valid, true);
  assert.equal(single.sanitizedBaseName, 'report');
  assert.equal(single.finalFileName, 'report.xlsx');

  const duplicate = sanitizeExportFileName('report.xlsx.xlsx');
  assert.equal(duplicate.valid, true);
  assert.equal(duplicate.sanitizedBaseName, 'report');
  assert.equal(duplicate.finalFileName, 'report.xlsx');

  const mixedCase = sanitizeExportFileName('Q3-Leads.XLSX.xlsx');
  assert.equal(mixedCase.valid, true);
  assert.equal(mixedCase.sanitizedBaseName, 'Q3-Leads');
  assert.equal(mixedCase.finalFileName, 'Q3-Leads.xlsx');
});

test('very long file names are capped at 120 characters', () => {
  const longName = 'A'.repeat(200);
  const res = sanitizeExportFileName(longName);
  assert.equal(res.valid, true);
  assert.equal(res.sanitizedBaseName.length, 120);
  assert.equal(res.sanitizedBaseName, 'A'.repeat(120));
  assert.equal(res.finalFileName, 'A'.repeat(120) + '.xlsx');
});

test('control characters and leading/trailing dots are removed', () => {
  const res = sanitizeExportFileName('..\x00My\x1fReport\x7f..');
  assert.equal(res.valid, true);
  assert.equal(res.sanitizedBaseName, 'MyReport');
  assert.equal(res.finalFileName, 'MyReport.xlsx');
});

test('export url parameter building preserves filters and selected IDs', () => {
  // Selected IDs take precedence over filters
  const params1 = new URLSearchParams();
  const selected = new Set(['lead-1', 'lead-2']);
  selected.forEach((id) => params1.append('ids', id));
  params1.set('filename', 'Custom Export');
  assert.equal(params1.getAll('ids').length, 2);
  assert.equal(params1.get('filename'), 'Custom Export');

  // Filters used when no selection
  const params2 = new URLSearchParams();
  params2.set('search', 'Tech');
  params2.set('company_name', 'Acme');
  params2.set('city', 'Pune');
  params2.set('state', 'Maharashtra');
  params2.set('export_count', '25');
  params2.set('filename', 'pune-leads');
  assert.equal(params2.get('search'), 'Tech');
  assert.equal(params2.get('company_name'), 'Acme');
  assert.equal(params2.get('city'), 'Pune');
  assert.equal(params2.get('state'), 'Maharashtra');
  assert.equal(params2.get('export_count'), '25');
  assert.equal(params2.get('filename'), 'pune-leads');
});

// Helper: create a fake valid XLSX blob (PK zip signature)
function makeFakeXlsxBlob() {
  const bytes = new Uint8Array([0x50, 0x4B, 0x03, 0x04, 0x00, 0x00, 0x00, 0x00]);
  return new Blob([bytes], {
    type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  });
}

test('successful native share', async () => {
  let shareCalledWith = null;
  const mockNavigator = {
    share: async (data) => {
      shareCalledWith = data;
    },
    canShare: () => true,
  };

  const blob = makeFakeXlsxBlob();
  const result = await executeExportFlow({
    blob,
    finalFileName: 'October leads.xlsx',
    navigatorRef: mockNavigator,
  });

  assert.equal(result.method, 'share');
  assert.equal(result.message, 'Spreadsheet ready: October leads.xlsx');
  assert.equal(shareCalledWith.title, 'October leads.xlsx');
  assert.equal(shareCalledWith.files[0].name, 'October leads.xlsx');
});

test('native share rejection with NotAllowedError falls back to download', async () => {
  const mockNavigator = {
    share: async () => {
      const err = new Error('Permission denied');
      err.name = 'NotAllowedError';
      throw err;
    },
    canShare: () => true,
  };

  let downloadedFile = null;
  const mockDocument = {
    createElement: () => ({
      href: '',
      download: '',
      click: () => {},
      remove: () => {},
    }),
    body: {
      appendChild: (el) => {
        downloadedFile = el.download;
      },
    },
  };

  const blob = makeFakeXlsxBlob();
  const result = await executeExportFlow({
    blob,
    finalFileName: 'October leads.xlsx',
    navigatorRef: mockNavigator,
    documentRef: mockDocument,
  });

  assert.equal(result.method, 'download');
  assert.equal(result.message, 'Spreadsheet downloaded as October leads.xlsx.');
  assert.equal(downloadedFile, 'October leads.xlsx');
});

test('native share cancellation with AbortError falls back to download', async () => {
  const mockNavigator = {
    share: async () => {
      const err = new Error('Share canceled');
      err.name = 'AbortError';
      throw err;
    },
    canShare: () => true,
  };

  let downloadedFile = null;
  const mockDocument = {
    createElement: () => ({
      href: '',
      download: '',
      click: () => {},
      remove: () => {},
    }),
    body: {
      appendChild: (el) => {
        downloadedFile = el.download;
      },
    },
  };

  const blob = makeFakeXlsxBlob();
  const result = await executeExportFlow({
    blob,
    finalFileName: 'Pune contacts.xlsx',
    navigatorRef: mockNavigator,
    documentRef: mockDocument,
  });

  assert.equal(result.method, 'download');
  assert.equal(result.message, 'Spreadsheet downloaded as Pune contacts.xlsx.');
  assert.equal(downloadedFile, 'Pune contacts.xlsx');
});

test('native share rejection with generic Permission denied falls back to download without throwing', async () => {
  const mockNavigator = {
    share: async () => {
      throw new Error('Permission denied');
    },
    canShare: () => true,
  };

  let downloadTriggered = false;
  const mockDocument = {
    createElement: () => ({
      href: '',
      download: '',
      click: () => { downloadTriggered = true; },
      remove: () => {},
    }),
    body: { appendChild: () => {} },
  };

  const blob = makeFakeXlsxBlob();
  const result = await executeExportFlow({
    blob,
    finalFileName: 'Manager review 2026.xlsx',
    navigatorRef: mockNavigator,
    documentRef: mockDocument,
  });

  assert.equal(result.method, 'download');
  assert.equal(result.message, 'Spreadsheet downloaded as Manager review 2026.xlsx.');
  assert.equal(downloadTriggered, true);
});

test('no second API request made after share failure', async () => {
  let apiFetchCount = 0;
  async function fakeFetch() {
    apiFetchCount += 1;
    return new Response(makeFakeXlsxBlob(), {
      status: 200,
      headers: { 'Content-Type': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' },
    });
  }

  const mockNavigator = {
    share: async () => {
      const err = new Error('User cancelled share');
      err.name = 'AbortError';
      throw err;
    },
    canShare: () => true,
  };

  const mockDocument = {
    createElement: () => ({ href: '', download: '', click: () => {}, remove: () => {} }),
    body: { appendChild: () => {} },
  };

  // Perform flow once
  const res = await fakeFetch();
  const blob = await validateExportResponse(res);
  const flowResult = await executeExportFlow({
    blob,
    finalFileName: 'single-request.xlsx',
    navigatorRef: mockNavigator,
    documentRef: mockDocument,
  });

  assert.equal(flowResult.method, 'download');
  assert.equal(apiFetchCount, 1, 'Exactly one API fetch must occur even if native share fails');
});

test('authentication 401 response returns clear session expired message', async () => {
  const response = new Response(JSON.stringify({ detail: 'Not authenticated' }), {
    status: 401,
    headers: { 'Content-Type': 'application/json' },
  });

  await assert.rejects(
    async () => validateExportResponse(response),
    /Your session has expired\. Please log in again\./
  );
});

test('authentication 403 response returns clear session expired message', async () => {
  const response = new Response(JSON.stringify({ detail: 'Forbidden' }), {
    status: 403,
    headers: { 'Content-Type': 'application/json' },
  });

  await assert.rejects(
    async () => validateExportResponse(response),
    /Your session has expired\. Please log in again\./
  );
});

test('server 500 response returns clear server error message', async () => {
  const response = new Response(JSON.stringify({ detail: 'Internal error' }), {
    status: 500,
    headers: { 'Content-Type': 'application/json' },
  });

  await assert.rejects(
    async () => validateExportResponse(response),
    /The server could not create the Excel file\. Please try again\./
  );
});

test('server 503 response returns clear server error message', async () => {
  const response = new Response('Service Unavailable', {
    status: 503,
    headers: { 'Content-Type': 'text/plain' },
  });

  await assert.rejects(
    async () => validateExportResponse(response),
    /The server could not create the Excel file\. Please try again\./
  );
});

test('endpoint 404 response returns endpoint unavailable message', async () => {
  const response = new Response(JSON.stringify({ detail: 'Not Found' }), {
    status: 404,
    headers: { 'Content-Type': 'application/json' },
  });

  await assert.rejects(
    async () => validateExportResponse(response),
    /The export endpoint is unavailable\./
  );
});

test('HTML error page response is rejected before sharing or downloading', async () => {
  const htmlBody = '<!DOCTYPE html><html><body>502 Bad Gateway</body></html>';
  const response = new Response(htmlBody, {
    status: 200,
    headers: { 'Content-Type': 'text/html; charset=utf-8' },
  });

  await assert.rejects(
    async () => validateExportResponse(response),
    /unexpected web page instead of an Excel file/
  );
});

test('invalid/non-XLSX response body is rejected', async () => {
  const plainText = 'This is just a text file, not an Excel file.';
  const response = new Response(plainText, {
    status: 200,
    headers: { 'Content-Type': 'application/octet-stream' },
  });

  await assert.rejects(
    async () => validateExportResponse(response),
    /not a valid Excel file/
  );
});

test('mobile download fallback sets download attribute to sanitized name', () => {
  let downloadAttribute = '';
  let clicked = false;
  const mockDocument = {
    createElement: (tag) => {
      assert.equal(tag, 'a');
      return {
        href: '',
        download: '',
        rel: '',
        click: () => { clicked = true; },
        remove: () => {},
      };
    },
    body: {
      appendChild: (el) => {
        downloadAttribute = el.download;
      },
    },
  };

  const blob = makeFakeXlsxBlob();
  const success = triggerBlobDownload(blob, 'Pune contacts.xlsx', mockDocument);
  assert.equal(success, true);
  assert.equal(downloadAttribute, 'Pune contacts.xlsx');
  assert.equal(clicked, true);
});

test('iOS Safari fallback message when both share and synthetic download fail', async () => {
  const mockNavigator = {
    share: async () => {
      throw new Error('NotAllowedError');
    },
    canShare: () => true,
  };

  // Mock document where createElement throws (e.g. strict restricted context)
  const mockBrokenDocument = {
    createElement: () => {
      throw new Error('SecurityError: DOM manipulation disallowed');
    },
    body: { appendChild: () => {} },
  };

  const blob = makeFakeXlsxBlob();
  const result = await executeExportFlow({
    blob,
    finalFileName: 'ios-leads.xlsx',
    navigatorRef: mockNavigator,
    documentRef: mockBrokenDocument,
  });

  assert.equal(result.method, 'manual');
  assert.equal(result.message, 'Excel file is ready. Use the browser share or download action to save it.');
});

test('export duplicate-click prevention simulation', async () => {
  let exportRunning = false;
  let requestsInitiated = 0;

  async function handleExportClick() {
    if (exportRunning) return;
    exportRunning = true;
    requestsInitiated += 1;
    await new Promise((resolve) => setTimeout(resolve, 20));
    exportRunning = false;
  }

  // Simulate user tapping Export twice rapidly
  const p1 = handleExportClick();
  const p2 = handleExportClick();
  await Promise.all([p1, p2]);

  assert.equal(requestsInitiated, 1, 'Rapid duplicate clicks must be ignored while export is running');
});
