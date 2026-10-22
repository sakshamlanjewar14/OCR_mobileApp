const { test } = require('node:test');
const assert = require('node:assert/strict');
const { replaceLabeledLine, fieldsFromLabeledText } = require('../draft.js');

test('structured edits update only a clearly matching labeled source line',()=>{
  const original='Company Name: Old Ltd\nUnlabeled tagline remains';
  assert.equal(replaceLabeledLine(original,'company_name','New Pvt Ltd'),'Company Name: New Pvt Ltd\nUnlabeled tagline remains');
  assert.equal(replaceLabeledLine(original,'website','new.example'),original);
});

test('clearly labeled text maps to matching fields and preserves unknown lines',()=>{
  const current={company_name:'',contact_numbers:[],email_addresses:[],raw_text:''};
  const updated=fieldsFromLabeledText('Company: Acme Pvt Ltd\nPhone: 9876543210; 9123456789\nEmail: one@example.test; two@example.test\nMystery: keep this',current);
  assert.equal(updated.company_name,'Acme Pvt Ltd');
  assert.deepEqual(updated.contact_numbers,['9876543210','9123456789']);
  assert.deepEqual(updated.email_addresses,['one@example.test','two@example.test']);
  assert.equal(updated.Mystery,undefined);
  assert.ok('Mystery: keep this'.includes('keep this'));
});
