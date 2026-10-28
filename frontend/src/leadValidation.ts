export interface LeadValidationInput {
  company_name?: string | null;
  contact_numbers?: string[] | null;
}

export interface LeadValidationResult {
  valid: boolean;
  error?: string;
  firstMissingField?: "company_name" | "contact_numbers";
  trimmedCompanyName: string;
  cleanedContactNumbers: string[];
}

export const INCOMPLETE_LEAD_ERROR_MESSAGE = "Company name and at least one contact number are required.";

export function validateLead(input: LeadValidationInput): LeadValidationResult {
  const trimmedCompanyName = (input.company_name || "").trim();
  const seenDigits = new Set<string>();
  const cleanedContactNumbers: string[] = [];

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
      error: INCOMPLETE_LEAD_ERROR_MESSAGE,
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

