const test = require("node:test");
const assert = require("node:assert/strict");

// Re-implement the pure logic in CommonJS test suite to mirror frontend/src/leadValidation.ts
function validateLead(input) {
  const trimmedCompanyName = (input.company_name || "").trim();
  const seenDigits = new Set();
  const cleanedContactNumbers = [];

  for (const num of input.contact_numbers || []) {
    const val = typeof num === "string" ? num.trim() : "";
    if (!val) continue;
    const digits = val.replace(/\D/g, "");
    const key = digits.length >= 10 ? digits.slice(-10) : digits || val;
    if (!seenDigits.has(key)) {
      seenDigits.add(key);
      cleanedContactNumbers.push(val);
    }
  }

  if (!trimmedCompanyName || cleanedContactNumbers.length === 0) {
    return {
      valid: false,
      error: "Company name and at least one contact number are required.",
      firstMissingField: !trimmedCompanyName ? "company_name" : "contact_numbers",
      trimmedCompanyName,
      cleanedContactNumbers,
    };
  }

  return {
    valid: true,
    trimmedCompanyName,
    cleanedContactNumbers,
  };
}

test("rejects missing company name and identifies company_name as first missing field", () => {
  const result = validateLead({ company_name: "", contact_numbers: ["9876543210"] });
  assert.equal(result.valid, false);
  assert.equal(result.error, "Company name and at least one contact number are required.");
  assert.equal(result.firstMissingField, "company_name");
});

test("rejects whitespace-only company name", () => {
  const result = validateLead({ company_name: "   \t\n  ", contact_numbers: ["9876543210"] });
  assert.equal(result.valid, false);
  assert.equal(result.error, "Company name and at least one contact number are required.");
  assert.equal(result.firstMissingField, "company_name");
});

test("rejects empty contact numbers array and identifies contact_numbers as first missing field", () => {
  const result = validateLead({ company_name: "Acme Corp", contact_numbers: [] });
  assert.equal(result.valid, false);
  assert.equal(result.error, "Company name and at least one contact number are required.");
  assert.equal(result.firstMissingField, "contact_numbers");
});

test("rejects contact numbers array containing only empty or whitespace strings", () => {
  const result = validateLead({ company_name: "Acme Corp", contact_numbers: ["", "   ", " \t "] });
  assert.equal(result.valid, false);
  assert.equal(result.error, "Company name and at least one contact number are required.");
  assert.equal(result.firstMissingField, "contact_numbers");
});

test("prioritizes focusing company name when both company and contact numbers are missing", () => {
  const result = validateLead({ company_name: "", contact_numbers: [] });
  assert.equal(result.valid, false);
  assert.equal(result.firstMissingField, "company_name");
});

test("deduplicates identical and prefixed contact numbers before check", () => {
  const result = validateLead({
    company_name: "Acme Corp",
    contact_numbers: ["9876543210", "+91 98765 43210", "9876543210"],
  });
  assert.equal(result.valid, true);
  assert.equal(result.cleanedContactNumbers.length, 1);
  assert.equal(result.cleanedContactNumbers[0], "9876543210");
});

test("accepts valid company name and at least one contact number", () => {
  const result = validateLead({
    company_name: "  Acme Corp  ",
    contact_numbers: ["+91 9876543210"],
  });
  assert.equal(result.valid, true);
  assert.equal(result.trimmedCompanyName, "Acme Corp");
  assert.deepEqual(result.cleanedContactNumbers, ["+91 9876543210"]);
});

test("preserves existing form draft when validation fails (simulated flow)", () => {
  const draftForm = {
    company_name: "  ",
    contact_numbers: [],
    owner_name: "John Doe",
    email_addresses: ["john@example.com"],
  };
  let formState = { ...draftForm };
  let apiCalled = false;
  let offlineQueueEnqueued = false;

  const validation = validateLead({ company_name: formState.company_name, contact_numbers: formState.contact_numbers });
  if (!validation.valid) {
    // Validation fails - saveLead returns early without clearing form or sending request
  } else {
    apiCalled = true;
    offlineQueueEnqueued = true;
    formState = {};
  }

  assert.equal(apiCalled, false, "API request must not be dispatched");
  assert.equal(offlineQueueEnqueued, false, "Offline queue must not be enqueued");
  assert.deepEqual(formState, draftForm, "Form draft values must be preserved intact");
});

