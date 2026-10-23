const { test } = require('node:test');
const assert = require('node:assert/strict');
const { parseQrPayload } = require('../qr.js');

test('vCard parses fields and preserves multiple phone numbers separately', () => {
  const value=parseQrPayload('BEGIN:VCARD\nFN:Jane Doe\nORG:Acme Pvt Ltd\nADR:;;Main Road;Mumbai;MH;400001;India\nTEL:+919876543210\nTEL:+919876543211\nEMAIL:jane@acme.example\nEND:VCARD');
  assert.equal(value.owner_name,'Jane Doe'); assert.equal(value.company_name,'Acme Pvt Ltd');
  assert.deepEqual(value.contacts,['+919876543210','+919876543211']); assert.equal(value.company_address,'Main Road, Mumbai, MH, 400001, India');
});

test('phone, email, social and location formats map to their appropriate fields', () => {
  assert.deepEqual(parseQrPayload('tel:+919876543210').contacts,['+919876543210']);
  assert.deepEqual(parseQrPayload('mailto:team@acme.example').emails,['team@acme.example']);
  assert.equal(parseQrPayload('https://instagram.com/acme').instagram_link,'https://instagram.com/acme');
  assert.equal(parseQrPayload('geo:19.076,72.8777').location_coordinates.latitude,19.076);
  assert.equal(parseQrPayload('https://maps.google.com/?q=19.076,72.8777').website,undefined);
});

test('unsafe or unknown payloads stay as source text and are never opened or fetched', () => {
  const value=parseQrPayload('https://user:secret@unknown.example/path');
  assert.equal(value.unmapped,true); assert.equal(value.urls.length,0); assert.equal(value.source_text,'https://user:secret@unknown.example/path');
});

test('QR redirect service URLs remain source data, not company websites', () => {
  const value = parseQrPayload('https://api.singleqr.in/r/example');
  assert.equal(value.website, undefined);
  assert.equal(value.qr_redirect_url, 'https://api.singleqr.in/r/example');
  assert.equal(value.source_text, 'https://api.singleqr.in/r/example');
});
