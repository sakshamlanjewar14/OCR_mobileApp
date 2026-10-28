import { ChangeEvent, FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import "./details.css";
import { clearLeadDraft, enqueueLead, readLeadDraft, readLeadQueue, removeQueuedLead, saveLeadDraft } from "./draftStore";
import { decodeQrFromVideo, parseQrLocally, recognizeCard, scanQrImage } from "./clientOcr";
import { startQrScanLoop } from "./qrScanner";
import { DEFAULT_EXPORT_BASE_NAME, executeExportFlow, sanitizeExportFileName, validateExportResponse } from "./exportFileName";
import { validateLead } from "./leadValidation";

// Development uses the Vite same-origin proxy so cookie authentication is not
// blocked by browser CORS. Production keeps the configured API origin.
// Vite environment values are baked into the production bundle. Keep a
// working hosted fallback so a missing VITE_API_URL cannot point a deployed
// phone app at the Vercel frontend host on port 8000.
const API = import.meta.env.VITE_API_URL || (import.meta.env.DEV ? "/api" : "https://ocr-mobileapp-3.onrender.com/api");

type Lead = {
  id: string;
  lead_number: string;
  company_name: string;
  owner_name: string | null;
  designation: string | null;
  business_description: string | null;
  company_address: string | null;
  contact_number: string | null;
  contact_numbers: string[];
  email: string | null;
  email_addresses: string[];
  website: string | null;
  facebook_link: string | null;
  instagram_link: string | null;
  whatsapp_link: string | null;
  source_document_ids: string[];
  raw_text: string | null;
  original_ocr_text: string | null;
  document_id: string | null;
  verification_status: "verified" | "needs_review";
};

type LeadForm = {
  company_name: string;
  owner_name: string;
  designation: string;
  business_description: string;
  company_address: string;
  contact_numbers: string[];
  email_addresses: string[];
  website: string;
  facebook_link: string;
  instagram_link: string;
  whatsapp_link: string;
  raw_text: string;
};
type ExtractedLeadFields = Omit<Partial<Record<keyof LeadForm, string | null>>, "contact_numbers" | "email_addresses"> & {
  contact_number?: string | null;
  email?: string | null;
  contact_numbers?: string[] | null;
  email_addresses?: string[] | null;
  city?: string | null;
  district?: string | null;
  state?: string | null;
  cards?: ExtractedLeadFields[] | null;
  original_ocr_text?: string | null;
  translation_status?: string | null;
  translation_message?: string | null;
  review_warnings?: string[];
  qr_payloads?: string[];
  qr_data?: Record<string, unknown>[];
  field_sources?: Record<string, { source_type: string; source_text: string; confidence: string }>;
  processing_times_ms?: { ocr_ms?: number; translation_ms?: number; translation_model_init_ms?: number; translation_inference_ms?: number; field_extraction_ms?: number; decode_ms?: number; preprocess_ms?: number; qr_ms?: number; queue_ms?: number; total_ms?: number; image_width?: number; image_height?: number; image_bytes?: number };
};
type DocumentRecord = {
  id: string;
  original_filename: string;
  content_type: string;
  processing_status: string;
  extracted_fields: ExtractedLeadFields;
  error_message: string | null;
  lead_id: string | null;
  lead_count: number;
  resolved_card_indexes: number[];
};
type DocumentStatus = Pick<DocumentRecord, "id" | "processing_status" | "error_message" | "lead_count">;
type DocumentBatchResult = { documents: DocumentRecord[]; skipped_files: { filename: string; reason: string }[] };
type RawCandidate = { source: string; row: number; fields: ExtractedLeadFields };
type RawPreviewResult = { filename: string; candidates: { row: number; fields: ExtractedLeadFields }[] };
type QRResult = { source_text?: string; contacts?: string[]; emails?: string[]; urls?: string[]; [key: string]: unknown };
type WebsitePreview = { url: string; fields: Record<string, unknown>; source_text: string; warnings: string[] };
type DuplicateCheckResult = {
  is_duplicate: boolean;
  existing_lead_id?: string | null;
  existing_lead_number?: string | null;
  existing_company_name?: string | null;
  matched_type?: string | null;
  matched_value?: string | null;
};
type LeadPage = { leads: Lead[]; total: number; page: number; page_size: number };
const LEADS_PAGE_SIZE = 50;

async function optimizeCameraPhoto(blob: Blob): Promise<Blob> {
  if (blob.size <= 1_000_000) return blob;
  if (typeof createImageBitmap !== "function") return blob;
  let bitmap: ImageBitmap;
  try {
    bitmap = await createImageBitmap(blob);
  } catch {
    return blob;
  }
  try {
    // The backend OCR pipeline caps images at 1600 px. Sending an
    // 8–12 MP phone photo over cellular only adds upload time without giving
    // OCR more pixels to read, so retain that effective resolution at high
    // JPEG quality before upload.
    const scale = Math.min(1, 1600 / Math.max(bitmap.width, bitmap.height));
    const canvas = window.document.createElement("canvas");
    canvas.width = Math.max(1, Math.round(bitmap.width * scale));
    canvas.height = Math.max(1, Math.round(bitmap.height * scale));
    const context = canvas.getContext("2d");
    if (!context) return blob;
    context.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
    const optimized = await new Promise<Blob | null>((resolve) => canvas.toBlob(resolve, "image/jpeg", 0.9));
    return optimized && optimized.size < blob.size ? optimized : blob;
  } finally {
    bitmap.close();
  }
}

function mergeExtractedFields(records: DocumentRecord[]): ExtractedLeadFields {
  const merged: ExtractedLeadFields = {};
  for (const key of ["company_name", "owner_name", "designation", "business_description", "company_address", "website", "facebook_link", "instagram_link", "whatsapp_link"] as const) {
    const value = records.map((record) => record.extracted_fields?.[key]).find((item) => typeof item === "string" && item.trim());
    if (value) merged[key] = value as string;
  }
  merged.contact_numbers = stringValues(records.flatMap((record) => record.extracted_fields?.contact_numbers || []));
  merged.email_addresses = stringValues(records.flatMap((record) => record.extracted_fields?.email_addresses || []));
  merged.raw_text = records.map((record) => record.extracted_fields?.raw_text).filter((item): item is string => Boolean(item)).join("\n\n");
  merged.original_ocr_text = records.map((record) => record.extracted_fields?.original_ocr_text).filter((item): item is string => Boolean(item)).join("\n\n");
  merged.review_warnings = [...new Set(records.flatMap((record) => record.extracted_fields?.review_warnings || []))];
  merged.qr_payloads = records.flatMap((record) => record.extracted_fields?.qr_payloads || []);
  return merged;
}

function mergeLocalCards(records: ExtractedLeadFields[]): { fields: ExtractedLeadFields; conflicts: string[] } {
  const fields: ExtractedLeadFields = {};
  const conflicts: string[] = [];
  const scalarKeys = ["company_name", "owner_name", "designation", "business_description", "company_address", "website", "facebook_link", "instagram_link", "whatsapp_link"] as const;
  for (const key of scalarKeys) {
    const values = [...new Set(records.map((item) => textValue(item[key]).trim()).filter(Boolean))];
    if (values.length) fields[key] = key === "company_address" ? combineAddress(...values) : values[0];
    if (values.length > 1 && key !== "company_address") conflicts.push(`${key.replace(/_/g, " ")}: ${values.join(" / ")}`);
  }
  fields.contact_numbers = stringValues(records.flatMap((item) => item.contact_numbers || []));
  fields.email_addresses = stringValues(records.flatMap((item) => item.email_addresses || []));
  fields.raw_text = records.map((item) => textValue(item.raw_text)).filter(Boolean).join("\n\n");
  fields.original_ocr_text = records.map((item) => textValue(item.original_ocr_text)).filter(Boolean).join("\n\n");
  fields.field_sources = Object.assign({}, ...records.map((item) => item.field_sources || {}));
  return { fields, conflicts };
}

const emptyForm: LeadForm = {
  company_name: "",
  owner_name: "",
  designation: "",
  business_description: "",
  company_address: "",
  contact_numbers: [],
  email_addresses: [],
  website: "",
  facebook_link: "",
  instagram_link: "",
  whatsapp_link: "",
  raw_text: "",
};

const fields: { key: "company_name" | "owner_name" | "designation" | "business_description" | "company_address" | "website" | "facebook_link" | "instagram_link" | "whatsapp_link"; label: string; placeholder: string }[] = [
  { key: "company_name", label: "Full company name", placeholder: "Example Technologies Pvt Ltd" },
  { key: "owner_name", label: "Owner name", placeholder: "Contact person" },
  { key: "designation", label: "Designation", placeholder: "Director, Manager, etc." },
  { key: "business_description", label: "Business description / services", placeholder: "Products or services offered" },
  { key: "company_address", label: "Address", placeholder: "Enter address" },
  { key: "website", label: "Website", placeholder: "www.company.com" },
  { key: "facebook_link", label: "Facebook link", placeholder: "https://facebook.com/company" },
  { key: "instagram_link", label: "Instagram link", placeholder: "https://instagram.com/company" },
  { key: "whatsapp_link", label: "WhatsApp link", placeholder: "https://wa.me/919876543210" },
];

function combineAddress(...parts: (string | null | undefined)[]): string {
  const result = parts.map((part) => part?.trim()).filter((part): part is string => Boolean(part));
  const combined: string[] = [];
  for (const part of result) {
    const normalized = part.toLocaleLowerCase().replace(/[^a-z0-9]/g, "");
    if (!normalized || combined.some((existing) => existing.toLocaleLowerCase().replace(/[^a-z0-9]/g, "").includes(normalized))) continue;
    const completeAddress = combined.findIndex((existing) => normalized.includes(existing.toLocaleLowerCase().replace(/[^a-z0-9]/g, "")));
    if (completeAddress >= 0) {
      combined[completeAddress] = part;
      continue;
    }
    combined.push(part);
  }
  return combined.join(", ");
}

function textValue(value: string | null | undefined): string {
  return typeof value === "string" ? value : "";
}

function safeText(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function stringValues(values: string[] | null | undefined, fallback?: string | null): string[] {
  const source = Array.isArray(values) ? values : fallback ? [fallback] : [];
  return [...new Set(source.filter((value): value is string => typeof value === "string").map((value) => value.trim()).filter(Boolean))];
}

function listFromText(value: string, separators: RegExp = /[\n;]+/): string[] {
  return value.split(separators).map((item) => item.trim()).filter(Boolean);
}

const EDITED_PREVIEW_MARKER = "[Edited preview]";
function syncedRawText(form: LeadForm): string {
  const base = form.raw_text.split(/\n\s*\[Edited preview\]\s*\n/i)[0].trimEnd();
  const lines = [
    ["Company name", form.company_name], ["Owner name", form.owner_name], ["Designation", form.designation], ["Business description", form.business_description],
    ["Address", form.company_address], ["Contact numbers", form.contact_numbers.join("; ")],
    ["Email addresses", form.email_addresses.join("; ")], ["Website", form.website],
    ["Facebook", form.facebook_link], ["Instagram", form.instagram_link], ["WhatsApp", form.whatsapp_link],
  ].filter(([, value]) => value.trim()).map(([label, value]) => `${label}: ${value}`);
  return lines.length ? `${base}\n\n${EDITED_PREVIEW_MARKER}\n${lines.join("\n")}`.trim() : base;
}

function updateFormField(current: LeadForm, patch: Partial<LeadForm>): LeadForm {
  const next = { ...current, ...patch };
  return { ...next, raw_text: syncedRawText(next) };
}

function updateFormFromDocumentText(current: LeadForm, rawText: string): LeadForm {
  const next: Partial<LeadForm> = {};
  const value = (label: string) => rawText.match(new RegExp(`^\\s*${label}\\s*:\\s*(.*)$`, "im"))?.[1]?.trim();
  const company = value("(?:full\\s+)?company(?:\\s+name)?");
  const owner = value("owner(?:\\s+name)?|contact(?:\\s+person)?");
  const designation = value("designation|title|position");
  const description = value("business description|company description|description|services");
  const address = value("address");
  const contact = value("contact(?:\\s+numbers?)?|phone(?:\\s+numbers?)?");
  const emails = value("emails?|email\\s+addresses?");
  if (company !== undefined) next.company_name = company;
  if (owner !== undefined) next.owner_name = owner;
  if (designation !== undefined) next.designation = designation;
  if (description !== undefined) next.business_description = description;
  if (address !== undefined) next.company_address = address;
  if (contact !== undefined) next.contact_numbers = listFromText(contact, /[;,]+/);
  if (emails !== undefined) next.email_addresses = listFromText(emails, /[;,]+/);
  for (const [key, label] of [["website", "website"], ["facebook_link", "facebook"], ["instagram_link", "instagram"], ["whatsapp_link", "whatsapp"]] as const) {
    const item = value(label);
    if (item !== undefined) next[key] = item;
  }
  return { ...current, ...next, raw_text: rawText };
}

function mergeValues(current: string[], extracted?: string[] | null): string[] {
  return stringValues([...(Array.isArray(current) ? current : []), ...(Array.isArray(extracted) ? extracted : [])]);
}

function indianPhone(value: string): string {
  const digits = value.replace(/\D/g, "");
  if (value.trim().startsWith("+")) return value.trim();
  if (digits.length === 10) return `+91 ${digits}`;
  if (digits.length === 11 && digits.startsWith("0") && /^[6-9]/.test(digits.slice(1))) return `+91 ${digits.slice(1)}`;
  if (digits.length === 12 && digits.startsWith("91") && /^[6-9]/.test(digits.slice(2))) return `+91 ${digits.slice(2)}`;
  if (digits.length === 14 && digits.startsWith("0091") && /^[6-9]/.test(digits.slice(4))) return `+91 ${digits.slice(4)}`;
  return value.trim();
}

function formFromExtracted(extracted: ExtractedLeadFields, cardIndex = 0): LeadForm {
  const candidate = extracted.cards?.[cardIndex] || extracted;
  return {
    company_name: textValue(candidate.company_name),
    owner_name: textValue(candidate.owner_name),
    designation: textValue(candidate.designation),
    business_description: textValue(candidate.business_description),
    company_address: textValue(candidate.company_address),
    contact_numbers: stringValues(candidate.contact_numbers, candidate.contact_number).map(indianPhone),
    email_addresses: stringValues(candidate.email_addresses, candidate.email),
    website: textValue(candidate.website),
    facebook_link: textValue(candidate.facebook_link),
    instagram_link: textValue(candidate.instagram_link),
    whatsapp_link: textValue(candidate.whatsapp_link),
    raw_text: textValue(candidate.raw_text || extracted.raw_text),
  };
}

function mergeDocuments(current: DocumentRecord[], incoming: DocumentRecord[]): DocumentRecord[] {
  return Array.from(new Map([...current, ...incoming].map((item) => [item.id, item])).values());
}

function sourceContainsField(value: string, sourceTokens: Set<string>): boolean {
  const tokens = value.normalize("NFKD").toLocaleLowerCase().replace(/[\u0300-\u036f]/g, "").match(/[a-z0-9]+/g) || [];
  if (!tokens.length) return true;
  const matches = tokens.filter((token) => sourceTokens.has(token)).length;
  // OCR commonly loses a character or one short word. Allow one missed token,
  // while requiring every token for a one-word value.
  const required = tokens.length === 1 ? 1 : Math.max(tokens.length - 1, Math.ceil(tokens.length * 0.8));
  return matches >= required;
}

function addressTextFromSource(source: string): string {
  const lines = source.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  const addressLabel = /^\s*(?:(?:office|registered|company)\s+)*address\s*[:#-]*\s*/i;
  const labelledIndex = lines.findIndex((line) => addressLabel.test(line));
  const addressMarker = /\b(road|rd\.?|street|st\.?|avenue|ave\.?|lane|suite|floor|block|sector|near|complex|building|tower|plot|area|landmark|district|state|province|region|city|town|\d{5,6})\b/i;
  const boundary = /@|https?:\/\/|www\.|\b(tel\.?|telephone|mobile|phone|email|e-mail|web|website|fax|director|manager|ceo|owner|founder|mto|iso)\b/i;
  const phone = /\+?\d(?:[ \t().-]*\d){6,14}/;
  if (labelledIndex >= 0) {
    const addressLines: string[] = [];
    for (const [index, rawLine] of lines.entries()) {
      if (index < labelledIndex) continue;
      let line = index === labelledIndex ? rawLine.replace(addressLabel, "").trim() : rawLine;
      if (!line || boundary.test(line)) break;
      const phoneMatch = phone.exec(line);
      if (phoneMatch) line = line.slice(0, phoneMatch.index).trim();
      if (line) addressLines.push(line);
      if (phoneMatch) break;
    }
    return addressLines.join(" ");
  }
  return lines.filter((line) => addressMarker.test(line) && !boundary.test(line) && !phone.test(line)).join(" ");
}

function reviewMismatches(form: LeadForm): string[] {
  const source = textValue(form.raw_text).trim();
  if (!source) return [];
  const sourceTokens = new Set(source.normalize("NFKD").toLocaleLowerCase().replace(/[\u0300-\u036f]/g, "").match(/[a-z0-9]+/g) || []);
  const addressTokens = new Set(addressTextFromSource(source).normalize("NFKD").toLocaleLowerCase().replace(/[\u0300-\u036f]/g, "").match(/[a-z0-9]+/g) || []);
  const mismatches = new Set<string>();
  const checkText = (label: string, value: string, tokens = sourceTokens) => {
    const fieldValue = textValue(value);
    if (fieldValue.trim() && !sourceContainsField(fieldValue, tokens)) mismatches.add(label);
  };
  const companyName = textValue(form.company_name);
  const ownerName = textValue(form.owner_name);
  const designation = textValue(form.designation);
  checkText("Full company name", companyName);
  checkText("Owner name", ownerName);
  checkText("Designation", designation);
  const promotionalText = /\b(certified|authori[sz]ed|official|channel|solutions?|provider|dealer|distributor|welcome|quality|trusted|leading|serving|partner of)\b/i;
  const roleText = /\b(chief|director|manager|owner|founder|ceo|cto|cfo|president|partner|head|sales|marketing|consultant|engineer|officer|executive|administrator|coordinator|developer|architect|accountant)\b/i;
  const companyEntityText = /\b(incorporated|inc\.?|llc|ltd\.?|limited|private|corporation|corp\.?|technologies|solutions?|services|company|group|industries|shipping|logistics|foundation|trust|association|society|institute|university|bank|hospital|clinic|school|college|academy|agency|studio|consulting|enterprise|pvt\.?)\b/i;
  const companyParts = companyName.trim().split(/\s+/);
  const sourceHasCompanyLabel = /(?:^|\n)\s*(?:full\s+company\s+name|company(?:\s+(?:full\s+)?name)?|business\s+name|firm\s+name|organization(?:\s+name)?)\s*:\s*\S/im.test(source);
  if (companyName.trim() && !sourceHasCompanyLabel && !companyEntityText.test(companyName) && companyParts.length >= 2 && companyParts.length <= 4 && companyParts.every((part) => /^[\p{L}][\p{L}'.-]*$/u.test(part))) {
    mismatches.add("Company name may contain a contact person's name instead of the business name");
  }
  if (ownerName.trim()) {
    const personName = ownerName.trim().replace(/^(?:mr|mrs|ms|miss|dr|prof)\.?\s+/i, "");
    const nameParts = personName.split(/\s+/);
    if (nameParts.length < 2 || nameParts.length > 4 || !nameParts.every((part) => /^[\p{L}][\p{L}'.-]*$/u.test(part)) || promotionalText.test(ownerName) || roleText.test(ownerName)) {
      mismatches.add("Owner name may be a tagline, role, or incomplete name");
    }
  }
  if (designation.trim() && (!roleText.test(designation) || promotionalText.test(designation))) {
    mismatches.add("Designation does not look like a job title");
  }
  if (ownerName.trim() && designation.trim() && ownerName.trim().toLocaleLowerCase() === designation.trim().toLocaleLowerCase()) {
    mismatches.add("Owner name and designation contain the same value");
  }
  const legalSuffix = /\b(private|pvt\.?|limited|ltd\.?|llp|incorporated|inc\.?|corporation|corp\.?)\b/i;
  if (companyName.trim() && legalSuffix.test(source) && !legalSuffix.test(companyName)) {
    mismatches.add("Company legal suffix may be missing");
  }
  const addressValue = textValue(form.company_address);
  if (addressValue.trim() && !sourceContainsField(addressValue, addressTokens) && !sourceContainsField(addressValue, sourceTokens)) {
    mismatches.add("Address");
  }
  const websiteValue = textValue(form.website);
  if (websiteValue.trim()) {
    const website = websiteValue.toLocaleLowerCase().replace(/^https?:\/\//, "").replace(/^www\./, "").replace(/\/$/, "");
    const sourceWebsite = source.toLocaleLowerCase().replace(/^https?:\/\//, "").replace(/^www\./, "");
    if (!sourceWebsite.includes(website)) mismatches.add("Website");
  }
  const phoneDigits = (value: string) => value.replace(/\D/g, "");
  const phones = stringValues(form.contact_numbers).map(phoneDigits).filter((digits) => digits.length >= 7);
  const samePhone = (left: string, right: string) => left === right || (Math.min(left.length, right.length) >= 10 && left.slice(-10) === right.slice(-10));
  const sourcePhones = [...source.matchAll(/(?<!\d)\+?\d(?:[ \t().-]*\d){6,14}(?!\d)/g)]
    .map((match) => phoneDigits(match[0])).filter((digits) => digits.length >= 7);
  if (phones.some((number) => !sourcePhones.some((sourceNumber) => samePhone(number, sourceNumber)))) {
    mismatches.add("A saved phone number is not found in the source");
  }
  if (sourcePhones.some((number) => !phones.some((savedNumber) => samePhone(number, savedNumber)))) {
    mismatches.add("A phone number from the source may be missing");
  }
  const emails = stringValues(form.email_addresses).map((address) => address.toLocaleLowerCase().trim()).filter(Boolean);
  const sourceEmails = [...source.matchAll(/[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/gi)].map((match) => match[0].toLocaleLowerCase());
  if (emails.some((address) => !sourceEmails.includes(address))) {
    mismatches.add("A saved email address is not found in the source");
  }
  if (sourceEmails.some((address) => !emails.includes(address))) {
    mismatches.add("An email address from the source may be missing");
  }
  const addressLines = source.split(/\r?\n/).filter((line) =>
    /\b(address|road|rd\.?|street|st\.?|avenue|ave\.?|lane|suite|floor|block|sector|near|complex|building|tower|plot|area|landmark|district|state|province|region|city|town|pin|postal|zip|india)\b/i.test(line)
    && !/\b(phone|mobile|telephone|tel|fax)\b/i.test(line),
  );
  const addressSource = addressLines.join(" ");
  const postalCodes = [...addressSource.matchAll(/\b\d{3}\s?\d{3}\b|\b\d{5,6}\b/g)].map((match) => match[0].replace(/\D/g, ""));
  const addressDigits = phoneDigits(form.company_address);
  if (postalCodes.some((postal) => !addressDigits.includes(postal))) mismatches.add("Postal code may be missing from the address");
  return [...mismatches];
}

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  if (!headers.has("X-Request-ID")) {
    const requestId = crypto.randomUUID?.() || "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (digit) => {
      const random = Math.random() * 16 | 0;
      return (digit === "x" ? random : (random & 0x3 | 0x8)).toString(16);
    });
    headers.set("X-Request-ID", requestId);
  }
  let response: Response;
  try {
    response = await fetch(`${API}${path}`, { ...init, headers, credentials: "include" });
  } catch (error) {
    const reason = error instanceof Error ? error.message : "network error";
    throw new Error(`Cannot reach the API at ${API}. Check the deployment or connection (${reason}).`);
  }
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new Error(body?.detail || `Request failed (${response.status})`);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

type AuthUser = { id: string; full_name: string; email: string; is_verified: boolean };

function userDisplayName(user?: AuthUser | null): string {
  if (!user) return "Account";
  const trimmed = (user.full_name || "").trim();
  if (trimmed) return trimmed;
  if (user.email && user.email.includes("@")) {
    const username = user.email.split("@")[0].trim();
    if (username) return username;
  }
  return "Account";
}

function LeadApp({ user, onLogout }: { user: AuthUser; onLogout: () => void }) {
  const [leads, setLeads] = useState<Lead[]>([]);
  const [leadTotal, setLeadTotal] = useState(0);
  const [leadPage, setLeadPage] = useState(1);
  const [search, setSearch] = useState("");
  const [companyFilter, setCompanyFilter] = useState("");
  const [ownerFilter, setOwnerFilter] = useState("");
  const [cityFilter, setCityFilter] = useState("");
  const [stateFilter, setStateFilter] = useState("");
  const [exportCount, setExportCount] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [tab, setTab] = useState<"leads" | "form">("leads");
  const [document, setDocument] = useState<DocumentRecord | null>(null);
  const [activeCardIndex, setActiveCardIndex] = useState(0);
  const [pendingDocuments, setPendingDocuments] = useState<DocumentRecord[]>([]);
  const [scanIssues, setScanIssues] = useState<DocumentRecord[]>([]);
  const [reviewBatchIds, setReviewBatchIds] = useState<string[]>([]);
  const [rawCandidates, setRawCandidates] = useState<RawCandidate[]>([]);
  const [rawCandidateIndex, setRawCandidateIndex] = useState(0);
  const [cameraOpen, setCameraOpen] = useState(false);
  const [cameraError, setCameraError] = useState("");
  const [cameraFiles, setCameraFiles] = useState<File[]>([]);
  const [qrCameraOpen, setQrCameraOpen] = useState(false);
  const [qrCameraError, setQrCameraError] = useState("");
  const [localPreviewUrl, setLocalPreviewUrl] = useState("");
  const [localPreviewUrls, setLocalPreviewUrls] = useState<string[]>([]);
  const [ocrFallbackFiles, setOcrFallbackFiles] = useState<File[]>([]);
  const [localConflicts, setLocalConflicts] = useState<string[]>([]);
  const [localOriginalOcrText, setLocalOriginalOcrText] = useState("");
  const [syncCount, setSyncCount] = useState(0);
  const draftLoadedRef = useRef(false);
  const [qrResult, setQrResult] = useState<QRResult | null>(null);
  const [qrChoices, setQrChoices] = useState<string[]>([]);
  const qrChoicesRef = useRef<string[]>([]);
  const [websitePreview, setWebsitePreview] = useState<WebsitePreview | null>(null);
  const [batchAsOneLead, setBatchAsOneLead] = useState(false);
  const [sourceDocumentIds, setSourceDocumentIds] = useState<string[]>([]);
  const [form, setForm] = useState<LeadForm>(emptyForm);
  const [editingLead, setEditingLead] = useState<Lead | null>(null);
  const [duplicateInfo, setDuplicateInfo] = useState<DuplicateCheckResult | null>(null);
  const [viewingLead, setViewingLead] = useState<Lead | null>(null);
  const [exportDialogOpen, setExportDialogOpen] = useState(false);
  const [exportFileNameInput, setExportFileNameInput] = useState(DEFAULT_EXPORT_BASE_NAME);
  const [exportFileNameError, setExportFileNameError] = useState("");
  const [exporting, setExporting] = useState(false);
  const exportInputRef = useRef<HTMLInputElement>(null);
  const companyInputRef = useRef<HTMLInputElement>(null);
  const contactNumbersRef = useRef<HTMLTextAreaElement>(null);
  const [reviewedFingerprint, setReviewedFingerprint] = useState("");

  useEffect(() => {
    if (exportDialogOpen) {
      window.setTimeout(() => {
        exportInputRef.current?.focus();
        exportInputRef.current?.select();
      }, 50);
    }
  }, [exportDialogOpen]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [scanStage, setScanStage] = useState("");
  const [clientScanTiming, setClientScanTiming] = useState<{ documentId: string; prepareMs: number; uploadMs: number; totalMs: number } | null>(null);
  const videoRef = useRef<HTMLVideoElement>(null);
  const cameraStreamRef = useRef<MediaStream | null>(null);
  const qrVideoRef = useRef<HTMLVideoElement>(null);
  const qrStreamRef = useRef<MediaStream | null>(null);
  const scanSubmissionRef = useRef(false);
  const scanStartedAtRef = useRef(0);
  const scanPreparationMsRef = useRef(0);
  const scanUploadMsRef = useRef(0);
  const cameraPreparationMsRef = useRef(0);
  const statusPollingPausedRef = useRef(false);
  const statusFailureStartedRef = useRef(0);
  const statusRetryCountRef = useRef(0);

  const loadLeads = useCallback(async (requestedPage = leadPage) => {
    try {
      const params = new URLSearchParams();
      if (search.trim()) params.set("search", search.trim());
      if (companyFilter.trim()) params.set("company_name", companyFilter.trim());
      if (ownerFilter.trim()) params.set("owner_name", ownerFilter.trim());
      if (cityFilter.trim()) params.set("city", cityFilter.trim());
      if (stateFilter.trim()) params.set("state", stateFilter.trim());
      params.set("page", String(requestedPage));
      params.set("page_size", String(LEADS_PAGE_SIZE));
      const data = await api<LeadPage>(`/leads?${params.toString()}`);
      setLeads(data.leads);
      setLeadTotal(data.total);
      setLeadPage(data.page);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load leads.");
    }
  }, [search, companyFilter, ownerFilter, cityFilter, stateFilter, leadPage]);

  const loadScanIssues = useCallback(async () => {
    try {
      const documents = await api<DocumentRecord[]>("/documents/pending");
      setScanIssues(documents.filter((item) => ["needs_review", "failed"].includes(item.processing_status)));
      setPendingDocuments(documents.filter((item) => ["uploaded", "processing"].includes(item.processing_status)));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load scan status.");
    }
  }, []);

  const duplicateLeadIds = useMemo(() => {
    const phoneMap = new Map<string, string>();
    const emailMap = new Map<string, string>();
    const dupes = new Set<string>();

    for (const item of leads) {
      const phones = item.contact_numbers?.length ? item.contact_numbers : item.contact_number ? [item.contact_number] : [];
      for (const phone of phones) {
        const digits = phone.replace(/\D/g, "");
        const key = digits.length >= 10 ? digits.slice(-10) : digits;
        if (key) {
          if (phoneMap.has(key)) {
            dupes.add(item.id);
            dupes.add(phoneMap.get(key)!);
          } else {
            phoneMap.set(key, item.id);
          }
        }
      }
      const emails = item.email_addresses?.length ? item.email_addresses : item.email ? [item.email] : [];
      for (const em of emails) {
        const key = em.trim().toLowerCase();
        if (key) {
          if (emailMap.has(key)) {
            dupes.add(item.id);
            dupes.add(emailMap.get(key)!);
          } else {
            emailMap.set(key, item.id);
          }
        }
      }
    }
    return dupes;
  }, [leads]);

  useEffect(() => {
    if (tab !== "form") {
      setDuplicateInfo(null);
      return;
    }
    const hasContacts = form.contact_numbers.some((num) => num.trim()) || form.email_addresses.some((em) => em.trim());
    if (!hasContacts) {
      setDuplicateInfo(null);
      return;
    }

    let active = true;
    const timer = window.setTimeout(async () => {
      try {
        const result = await api<DuplicateCheckResult>("/leads/check-duplicate", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            contact_numbers: form.contact_numbers,
            email_addresses: form.email_addresses,
            exclude_lead_id: editingLead?.id || null,
          }),
        });
        if (active) {
          setDuplicateInfo(result.is_duplicate ? result : null);
        }
      } catch {
        // Live duplicate check failed; save action still validates on server
      }
    }, 250);

    return () => {
      active = false;
      window.clearTimeout(timer);
    };
  }, [tab, form.contact_numbers, form.email_addresses, editingLead?.id]);

  useEffect(() => { void loadLeads(); }, [loadLeads]);
  useEffect(() => { void loadScanIssues(); }, [loadScanIssues]);

  useEffect(() => {
    if (!cameraOpen) return;
    let cancelled = false;
    setCameraError("");
    if (!navigator.mediaDevices?.getUserMedia) {
      setCameraError("This browser cannot open the camera here. Use the photo upload option instead.");
      return;
    }
    navigator.mediaDevices?.getUserMedia({
      video: {
        facingMode: { ideal: "environment" },
        width: { ideal: 2560 },
        height: { ideal: 1440 },
        frameRate: { ideal: 30, max: 30 },
      },
      audio: false,
    })
      .then((stream) => {
        if (cancelled) {
          stream.getTracks().forEach((track) => track.stop());
          return;
        }
        cameraStreamRef.current = stream;
        const track = stream.getVideoTracks()[0];
        if (track && "getCapabilities" in track && "applyConstraints" in track) {
          const capabilities = track.getCapabilities() as MediaTrackCapabilities & Record<string, unknown>;
          const continuousSettings: Record<string, string | boolean> = {};
          for (const setting of ["focusMode", "exposureMode", "whiteBalanceMode"]) {
            const values = capabilities[setting];
            if (Array.isArray(values) && values.includes("continuous")) continuousSettings[setting] = "continuous";
          }
          if (capabilities.imageStabilization === true) continuousSettings.imageStabilization = true;
          if (Object.keys(continuousSettings).length) {
            void track.applyConstraints({ advanced: [continuousSettings] as MediaTrackConstraintSet[] }).catch(() => undefined);
          }
        }
        if (videoRef.current) {
          videoRef.current.srcObject = stream;
          void videoRef.current.play();
        }
      })
      .catch(() => setCameraError("Camera access is unavailable. Allow camera permission or choose a photo from your device."));
    return () => {
      cancelled = true;
      cameraStreamRef.current?.getTracks().forEach((track) => track.stop());
      cameraStreamRef.current = null;
    };
  }, [cameraOpen]);

  useEffect(() => {
    if (!qrCameraOpen) return;
    let cancelled = false;
    let stopScanning: (() => void) | undefined;
    setQrCameraError("");
    const BarcodeDetectorCtor = (window as unknown as { BarcodeDetector?: new (options?: { formats?: string[] }) => { detect: (source: HTMLVideoElement) => Promise<Array<{ rawValue?: string }>> } }).BarcodeDetector;
    if (!navigator.mediaDevices?.getUserMedia) {
      setQrCameraError("Camera access is unavailable. Allow camera permission or upload the QR image instead.");
      return;
    }
    let detector: InstanceType<NonNullable<typeof BarcodeDetectorCtor>> | null = null;
    try { if (BarcodeDetectorCtor) detector = new BarcodeDetectorCtor({ formats: ["qr_code"] }); } catch { /* Local decoder remains available. */ }
    navigator.mediaDevices.getUserMedia({ video: { facingMode: { ideal: "environment" }, width: { ideal: 1920 }, height: { ideal: 1080 } }, audio: false })
      .then((stream) => {
        if (cancelled) { stream.getTracks().forEach((track) => track.stop()); return; }
        qrStreamRef.current = stream;
        if (qrVideoRef.current) { qrVideoRef.current.srcObject = stream; void qrVideoRef.current.play(); }
        stopScanning = startQrScanLoop({
          ready: () => Boolean(!cancelled && qrVideoRef.current && qrVideoRef.current.readyState >= 2 && qrVideoRef.current.videoWidth),
          nativeScan: detector ? async () => {
            const values = (await detector!.detect(qrVideoRef.current!)).map(item => item.rawValue || "").filter(Boolean);
            return values.length === 1 ? [...new Set([...values, ...decodeQrFromVideo(qrVideoRef.current!)])] : values;
          } : undefined,
          fallbackScan: () => decodeQrFromVideo(qrVideoRef.current!),
          onResults: (values) => {
            stream.getTracks().forEach(track => track.stop());
            setQrCameraOpen(false);
            presentQrChoices(values);
            if (values.length === 1) acceptQrPayload(values[0]);
            else setNotice("Several QR codes found. Choose the company or contact code below.");
          },
          onResult: (value) => {
              stream.getTracks().forEach((track) => track.stop());
              setQrCameraOpen(false);
              presentQrChoices([value]); acceptQrPayload(value);
          },
        });
      })
      .catch(() => setQrCameraError("Camera access is unavailable. Allow camera permission or upload the QR/card image."));
    return () => {
      cancelled = true;
      stopScanning?.();
      qrStreamRef.current?.getTracks().forEach((track) => track.stop());
      qrStreamRef.current = null;
    };
  }, [qrCameraOpen]);

  async function previewQrWebsite() {
    const url = safeText(qrResult?.website);
    if (!url) return;
    setBusy(true);
    try {
      const result = await api<WebsitePreview>("/qr/website-preview", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ url }) });
      setWebsitePreview(result);
      setForm((current) => ({
        ...current,
        company_name: current.company_name || safeText(result.fields.company_name),
        owner_name: current.owner_name || safeText(result.fields.owner_name),
        designation: current.designation || safeText(result.fields.designation),
        company_address: current.company_address || safeText(result.fields.company_address),
        contact_numbers: stringValues([...current.contact_numbers, ...stringValues(result.fields.contact_numbers as string[])]),
        email_addresses: stringValues([...current.email_addresses, ...stringValues(result.fields.email_addresses as string[])]),
        business_description: current.business_description || safeText(result.fields.business_description),
        facebook_link: current.facebook_link || safeText(result.fields.facebook_link),
        instagram_link: current.instagram_link || safeText(result.fields.instagram_link),
        whatsapp_link: current.whatsapp_link || safeText(result.fields.whatsapp_link),
      }));
      setNotice(["Website suggestions require review; existing card values were preserved.", ...result.warnings].join(" "));
    } catch (err) { setError(err instanceof Error ? err.message : "Could not read the public website."); }
    finally { setBusy(false); }
  }

  async function uploadQrImage(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;
    setQrCameraOpen(false);
    setError("");
    try {
      const values = await scanQrImage(file);
      if (!values.length) throw new Error("No readable QR found. Try a sharper, closer photo with the full code visible.");
      presentQrChoices(values);
      if (values.length === 1) acceptQrPayload(values[0]);
      else setNotice("Several QR codes found. Choose one below.");
    } catch (err) { setError(err instanceof Error ? err.message : "Could not decode QR image."); }
  }

  function presentQrChoices(payloads: string[]) {
    const choices = [...new Set([...qrChoicesRef.current, ...payloads])];
    qrChoicesRef.current = choices;
    setQrChoices(choices);
    setWebsitePreview(null);
    if (choices.length === 1) setQrResult(parseQrLocally(choices[0]) as QRResult);
    else setQrResult(null);
  }

  function acceptQrPayload(value: string) {
    const parsed = parseQrLocally(value);
    setQrResult(parsed as QRResult);
    setWebsitePreview(null);
    if (parsed.review_warnings?.length) setLocalConflicts(items => [...new Set([...items, ...parsed.review_warnings!])]);
    const next = { ...form };
    const conflicts: string[] = [];
    for (const key of ["company_name", "owner_name", "designation", "company_address", "website", "facebook_link", "instagram_link", "whatsapp_link"] as const) {
      const proposed = parsed[key];
      if (!proposed) continue;
      if (form[key] && form[key] !== proposed) conflicts.push(`${key.replace(/_/g, " ")}: card says ${form[key]}; QR says ${proposed}`);
      else next[key] = proposed;
    }
    next.contact_numbers = stringValues([...form.contact_numbers, ...parsed.contact_numbers]);
    next.email_addresses = stringValues([...form.email_addresses, ...parsed.email_addresses]);
    next.raw_text = form.raw_text.includes(parsed.source_text) ? form.raw_text : [form.raw_text, parsed.source_text].filter(Boolean).join("\n");
    if (conflicts.length) setLocalConflicts(items => [...new Set([...items, ...conflicts])]);
    setForm(next);
    setTab("form");
    setNotice("QR data is ready. Review the values before saving.");
  }

  useEffect(() => {
    if (statusPollingPausedRef.current) return;
    if (!pendingDocuments.some((item) => ["uploaded", "processing"].includes(item.processing_status))) return;
    setScanStage(pendingDocuments.some((item) => item.processing_status === "processing")
      ? "Reading card and extracting details"
      : "Waiting for the scanner");
    const retryDelay = statusRetryCountRef.current ? Math.min(8000, 1000 * (2 ** Math.min(statusRetryCountRef.current - 1, 2))) : 350;
    const timer = window.setTimeout(async () => {
      try {
        const statuses = await api<DocumentStatus[]>("/documents/status", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ids: pendingDocuments.map((item) => item.id) }),
        });
        const statusById = new Map(statuses.map((item) => [item.id, item]));
        statusRetryCountRef.current = 0;
        statusFailureStartedRef.current = 0;
        const updated = pendingDocuments.map((item) => ({ ...item, ...(statusById.get(item.id) || {
          processing_status: "failed", error_message: "Document status was not returned. Retry the scan.",
        }) }));
        setDocument((current) => {
          if (!current) return current;
          const status = statusById.get(current.id);
          return status ? { ...current, ...status } : current;
        });
        const stillProcessing = updated.filter((item) => ["uploaded", "processing"].includes(item.processing_status));
        if (stillProcessing.length) {
          setPendingDocuments(stillProcessing);
        } else {
          setPendingDocuments([]);
          setBusy(false);
          setScanStage("");
          scanSubmissionRef.current = false;
          if (scanStartedAtRef.current) {
            const timingDocumentId = document?.id || pendingDocuments[0]?.id;
            if (timingDocumentId) setClientScanTiming({ documentId: timingDocumentId, prepareMs: scanPreparationMsRef.current, uploadMs: scanUploadMsRef.current, totalMs: Math.round(performance.now() - scanStartedAtRef.current) });
            scanStartedAtRef.current = 0;
          }
          const completed = updated.find((item) => (!reviewBatchIds.length || reviewBatchIds.includes(item.id)) && (item.processing_status === "needs_review" || item.processing_status === "failed"));
          if (completed && !rawCandidates.length && (!document || document.id === completed.id)) {
            try {
              const batchDocuments = batchAsOneLead && reviewBatchIds.length > 1
                ? await Promise.all(reviewBatchIds.map((id) => api<DocumentRecord>(`/documents/${id}`)))
                : [await api<DocumentRecord>(`/documents/${completed.id}`)];
              const fullDocument = batchDocuments.length > 1
                ? { ...batchDocuments[0], original_filename: batchDocuments.map((item) => item.original_filename).join(" + "), extracted_fields: mergeExtractedFields(batchDocuments) }
                : batchDocuments[0];
              setDocument(fullDocument);
              setForm(formFromExtracted(fullDocument.extracted_fields || {}));
              presentQrChoices(fullDocument.extracted_fields.qr_payloads || []);
              setTab("form");
              setNotice(fullDocument.processing_status === "needs_review"
                ? "OCR finished. Check the source and lead details, then choose Save lead."
                : "Scanning finished. Review the source and enter the lead details before saving.");
            } catch (err) {
              setError(err instanceof Error ? err.message : "Could not load OCR results.");
            }
          }
          await loadScanIssues();
        }
      } catch (err) {
        statusRetryCountRef.current += 1;
        if (!statusFailureStartedRef.current) statusFailureStartedRef.current = performance.now();
        const timedOut = performance.now() - statusFailureStartedRef.current > 120000;
        if (timedOut) {
          statusPollingPausedRef.current = true;
          setBusy(false);
          setScanStage("OCR status is unavailable");
          setError("The OCR server could not be reached for two minutes. Your uploaded source is still available; retry when the server is ready.");
        } else {
          setError(err instanceof Error ? `${err.message}. Retrying shortly.` : "Could not check document status. Retrying shortly.");
          setPendingDocuments((current) => [...current]);
        }
      }
    }, retryDelay);
    return () => window.clearTimeout(timer);
  }, [pendingDocuments, loadScanIssues, rawCandidates.length, document, reviewBatchIds, batchAsOneLead]);

  const title = editingLead ? "Edit lead" : "Add lead";
  const formFingerprint = JSON.stringify(form);
  const mismatches = reviewMismatches(form);
  const hasOcrText = Boolean(textValue(form.raw_text).trim());
  const needsSourceReview = Boolean(hasOcrText || document || editingLead?.document_id);
  const sourceReviewConfirmed = reviewedFingerprint === formFingerprint;
  const canBulkGenerate = rawCandidates.some((candidate) => /\.xlsx$/i.test(candidate.source));
  const selectableScanIssues = reviewBatchIds.length
    ? scanIssues.filter((item) => reviewBatchIds.includes(item.id))
    : scanIssues;

  const flushQueuedLeads = useCallback(async () => {
    if (!navigator.onLine) return;
    const queued = await readLeadQueue(user.id).catch(() => []);
    setSyncCount(queued.length);
    for (const item of queued) {
      try {
        await api<Lead>("/leads", { method: "POST", headers: { "Content-Type": "application/json", "X-Idempotency-Key": item.idempotencyKey }, body: JSON.stringify(item.payload) });
        await removeQueuedLead(item.id);
      } catch {
        break;
      }
    }
    setSyncCount((await readLeadQueue(user.id).catch(() => [])).length);
    if (queued.length) await loadLeads(1).catch(() => undefined);
  }, [user.id, loadLeads]);

  useEffect(() => {
    void readLeadQueue(user.id).then((items) => setSyncCount(items.length)).catch(() => undefined);
    const onOnline = () => { void flushQueuedLeads(); };
    window.addEventListener("online", onOnline);
    return () => window.removeEventListener("online", onOnline);
  }, [user.id, flushQueuedLeads]);

  useEffect(() => {
    if (draftLoadedRef.current || document || editingLead || rawCandidates.length || tab !== "leads") return;
    draftLoadedRef.current = true;
    void readLeadDraft(user.id).then((draft) => {
      if (!draft?.fields) return;
      const restored = draft.fields as Partial<LeadForm>;
      if (!Object.values(restored).some((value) => Array.isArray(value) ? value.length : String(value || "").trim())) return;
      setForm((current) => ({ ...current, ...restored, contact_numbers: Array.isArray(restored.contact_numbers) ? restored.contact_numbers.map(String) : current.contact_numbers, email_addresses: Array.isArray(restored.email_addresses) ? restored.email_addresses.map(String) : current.email_addresses }));
      setTab("form");
      setNotice("An unfinished local draft was restored. Nothing has been saved to the server.");
    }).catch(() => undefined);
  }, [user.id, document, editingLead, rawCandidates.length, tab]);

  useEffect(() => {
    if (tab !== "form" || editingLead || document || rawCandidates.length) return;
    const timer = window.setTimeout(() => { void saveLeadDraft(user.id, form as unknown as Record<string, unknown>).catch(() => undefined); }, 250);
    return () => window.clearTimeout(timer);
  }, [user.id, tab, editingLead, document, rawCandidates.length, form]);

  async function upload(event: ChangeEvent<HTMLInputElement>) {
    const files = Array.from(event.target.files || []);
    event.target.value = "";
    // Use the reliable server OCR path for normal uploads. The browser WASM
    // worker is optional and must not block a production scan.
    if (files.length) await uploadFiles(files, false, false, true);
  }

  async function uploadFiles(files: File[], asOneLead = false, cameraFilesAlreadyOptimized = false, forceServerOcr = false) {
    if (!files.length) return;
    if (scanSubmissionRef.current) return;
    setError("");
    setNotice("");
    setBusy(true);
    statusPollingPausedRef.current = false;
    statusFailureStartedRef.current = 0;
    statusRetryCountRef.current = 0;
    setEditingLead(null);
    setReviewedFingerprint("");
    setForm(emptyForm);
    setLocalOriginalOcrText("");
    setOcrFallbackFiles([]);
    setLocalConflicts([]);
    setLocalPreviewUrl((current) => { if (current) URL.revokeObjectURL(current); return ""; });
    setLocalPreviewUrls((current) => { current.forEach((url) => URL.revokeObjectURL(url)); return []; });
    setRawCandidates([]);
    setReviewBatchIds([]);
    setSourceDocumentIds([]);
    setBatchAsOneLead(asOneLead);
    qrChoicesRef.current = [];
    setQrChoices([]);
    setQrResult(null);
    const rawFiles = files.filter((file) => /\.(txt|csv|xlsx)$/i.test(file.name));
    if (rawFiles.length) {
      setSearch("");
      setCompanyFilter("");
      setOwnerFilter("");
      setCityFilter("");
      setStateFilter("");
      setSelected(new Set());
      setLeadPage(1);
    }
    const documentFiles = files.filter((file) => /\.(jpe?g|png|webp|bmp|tiff?|gif|pdf|zip)$/i.test(file.name));
    const unsupportedCount = files.length - rawFiles.length - documentFiles.length;
    try {
      if (rawFiles.length) {
        const candidates: RawCandidate[] = [];
        for (const file of rawFiles) {
          const data = new FormData();
          data.append("file", file);
          const result = await api<RawPreviewResult>("/imports/leads/preview", { method: "POST", body: data });
          candidates.push(...result.candidates.map((candidate) => ({ source: result.filename, ...candidate })));
        }
        if (candidates.length) {
          setRawCandidates(candidates);
          setRawCandidateIndex(0);
          setDocument(null);
          setForm(formFromExtracted(candidates[0].fields));
          setTab("form");
          setNotice(`${candidates.length} raw-data row${candidates.length === 1 ? "" : "s"} ready. Review and save each row to add it to your leads.`);
        } else {
          throw new Error("No non-empty lead rows were found in the selected raw-data files.");
        }
      }
      if (!documentFiles.length && !rawFiles.length) throw new Error("Choose card images, PDF, ZIP, TXT, CSV, or XLSX files.");
      const imageFiles = documentFiles.filter((file) => /^(?:image\/(?:jpeg|png|webp|bmp))$/i.test(file.type) || /\.(jpe?g|png|webp|bmp)$/i.test(file.name));
      const otherDocuments = documentFiles.filter((file) => !imageFiles.includes(file));
      // Surface QR choices immediately, then keep reading the printed card.
      // The dedicated QR-image action skips text OCR when that is intended.
      if (!rawFiles.length && imageFiles.length && otherDocuments.length === 0) {
        const qrPayloads = [...new Set((await Promise.all(imageFiles.map((file) => scanQrImage(file).catch(() => [])))).flat())];
        if (qrPayloads.length) {
          presentQrChoices(qrPayloads);
        }
      }
      if (!forceServerOcr && !rawFiles.length && imageFiles.length > 0 && imageFiles.length <= 4 && otherDocuments.length === 0) {
        try {
          setScanStage("Preparing card image for local OCR");
          const localImageFiles = await Promise.all(imageFiles.map(async (file) => {
            try {
              const optimized = await optimizeCameraPhoto(file);
              return optimized === file ? file : new File([optimized], file.name.replace(/\.[^.]+$/, ".jpg"), { type: optimized.type || "image/jpeg", lastModified: file.lastModified });
            } catch {
              return file;
            }
          }));
          const localRecords: ExtractedLeadFields[] = [];
          for (let index = 0; index < localImageFiles.length; index += 1) {
            setScanStage(localImageFiles.length > 1 ? `Reading card image ${index + 1} of ${localImageFiles.length} on this device` : "Reading card on this device");
            localRecords.push(await recognizeCard(localImageFiles[index], (message) => setScanStage(message)));
          }
          const { fields: local, conflicts } = mergeLocalCards(localRecords);
          const hasUsefulValue = Boolean(local.company_name || local.contact_numbers?.length || local.email_addresses?.length || local.website);
          if (hasUsefulValue) {
            setForm(formFromExtracted(local));
            setLocalOriginalOcrText(local.original_ocr_text || "");
            const previewUrls = imageFiles.map((file) => URL.createObjectURL(file));
            setLocalPreviewUrls(previewUrls);
            setLocalPreviewUrl(previewUrls[0] || "");
            setLocalConflicts(conflicts);
            setTab("form");
            setNotice(`${imageFiles.length > 1 ? `${imageFiles.length} card sides` : "Card"} read on this device. Review the fields; nothing has been uploaded or saved yet.`);
            setBusy(false);
            scanSubmissionRef.current = false;
            scanStartedAtRef.current = 0;
            setScanStage("");
            return;
          }
          setOcrFallbackFiles(imageFiles);
          setBusy(false);
          scanSubmissionRef.current = false;
          scanStartedAtRef.current = 0;
          setScanStage("");
          setNotice("Browser OCR found no reliable lead fields.");
          setError("The image stayed on this device. Choose Retry locally or Use server OCR to continue.");
          return;
        } catch (error) {
          setOcrFallbackFiles(imageFiles);
          setBusy(false);
          scanSubmissionRef.current = false;
          scanStartedAtRef.current = 0;
          setScanStage("");
          setNotice(error instanceof Error ? error.message : "Browser OCR could not read this card.");
          setError("The image stayed on this device. Choose Retry locally or Use server OCR to continue.");
          return;
        }
      }
      let preparedFiles = documentFiles;
      if (documentFiles.length) {
        scanSubmissionRef.current = true;
        scanStartedAtRef.current = performance.now();
        scanPreparationMsRef.current = cameraFilesAlreadyOptimized ? cameraPreparationMsRef.current : 0;
        cameraPreparationMsRef.current = 0;
        scanUploadMsRef.current = 0;
        setClientScanTiming(null);
        setScanStage("Preparing image");
        const preparationStartedAt = performance.now();
        const preparedImages = cameraFilesAlreadyOptimized ? imageFiles : await Promise.all(imageFiles.map(async (file) => {
          try {
            const optimized = await optimizeCameraPhoto(file);
            return optimized === file ? file : new File([optimized], file.name.replace(/\.[^.]+$/, ".jpg"), { type: optimized.type || "image/jpeg", lastModified: file.lastModified });
          } catch {
            // An unsupported decoder must not prevent the original image from
            // being scanned by the backend.
            return file;
          }
        }));
        scanPreparationMsRef.current += Math.round(performance.now() - preparationStartedAt);
        preparedFiles = [...preparedImages, ...otherDocuments];
      }
      const uploadedDocuments: DocumentRecord[] = [];
      const skippedFiles: string[] = [];
      if (documentFiles.length) setScanStage("Uploading image");
      const uploadStartedAt = performance.now();
      for (let start = 0; start < preparedFiles.length; start += 20) {
        const data = new FormData();
        preparedFiles.slice(start, start + 20).forEach((file) => data.append("files", file));
        const result = await api<DocumentBatchResult>("/documents/batch", { method: "POST", body: data });
        uploadedDocuments.push(...result.documents);
        skippedFiles.push(...result.skipped_files.map((item) => `${item.filename}: ${item.reason}`));
      }
      scanUploadMsRef.current = Math.round(performance.now() - uploadStartedAt);
      if (!rawFiles.length) setDocument(uploadedDocuments[0] || null);
      setActiveCardIndex(0);
      setPendingDocuments(uploadedDocuments);
      setScanIssues((current) => mergeDocuments(current, uploadedDocuments));
      setReviewBatchIds(uploadedDocuments.map((item) => item.id));
      setSourceDocumentIds(uploadedDocuments.map((item) => item.id));
      if (!rawFiles.length && uploadedDocuments.length) {
        setForm(formFromExtracted(uploadedDocuments[0].extracted_fields || {}));
        setTab("form");
      }
      if (uploadedDocuments.length) setNotice(`${uploadedDocuments.length} document${uploadedDocuments.length === 1 ? "" : "s"} queued. OCR results will open for review and are saved only when you confirm each lead.`);
      if (!uploadedDocuments.length) {
        setBusy(false);
        scanSubmissionRef.current = false;
        scanStartedAtRef.current = 0;
        setScanStage("");
      }
      if (unsupportedCount || skippedFiles.length) setError([...skippedFiles, ...(unsupportedCount ? [`${unsupportedCount} unsupported file${unsupportedCount === 1 ? " was" : "s were"} skipped`] : [])].join("\n"));
    } catch (err) {
      setBusy(false);
      setScanStage("");
      scanSubmissionRef.current = false;
      scanStartedAtRef.current = 0;
      setError(err instanceof Error ? err.message : "Upload failed.");
    }
  }

  async function generateAllExcelLeads() {
    const remaining = rawCandidates.slice(rawCandidateIndex);
    const candidates = remaining.map((candidate, offset) => {
      const source = offset === 0 ? form : candidate.fields;
      const contactNumber = "contact_number" in source ? source.contact_number : null;
      const email = "email" in source ? source.email : null;
      return {
        company_name: textValue(source.company_name).trim(),
        owner_name: textValue(source.owner_name).trim() || null,
        designation: textValue(source.designation).trim() || null,
        company_address: textValue(source.company_address).trim() || null,
        contact_numbers: stringValues(source.contact_numbers, contactNumber),
        email_addresses: stringValues(source.email_addresses, email),
        website: textValue(source.website).trim() || null,
        facebook_link: textValue(source.facebook_link).trim() || null,
        instagram_link: textValue(source.instagram_link).trim() || null,
        whatsapp_link: textValue(source.whatsapp_link).trim() || null,
        raw_text: textValue(source.raw_text).trim() || null,
      };
    });
    const validCandidates = candidates.filter((candidate) => candidate.company_name);
    const skipped = candidates.length - validCandidates.length;
    if (!validCandidates.length) {
      setError("No remaining Excel rows have a company name to generate.");
      return;
    }
    setBusy(true);
    setError("");
    setNotice("");
    try {
      // Keep mobile request bodies small and let the API commit bounded ORM
      // chunks. This avoids one huge JSON payload for large spreadsheet imports.
      let created = 0;
      let skippedDuplicates = 0;
      for (let start = 0; start < validCandidates.length; start += 1000) {
        const result = await api<{ created: number; skipped_duplicates?: number }>("/leads/batch", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(validCandidates.slice(start, start + 1000)),
        });
        created += result.created;
        skippedDuplicates += result.skipped_duplicates || 0;
      }
      setLeadPage(1);
      await loadLeads(1);
      setRawCandidates([]);
      setRawCandidateIndex(0);
      setForm(emptyForm);
      setTab("leads");
      const blankMsg = skipped ? `${skipped} row${skipped === 1 ? " was" : "s were"} skipped because the company name was blank. ` : "";
      const dupMsg = skippedDuplicates ? `${skippedDuplicates} duplicate row${skippedDuplicates === 1 ? " was" : "s were"} skipped to prevent duplicate data. ` : "";
      setNotice(`${created} lead${created === 1 ? "" : "s"} generated. ${blankMsg}${dupMsg}Generated records are marked Check data until reviewed.`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not generate leads from the Excel file.");
    } finally {
      setBusy(false);
    }
  }

  async function capturePhoto() {
    const video = videoRef.current;
    if (!video?.videoWidth || !video.videoHeight) {
      setCameraError("The camera is still starting. Wait a moment and try again.");
      return;
    }
    let blob: Blob | null = null;
    const captureStartedAt = performance.now();
    try {
      const track = cameraStreamRef.current?.getVideoTracks()[0];
      const ImageCaptureApi = window.ImageCapture;
      if (track && ImageCaptureApi) {
        try {
          blob = await new ImageCaptureApi(track).takePhoto();
        } catch {
          // Fall back to the full-resolution live frame on browsers that expose
          // ImageCapture but cannot take a still photo from this camera.
        }
      }
      if (!blob) {
        const canvas = window.document.createElement("canvas");
        canvas.width = video.videoWidth;
        canvas.height = video.videoHeight;
        const context = canvas.getContext("2d");
        if (!context) throw new Error("Canvas is unavailable");
        context.drawImage(video, 0, 0, canvas.width, canvas.height);
        blob = await new Promise<Blob | null>((resolve) => canvas.toBlob(resolve, "image/jpeg", 0.98));
      }
      if (blob) blob = await optimizeCameraPhoto(blob);
    } catch {
      setCameraError("Could not capture the photo. Keep the card steady and try again.");
      return;
    }
    if (!blob) {
      setCameraError("Could not capture the photo. Try again or upload an image.");
      return;
    }
    cameraPreparationMsRef.current += Math.round(performance.now() - captureStartedAt);
    setCameraFiles((current) => [...current, new File([blob], `camera-${Date.now()}.jpg`, { type: "image/jpeg" })]);
    setCameraError("");
  }

  async function finishCameraScan() {
    if (!cameraFiles.length) {
      setCameraError("Capture at least one image first.");
      return;
    }
    setCameraOpen(false);
    const files = cameraFiles;
    setCameraFiles([]);
    await uploadFiles(files, files.length > 1, true, true);
  }

  async function retryDocument(source: DocumentRecord) {
    if (scanSubmissionRef.current) return;
    scanSubmissionRef.current = true;
    scanStartedAtRef.current = performance.now();
    scanPreparationMsRef.current = 0;
    scanUploadMsRef.current = 0;
    setClientScanTiming(null);
    setScanStage("Retrying scan");
    try {
      setError("");
      const retried = await api<DocumentRecord>(`/documents/${source.id}/retry`, { method: "POST" });
      setScanIssues((current) => current.filter((item) => item.id !== source.id));
      setDocument(retried);
      setActiveCardIndex(0);
      setForm(emptyForm);
      setTab("form");
      setPendingDocuments([retried]);
      setBusy(true);
      setNotice(`Retrying scan for ${source.original_filename}.`);
    } catch (err) {
      scanSubmissionRef.current = false;
      scanStartedAtRef.current = 0;
      setScanStage("");
      setError(err instanceof Error ? err.message : "Could not retry this scan.");
    }
  }

  async function reviewDocument(source: DocumentRecord) {
    try {
      setError("");
      const fullDocument = await api<DocumentRecord>(`/documents/${source.id}`);
      setDocument(fullDocument);
      setActiveCardIndex(0);
      setRawCandidates([]);
      setEditingLead(null);
      setForm(formFromExtracted(fullDocument.extracted_fields || {}));
      presentQrChoices(fullDocument.extracted_fields.qr_payloads || []);
      setReviewedFingerprint("");
      setTab("form");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load OCR results.");
    }
  }

  function startManualEntry() {
    setDocument(null);
    setRawCandidates([]);
    setRawCandidateIndex(0);
    setReviewBatchIds([]);
    setEditingLead(null);
    setForm(emptyForm);
    setBusy(false);
    setReviewedFingerprint("");
    setError("");
    setNotice("");
    setTab("form");
  }

  async function editLead(lead: Lead) {
    setDocument(null);
    setRawCandidates([]);
    setRawCandidateIndex(0);
    setReviewBatchIds([]);
    setEditingLead(lead);
    setBusy(Boolean(lead.document_id));
    setForm({
      company_name: lead.company_name,
      owner_name: lead.owner_name || "",
      designation: lead.designation || "",
      business_description: lead.business_description || "",
      company_address: lead.company_address || "",
      contact_numbers: lead.contact_numbers?.length ? lead.contact_numbers : lead.contact_number ? [lead.contact_number] : [],
      email_addresses: lead.email_addresses?.length ? lead.email_addresses : lead.email ? [lead.email] : [],
      website: lead.website || "",
      facebook_link: lead.facebook_link || "",
      instagram_link: lead.instagram_link || "",
      whatsapp_link: lead.whatsapp_link || "",
      raw_text: lead.raw_text || "",
    });
    setReviewedFingerprint("");
    setError("");
    setTab("form");
    if (lead.document_id) {
      try {
        const source = await api<DocumentRecord>(`/documents/${lead.document_id}`);
        setDocument(source);
        const extracted = source.extracted_fields;
        setForm((current) => ({
          ...current,
          company_address: current.company_address.trim() || combineAddress(extracted.company_address, extracted.city, extracted.district, extracted.state),
          designation: current.designation || extracted.designation || "",
          business_description: current.business_description || extracted.business_description || "",
          contact_numbers: mergeValues(current.contact_numbers, extracted.contact_numbers),
          email_addresses: mergeValues(current.email_addresses, extracted.email_addresses),
          raw_text: current.raw_text || extracted.raw_text || "",
        }));
        setBusy(false);
      } catch (err) {
        setError(err instanceof Error ? err.message : "Could not load source document.");
        setBusy(false);
      }
    }
  }

  async function saveLead(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError("");
    setNotice("");

    // Validate required fields: company_name and at least one contact_number
    const validation = validateLead({ company_name: form.company_name, contact_numbers: form.contact_numbers });
    if (!validation.valid) {
      setError(validation.error || "Company name and at least one contact number are required.");
      if (validation.firstMissingField === "company_name") {
        companyInputRef.current?.focus();
      } else {
        contactNumbersRef.current?.focus();
      }
      return;
    }
    const trimmedCompany = validation.trimmedCompanyName;
    const cleanedContactNumbers = validation.cleanedContactNumbers;

    if (duplicateInfo?.is_duplicate) {
      setError(`Cannot save duplicate lead: A lead with this ${duplicateInfo.matched_type} (${duplicateInfo.matched_value}) already exists as "${duplicateInfo.existing_company_name}" (${duplicateInfo.existing_lead_number}).`);
      return;
    }
    if (needsSourceReview && !sourceReviewConfirmed) {
      setError("Compare the lead details with the source, correct any mismatches, and confirm the check before saving.");
      return;
    }
    setBusy(true);
    const payload = {
      ...form,
      company_name: trimmedCompany,
      owner_name: form.owner_name || null,
      designation: form.designation || null,
      business_description: form.business_description || null,
      company_address: form.company_address || null,
      contact_numbers: cleanedContactNumbers,
      contact_number: cleanedContactNumbers[0] || null,
      email_addresses: form.email_addresses,
      email: form.email_addresses[0] || null,
      raw_text: form.raw_text || null,
      website: form.website || null,
      facebook_link: form.facebook_link || null,
      instagram_link: form.instagram_link || null,
      whatsapp_link: form.whatsapp_link || null,
      document_id: document?.id || null,
      source_document_ids: sourceDocumentIds,
      source_sides: sourceDocumentIds.map((_, index) => index === 0 ? "front" : "back"),
      document_card_index: document?.extracted_fields.cards && document.extracted_fields.cards.length > 1 ? activeCardIndex : null,
      original_ocr_text: editingLead?.original_ocr_text || localOriginalOcrText || textValue(document?.extracted_fields.cards?.[activeCardIndex]?.original_ocr_text || document?.extracted_fields.original_ocr_text) || null,
      source_review_confirmed: !needsSourceReview || sourceReviewConfirmed,
    };
    const idempotencyKey = crypto.randomUUID?.() || `${Date.now()}-${Math.random().toString(36).slice(2)}`;
    try {
      if (editingLead) {
        await api<Lead>(`/leads/${editingLead.id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
        setNotice("Lead updated.");
      } else {
        await api<Lead>("/leads", { method: "POST", headers: { "Content-Type": "application/json", "X-Idempotency-Key": idempotencyKey }, body: JSON.stringify(payload) });
        await clearLeadDraft(user.id).catch(() => undefined);
        setNotice("Lead saved.");
      }
      if (!editingLead) {
        setLeadPage(1);
        await loadLeads(1);
      } else {
        await loadLeads();
      }
      if (!editingLead && rawCandidates.length && rawCandidateIndex + 1 < rawCandidates.length) {
        const nextIndex = rawCandidateIndex + 1;
        setRawCandidateIndex(nextIndex);
        setForm(formFromExtracted(rawCandidates[nextIndex].fields));
        setReviewedFingerprint("");
        setTab("form");
        setNotice(`Row ${rawCandidates[rawCandidateIndex].row} saved. Review row ${rawCandidates[nextIndex].row} and save it when ready.`);
      } else if (!editingLead && document && (document.extracted_fields.cards?.length || 1) > activeCardIndex + 1) {
        const nextIndex = activeCardIndex + 1;
        setActiveCardIndex(nextIndex);
        setForm(formFromExtracted(document.extracted_fields, nextIndex));
        setReviewedFingerprint("");
        setNotice(`Card ${activeCardIndex + 1} saved. Review card ${nextIndex + 1} of ${document.extracted_fields.cards?.length} next.`);
        await loadScanIssues();
      } else {
        setDocument(null);
        setLocalOriginalOcrText("");
        setLocalPreviewUrl((current) => { if (current) URL.revokeObjectURL(current); return ""; });
        setLocalPreviewUrls((current) => { current.forEach((url) => URL.revokeObjectURL(url)); return []; });
        setEditingLead(null);
        setDuplicateInfo(null);
        setRawCandidates([]);
        setRawCandidateIndex(0);
        setForm(emptyForm);
        setTab("leads");
        if (document) {
          const waiting = await api<DocumentRecord[]>("/documents/pending");
          const queued = waiting.filter((item) => ["needs_review", "failed"].includes(item.processing_status));
          setScanIssues(queued);
          const nextCandidates = reviewBatchIds.length ? queued.filter((item) => reviewBatchIds.includes(item.id)) : queued;
          const next = nextCandidates.find((item) => item.processing_status === "needs_review" && item.id !== document.id);
          if (next) {
            setDocument(next);
            setActiveCardIndex(0);
            setForm(formFromExtracted(next.extracted_fields || {}));
            setReviewedFingerprint("");
            setTab("form");
            setNotice("Lead saved. The next document is ready for review.");
          } else if (reviewBatchIds.length && !waiting.some((item) => reviewBatchIds.includes(item.id) && ["uploaded", "processing"].includes(item.processing_status))) {
            setReviewBatchIds([]);
          }
        } else {
          await loadScanIssues();
        }
      }
    } catch (err) {
      if (!navigator.onLine || (err instanceof TypeError && /fetch/i.test(err.message))) {
        await enqueueLead(user.id, payload, idempotencyKey).catch(() => undefined);
        setSyncCount((count) => count + 1);
        setNotice("Lead saved on this device and queued for synchronization when the connection returns.");
      } else {
        setError(err instanceof Error ? err.message : "Could not save lead.");
      }
    } finally {
      setBusy(false);
    }
  }

  async function removeLead(lead: Lead) {
    if (!window.confirm(`Delete ${lead.company_name}?`)) return;
    try {
      await api<void>(`/leads/${lead.id}`, { method: "DELETE" });
      setSelected((current) => { const next = new Set(current); next.delete(lead.id); return next; });
      if (leads.length === 1 && leadPage > 1) {
        await loadLeads(leadPage - 1);
      } else {
        await loadLeads();
      }
      setNotice("Lead deleted.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not delete lead.");
    }
  }

  function openExportDialog() {
    setError("");
    if (exportCount.trim() && (!Number.isInteger(Number(exportCount)) || Number(exportCount) < 1)) {
      setError("Enter a whole number above zero, or clear Rows to export to export all matching leads.");
      return;
    }
    setExportFileNameInput(DEFAULT_EXPORT_BASE_NAME);
    setExportFileNameError("");
    setExportDialogOpen(true);
  }

  function handleCancelExport() {
    if (exporting) return;
    setExportDialogOpen(false);
    setExportFileNameError("");
  }

  async function handleConfirmExport() {
    if (exporting) return;
    const effectiveInput = exportFileNameInput.trim() || DEFAULT_EXPORT_BASE_NAME;
    const validation = sanitizeExportFileName(effectiveInput);
    if (!validation.valid) {
      setExportFileNameError(validation.error || "Enter a valid file name.");
      return;
    }
    setExporting(true);
    setExportFileNameError("");
    setError("");
    try {
      await performExport(validation.sanitizedBaseName, validation.finalFileName);
      setExportDialogOpen(false);
      setExportFileNameError("");
      setError("");
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Export failed.";
      setExportFileNameError(msg);
      setError(msg);
    } finally {
      setExporting(false);
    }
  }

  async function performExport(sanitizedBaseName: string, finalFileName: string) {
    if (exportCount.trim() && (!Number.isInteger(Number(exportCount)) || Number(exportCount) < 1)) {
      throw new Error("Enter a whole number above zero, or clear Rows to export to export all matching leads.");
    }
    const params = new URLSearchParams();
    if (selected.size) selected.forEach((id) => params.append("ids", id));
    else {
      if (search.trim()) params.set("search", search.trim());
      if (companyFilter.trim()) params.set("company_name", companyFilter.trim());
      if (ownerFilter.trim()) params.set("owner_name", ownerFilter.trim());
      if (cityFilter.trim()) params.set("city", cityFilter.trim());
      if (stateFilter.trim()) params.set("state", stateFilter.trim());
    }
    if (exportCount.trim()) params.set("export_count", String(Number(exportCount)));
    if (sanitizedBaseName) params.set("filename", sanitizedBaseName);
    const response = await fetch(`${API}/exports/leads?${params.toString()}`, {
      credentials: "include",
      headers: { "X-Request-ID": crypto.randomUUID?.() || `export-${Date.now()}` },
    });
    const blob = await validateExportResponse(response);
    const result = await executeExportFlow({ blob, finalFileName });
    setNotice(result.message);
  }

  function toggleSelected(id: string) {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  }

  return (
    <div className="app-shell">
      <header className="topbar">
        <a className="brand" href="#" onClick={(event) => { event.preventDefault(); setTab("leads"); }} aria-label="Lead Gennie home">
          <span className="brand-mark" aria-hidden="true">L</span>
          <span>Lead Gennie<small>Document to lead</small></span>
        </a>
        <div className="topbar-right" aria-label="User account and session">
          <span
            className="topbar-user-name"
            title={user.full_name?.trim() || user.email || "Account"}
            aria-label={`Signed in as ${userDisplayName(user)}`}
          >
            {userDisplayName(user)}
          </span>
          <button className="button button-secondary auth-logout" type="button" onClick={onLogout} aria-label="Log out of Lead Gennie">
            Log out
          </button>
        </div>
      </header>

      <main className="page">
        <div className="page-heading">
          <div><p className="eyebrow">BUSINESS CONTACTS</p><h1>{tab === "form" ? title : "Your leads"}</h1>
            <p className="subheading">Scan business documents and keep your contacts in one place.</p></div>
          <div className="upload-actions">
            <button className="button button-secondary" type="button" onClick={() => { cameraPreparationMsRef.current = 0; setCameraFiles([]); setCameraError(""); setCameraOpen(true); }}>Open camera</button>
            <button className="button button-secondary" type="button" onClick={() => { qrChoicesRef.current = []; setQrChoices([]); setQrResult(null); setWebsitePreview(null); setQrCameraError(""); setQrCameraOpen(true); }}>Scan QR</button>
            <label className="button button-primary upload-button"><span aria-hidden="true">＋</span> Upload files
              <input type="file" multiple accept="image/*,.pdf,.zip,.txt,.csv,.xlsx" onChange={upload} />
            </label>
            <label className="button button-secondary upload-button">Choose folder
              <input type="file" multiple {...({ webkitdirectory: "" } as Record<string, string>)} accept="image/*,.pdf,.zip,.txt,.csv,.xlsx" onChange={upload} />
            </label>
          </div>
        </div>

        <nav className="tabs" aria-label="Main navigation">
          <button className={tab === "leads" ? "tab active" : "tab"} onClick={() => setTab("leads")}>Leads <span className="count">{leadTotal}</span></button>
          <button className={tab === "form" ? "tab active" : "tab"} onClick={startManualEntry}>Add manually</button>
        </nav>

        {error && <div className="alert alert-error" role="alert">{error}<button aria-label="Dismiss" onClick={() => setError("")}>×</button></div>}
        {notice && <div className="alert alert-success" role="status">{notice}<button aria-label="Dismiss" onClick={() => setNotice("")}>×</button></div>}
        {syncCount > 0 && <div className="alert alert-success" role="status">{syncCount} lead{syncCount === 1 ? "" : "s"} waiting to sync. <button className="text-button" onClick={() => void flushQueuedLeads()}>Sync now</button></div>}
        {(pendingDocuments.length > 0 || scanStage) && <div className="scan-progress" role="status"><span className="spinner" />{scanStage || "Preparing scan"}. You will check the details before saving.</div>}

        {tab === "leads" ? (
          <>
          {(selectableScanIssues.length > 0 || rawCandidates.length > 0) && <div className="draft-queue-strip"><span>{rawCandidates.length ? <><strong>{rawCandidates.length} spreadsheet rows</strong> · {rawCandidateIndex} saved · row {rawCandidates[rawCandidateIndex]?.row} ready</> : <><strong>{selectableScanIssues.length} uploaded draft{selectableScanIssues.length === 1 ? "" : "s"}</strong> ready to review</>}</span><button className="button button-secondary" onClick={() => rawCandidates.length || document ? setTab("form") : void reviewDocument(selectableScanIssues.find((item) => item.processing_status === "needs_review") || selectableScanIssues[0])}>{rawCandidates.length ? `Continue row ${rawCandidates[rawCandidateIndex]?.row}` : document ? "Continue current draft" : "Open next draft"}</button></div>}
          <section className="panel">
            <div className="toolbar">
              <div className="search-wrap"><span aria-hidden="true">⌕</span><input value={search} onChange={(event) => { setSearch(event.target.value); setLeadPage(1); }} placeholder="Search company, contact, location..." aria-label="Search leads" /></div>
              <div className="toolbar-actions"><span className="result-count">{leadTotal} {leadTotal === 1 ? "lead" : "leads"}</span>
                <label className="export-count-field"><span>Rows to export</span><input type="number" min="1" step="1" inputMode="numeric" value={exportCount} onChange={(event) => setExportCount(event.target.value)} placeholder="All" aria-label="Number of leads to export" /></label>
                <button className="button button-secondary" onClick={openExportDialog} disabled={(!leadTotal && !selected.size) || exporting}>{exporting ? "Creating Excel file…" : `Export ${selected.size ? `${selected.size} selected` : "Excel"}`}</button>
                <button className="button button-primary" onClick={startManualEntry}>＋ Add lead</button>
              </div>
            </div>
            <div className="lead-filter-row">
              <label><span>Company name</span><input value={companyFilter} onChange={(event) => { setCompanyFilter(event.target.value); setLeadPage(1); }} placeholder="Filter company" /></label>
              <label><span>Owner name</span><input value={ownerFilter} onChange={(event) => { setOwnerFilter(event.target.value); setLeadPage(1); }} placeholder="Filter owner" /></label>
              <label><span>State <small>in address</small></span><input value={stateFilter} onChange={(event) => { setStateFilter(event.target.value); setLeadPage(1); }} placeholder="Filter state" /></label>
              <label><span>City <small>in address</small></span><input value={cityFilter} onChange={(event) => { setCityFilter(event.target.value); setLeadPage(1); }} placeholder="Filter city" /></label>
            </div>
            {leads.length ? <div className="table-scroll"><table><thead><tr>
              <th>S.No.</th><th className="check-cell"><span className="sr-only">Select</span></th><th>Company name</th><th>Data check</th><th>Owner name</th><th>Designation</th><th>Address</th><th>Contact numbers</th><th>Email addresses</th><th>Website</th><th>Social links</th><th>Actions</th>
            </tr></thead><tbody>{leads.map((lead, index) => <tr key={lead.id}>
              <td className="serial-cell">{(leadPage - 1) * LEADS_PAGE_SIZE + index + 1}</td>
              <td className="check-cell"><input type="checkbox" checked={selected.has(lead.id)} onChange={() => toggleSelected(lead.id)} aria-label={`Select ${lead.company_name}`} /></td>
              <td><button className="company-link" onClick={() => editLead(lead)}>{lead.company_name}<small>{lead.lead_number}</small></button></td>
              <td>
                <span className={`verification-pill ${lead.verification_status === "verified" ? "verified" : "needs-review"}`}>{lead.verification_status === "verified" ? "Checked" : lead.document_id ? "Check OCR" : "Check data"}</span>
                {duplicateLeadIds.has(lead.id) && <div><span className="verification-pill duplicate-pill" title="Shares phone number or email address with another lead in this list">Duplicate</span></div>}
              </td>
              <td>{lead.owner_name || <span className="muted">—</span>}</td>
              <td>{lead.designation || <span className="muted">—</span>}</td>
              <td className="address-cell">{lead.company_address || <span className="muted">—</span>}</td>
              <td><div className="cell-stack">{(lead.contact_numbers?.length ? lead.contact_numbers : lead.contact_number ? [lead.contact_number] : []).map((number) => <a key={number} href={`tel:${number.replace(/[^+\d]/g, "")}`}>{indianPhone(number)}</a>)}{!(lead.contact_numbers?.length || lead.contact_number) && <span className="muted">—</span>}</div></td>
              <td><div className="cell-stack">{lead.email_addresses?.length ? lead.email_addresses.map((address) => <a key={address} href={`mailto:${address}`}>{address}</a>) : lead.email ? <a href={`mailto:${lead.email}`}>{lead.email}</a> : <span className="muted">—</span>}</div></td>
              <td>{lead.website ? <a href={lead.website.startsWith("http") ? lead.website : `https://${lead.website}`} target="_blank" rel="noreferrer">{lead.website}</a> : <span className="muted">—</span>}</td>
              <td><div className="cell-stack">{lead.facebook_link && <a href={lead.facebook_link} target="_blank" rel="noreferrer">Facebook</a>}{lead.instagram_link && <a href={lead.instagram_link} target="_blank" rel="noreferrer">Instagram</a>}{lead.whatsapp_link && <a href={lead.whatsapp_link} target="_blank" rel="noreferrer">WhatsApp</a>}{!lead.facebook_link && !lead.instagram_link && !lead.whatsapp_link && <span className="muted">—</span>}</div></td>
              <td className="row-actions"><button className="view-action" aria-label={`View details for ${lead.company_name}`} title="View details" onClick={() => setViewingLead(lead)}><svg aria-hidden="true" viewBox="0 0 24 24"><path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/></svg></button><button onClick={() => editLead(lead)}>Edit</button><button className="delete-action" onClick={() => void removeLead(lead)}>Delete</button></td>
            </tr>)}</tbody></table></div> : <div className="empty-state"><div className="empty-icon">↗</div><h2>{search ? "No matching leads" : "Your lead list is ready"}</h2><p>{search ? "Try a different search." : "Scan cards, upload files or a folder, or add a lead by hand."}</p><div className="empty-actions"><button className="button button-secondary" onClick={startManualEntry}>Add manually</button></div></div>}
            <div className="pagination-controls"><span>{leadTotal ? `Showing ${(leadPage - 1) * LEADS_PAGE_SIZE + 1}–${Math.min(leadPage * LEADS_PAGE_SIZE, leadTotal)} of ${leadTotal} leads` : "No leads to show"}</span><div><button className="button button-secondary" disabled={leadPage <= 1} onClick={() => setLeadPage((page) => Math.max(1, page - 1))}>Previous</button><span>Page {leadPage} of {Math.max(1, Math.ceil(leadTotal / LEADS_PAGE_SIZE))}</span><button className="button button-secondary" disabled={leadPage >= Math.ceil(leadTotal / LEADS_PAGE_SIZE)} onClick={() => setLeadPage((page) => page + 1)}>Next</button></div></div>
            </section>
          </>
        ) : (
          <section className={`review-layout${document || localPreviewUrl ? " with-source-preview" : ""}`}>
            {!document && localPreviewUrl && <aside className="panel source-preview-panel"><div className="source-preview-heading"><span className="eyebrow">LOCAL SOURCE PREVIEW</span><strong>Images remain on this device until Save</strong></div><div className="local-preview-grid">{(localPreviewUrls.length ? localPreviewUrls : [localPreviewUrl]).map((url, index) => <figure key={url}><img src={url} alt={`Locally processed card image ${index + 1}`} /><figcaption>{index === 0 ? "Front" : `Back / image ${index + 1}`}</figcaption></figure>)}</div></aside>}
            {document && <aside className="panel source-preview-panel"><div className="source-preview-heading"><div className="source-preview-topline"><span className="eyebrow">UPLOADED DOCUMENT</span><button type="button" className="button button-secondary source-back-button" onClick={() => setTab("leads")}>← Back to leads</button></div><strong title={document.original_filename}>{document.original_filename}</strong></div>{document.content_type === "application/pdf" ? <iframe title={`Preview of ${document.original_filename}`} src={`${API}/documents/${document.id}/file`} /> : <img src={`${API}/documents/${document.id}/file`} alt={`Uploaded source ${document.original_filename}`} />}</aside>}
            <div className="panel form-panel review-form-panel">
              {document && <div className="source-summary">
                <div><span className="eyebrow">SOURCE DOCUMENT</span><strong title={document.original_filename}>{document.original_filename}</strong></div>
                <span className={`state-pill state-${document.processing_status}`}>{document.processing_status.replace("_", " ")}</span>
                <a className="button button-secondary" href={`${API}/documents/${document.id}/file`} target="_blank" rel="noreferrer">Open source</a>
                {document.error_message && <div className="ocr-error"><strong>OCR needs help</strong><span>{document.error_message} You can enter the details manually.</span>
                  {document.processing_status === "failed" && <button className="text-button" onClick={() => void retryDocument(document)}>Retry OCR</button>}
                </div>}
                {selectableScanIssues.some((item) => item.id !== document.id) && <label className="draft-picker"><span>Uploaded drafts in this batch</span><select value="" onChange={(event) => { const next = selectableScanIssues.find((item) => item.id === event.target.value); if (next) void reviewDocument(next); }}><option value="">Open another draft…</option>{selectableScanIssues.filter((item) => item.id !== document.id).map((item) => <option value={item.id} key={item.id}>{item.original_filename} · {item.processing_status.replace("_", " ")}</option>)}</select></label>}
                {busy && <p className="processing-note"><span className="spinner" />{scanStage || "Preparing scan"}…</p>}
                {(document.extracted_fields.cards?.[activeCardIndex]?.translation_message || document.extracted_fields.translation_message) && <p role="status" className="review-warning">{textValue(document.extracted_fields.cards?.[activeCardIndex]?.translation_message || document.extracted_fields.translation_message)}</p>}
                {(document.extracted_fields.cards?.[activeCardIndex]?.review_warnings || document.extracted_fields.review_warnings || []).length > 0 && <ul className="review-warning" role="status">{(document.extracted_fields.cards?.[activeCardIndex]?.review_warnings || document.extracted_fields.review_warnings || []).map((warning) => <li key={warning}>{warning}</li>)}</ul>}
                {(() => { const measured = document.extracted_fields.cards?.[activeCardIndex]?.processing_times_ms || document.extracted_fields.processing_times_ms; const clientTiming = clientScanTiming?.documentId === document.id ? clientScanTiming : null; return measured ? <details className="processing-note"><summary>Scan timing</summary><p>{clientTiming ? `Browser image prep ${(clientTiming.prepareMs / 1000).toFixed(2)}s · upload ${(clientTiming.uploadMs / 1000).toFixed(1)}s · ` : ""}queue {( (measured.queue_ms || 0) / 1000).toFixed(1)}s · image decode {( (measured.decode_ms || 0) / 1000).toFixed(2)}s · server image prep {( (measured.preprocess_ms || 0) / 1000).toFixed(2)}s · OCR {( (measured.ocr_ms || 0) / 1000).toFixed(1)}s · model load {( (measured.translation_model_init_ms || 0) / 1000).toFixed(1)}s · translation {( (measured.translation_inference_ms ?? measured.translation_ms ?? 0) / 1000).toFixed(1)}s · fields {( (measured.field_extraction_ms || 0) / 1000).toFixed(2)}s · QR {( (measured.qr_ms || 0) / 1000).toFixed(2)}s · server {( (measured.total_ms || 0) / 1000).toFixed(1)}s{clientTiming ? ` · app-to-result ${(clientTiming.totalMs / 1000).toFixed(1)}s` : ""}</p></details> : null; })()}
                {(() => { const sources = document.extracted_fields.cards?.[activeCardIndex]?.field_sources || document.extracted_fields.field_sources; const entries = Object.entries(sources || {}); return entries.length ? <details><summary>Source line for each extracted field</summary><ul>{entries.map(([key, source]) => <li key={key}><strong>{key.replace(/_/g, " ")}</strong> · {source.source_type} · {source.confidence === "high" ? "high confidence" : "check against card"}: <span>{source.source_text || "No exact source line found"}</span></li>)}</ul></details> : null; })()}
                {!!(document.extracted_fields.cards?.[activeCardIndex]?.qr_payloads || document.extracted_fields.qr_payloads)?.length && <details><summary>QR code data for verification</summary><pre style={{ whiteSpace: "pre-wrap" }}>{(document.extracted_fields.cards?.[activeCardIndex]?.qr_payloads || document.extracted_fields.qr_payloads || []).join("\n\n")}</pre></details>}
                {!!(document.extracted_fields.cards?.[activeCardIndex]?.original_ocr_text || document.extracted_fields.original_ocr_text) && <details><summary>Original OCR text</summary><pre style={{ whiteSpace: "pre-wrap" }}>{textValue(document.extracted_fields.cards?.[activeCardIndex]?.original_ocr_text || document.extracted_fields.original_ocr_text)}</pre></details>}
                {!busy && document.processing_status !== "uploaded" && document.processing_status !== "processing" && !editingLead && <button type="button" className="text-button" onClick={() => void retryDocument(document)}>Re-scan and translate</button>}
              </div>}
              {!document && !editingLead && <div className="candidate-summary"><span className="eyebrow">{rawCandidates.length ? "RAW DATA REVIEW" : localPreviewUrl ? "LOCAL OCR REVIEW" : "MANUAL ENTRY"}</span><p>{rawCandidates.length ? `${rawCandidates[rawCandidateIndex]?.source} · Row ${rawCandidates[rawCandidateIndex]?.row} · ${rawCandidateIndex + 1} of ${rawCandidates.length}. Review rows individually or generate the remaining Excel leads at once.` : localPreviewUrl ? "OCR ran on this device. Review the fields; images remain local until Save." : "Enter company information below, then select Save lead."}</p>{localConflicts.length > 0 && <div className="review-warning"><strong>Conflicting values found across card images:</strong><ul>{localConflicts.map((item) => <li key={item}>{item}</li>)}</ul><p>Review and keep the value that matches the card.</p></div>}{ocrFallbackFiles.length > 0 && <div className="review-warning"><p>Browser OCR could not reliably read this image. It has not been uploaded.</p><div className="form-actions"><button type="button" className="button button-secondary" disabled={busy} onClick={() => void uploadFiles(ocrFallbackFiles, false, false, false)}>Retry locally</button><button type="button" className="button button-primary" disabled={busy} onClick={() => void uploadFiles(ocrFallbackFiles, false, false, true)}>Continue with server OCR</button><button type="button" className="text-button" disabled={busy} onClick={() => { setOcrFallbackFiles([]); setLocalPreviewUrl((current) => { if (current) URL.revokeObjectURL(current); return ""; }); setLocalPreviewUrls((current) => { current.forEach((url) => URL.revokeObjectURL(url)); return []; }); setTab("leads"); }}>Cancel</button></div></div>}{canBulkGenerate && <button type="button" className="button button-primary generate-batch-button" onClick={() => void generateAllExcelLeads()} disabled={busy}>{busy ? "Generating leads…" : `Generate ${rawCandidates.slice(rawCandidateIndex).length} remaining leads in one click`}</button>}</div>}
              <div className="form-heading"><span className="eyebrow">LEAD INFORMATION</span><h2>{editingLead ? editingLead.lead_number : document?.extracted_fields.cards && document.extracted_fields.cards.length > 1 ? `Card ${activeCardIndex + 1} of ${document.extracted_fields.cards.length}` : "Add lead"}</h2><p>{editingLead ? "Correct any details, then save your changes." : "Enter the company information and save the lead."}</p></div>
              {error && <div className="alert alert-error form-error" role="alert">{error}</div>}
              <form onSubmit={saveLead}>
                {fields.map((field) => <label className="field" key={field.key}><span>{field.label}{field.key === "company_name" && <b>Required</b>}</span>
                {field.key === "company_address" || field.key === "business_description" ? <textarea rows={3} value={form[field.key]} onChange={(event) => setForm((current) => updateFormField(current, { [field.key]: event.target.value }))} placeholder={field.placeholder} /> : <input ref={field.key === "company_name" ? companyInputRef : undefined} value={form[field.key] || ""} onChange={(event) => setForm((current) => updateFormField(current, { [field.key]: event.target.value }))} placeholder={field.placeholder} required={field.key === "company_name"} />}
                </label>)}
                <label className="field"><span>Contact numbers <small>One per line</small> <b>Required</b></span><textarea ref={contactNumbersRef} rows={3} value={form.contact_numbers.join("\n")} onChange={(event) => setForm((current) => updateFormField(current, { contact_numbers: listFromText(event.target.value) }))} placeholder={"+91 98765 43210\n+91 98765 43211"} /></label>
                <label className="field"><span>Email addresses <small>One per line</small></span><textarea rows={3} value={form.email_addresses.join("\n")} onChange={(event) => setForm((current) => updateFormField(current, { email_addresses: listFromText(event.target.value, /[\n;,]+/) }))} placeholder="name@company.com&#10;sales@company.com" /></label>
                <label className="field"><span>Other text from document <small>Edit labeled lines here to update the matching fields automatically</small></span><textarea className="raw-text-area" rows={6} value={form.raw_text} onChange={(event) => setForm((current) => updateFormFromDocumentText(current, event.target.value))} placeholder="All readable text from the source document appears here." /></label>
                {needsSourceReview && <section className="review-check" aria-labelledby="review-check-title">
                  <h3 id="review-check-title">Check details before saving</h3>
                  {!hasOcrText ? <p className="review-warning">Source text is unavailable. Compare every field directly with the original source.</p> : mismatches.length ? <><p className="review-warning">Check these possible mismatches against the original. Correct any errors before saving:</p><ul>{mismatches.map((item) => <li key={item}>{item}</li>)}</ul></> : <p className="review-match">Structured values are consistent with the stored source text. OCR can still misread characters, so compare them with the original source.</p>}
                  <label className="review-confirm"><input type="checkbox" checked={sourceReviewConfirmed} onChange={(event) => setReviewedFingerprint(event.target.checked ? formFingerprint : "")} /><span>I compared the lead details with the available source and corrected any mismatches.</span></label>
                </section>}
                {duplicateInfo?.is_duplicate && (
                  <div className="review-warning duplicate-warning" role="alert">
                    <strong>⚠️ Duplicate Lead Detected:</strong> A lead with this {duplicateInfo.matched_type} (<code>{duplicateInfo.matched_value}</code>) already exists as <strong>{duplicateInfo.existing_company_name}</strong> ({duplicateInfo.existing_lead_number}).
                    <p>Saving is blocked to prevent duplicate data in the database.</p>
                  </div>
                )}
                <div className="form-actions">{rawCandidates.length > 0 && !editingLead && <button type="button" className="button button-secondary" onClick={() => setTab("leads")}>Back to leads</button>}<button type="button" className="button button-secondary" onClick={() => { setTab("leads"); setDocument(null); setOcrFallbackFiles([]); setLocalOriginalOcrText(""); setLocalPreviewUrl((current) => { if (current) URL.revokeObjectURL(current); return ""; }); setLocalPreviewUrls((current) => { current.forEach((url) => URL.revokeObjectURL(url)); return []; }); setEditingLead(null); setError(""); setBusy(false); setDuplicateInfo(null); }}>Cancel</button><button className="button button-primary" disabled={busy || Boolean(editingLead?.document_id && !document) || Boolean(duplicateInfo?.is_duplicate)}>{busy ? "Working…" : editingLead ? "Save changes" : rawCandidates.length > 0 && rawCandidateIndex + 1 < rawCandidates.length ? "Save & continue" : "Save lead"}</button></div>
              </form>
            </div>
          </section>
        )}
        {cameraOpen && <div className="camera-backdrop" role="presentation"><section className="camera-dialog" role="dialog" aria-modal="true" aria-labelledby="camera-title"><header><div><span className="eyebrow">LIVE SCANNER</span><h2 id="camera-title">Capture card images</h2><p>{cameraFiles.length ? `${cameraFiles.length} image${cameraFiles.length === 1 ? "" : "s"} captured. Capture the back side too, then scan.` : "Capture the front side, then optionally capture the back side."}</p><p className="camera-tip">For a sharper scan, use bright even light, hold the phone steady, and fit the whole card inside the frame.</p></div><button className="details-close" aria-label="Close camera" onClick={() => { cameraPreparationMsRef.current = 0; setCameraFiles([]); setCameraOpen(false); }}>×</button></header><div className="camera-preview"><video ref={videoRef} playsInline autoPlay muted /><div className="scanner-frame" /></div>{cameraError && <p className="camera-error" role="alert">{cameraError}</p>}<div className="camera-actions"><button className="button button-secondary" onClick={() => { cameraPreparationMsRef.current = 0; setCameraFiles([]); setCameraOpen(false); }}>Cancel</button><button className="button button-secondary" onClick={() => void capturePhoto()}>Capture {cameraFiles.length ? "another" : "image"}</button><button className="button button-primary" onClick={() => void finishCameraScan()} disabled={!cameraFiles.length}>Scan {cameraFiles.length || ""} image{cameraFiles.length === 1 ? "" : "s"}</button></div></section></div>}
        {qrCameraOpen && <div className="camera-backdrop" role="presentation"><section className="camera-dialog" role="dialog" aria-modal="true" aria-labelledby="qr-camera-title"><header><div><span className="eyebrow">QR SCANNER</span><h2 id="qr-camera-title">Scan a QR code</h2><p>Point the rear camera at a QR code. The code will be decoded locally and shown for review.</p></div><button className="details-close" aria-label="Close QR scanner" onClick={() => setQrCameraOpen(false)}>×</button></header><div className="camera-preview"><video ref={qrVideoRef} playsInline autoPlay muted /><div className="scanner-frame" /></div>{qrCameraError && <p className="camera-error" role="alert">{qrCameraError}</p>}<div className="camera-actions"><label className="button button-secondary">Choose QR image<input type="file" accept="image/*" style={{ display: "none" }} onChange={uploadQrImage} /></label><button className="button button-secondary" onClick={() => setQrCameraOpen(false)}>Cancel</button></div></section></div>}
        {qrChoices.length > 1 && <section className="qr-result panel" aria-label="Choose QR code"><strong>Choose a QR code</strong>{qrChoices.map(payload => <button type="button" className="button button-secondary" style={{ whiteSpace: "normal", overflowWrap: "anywhere" }} key={payload} onClick={() => acceptQrPayload(payload)}>{parseQrLocally(payload).kind}: {payload}</button>)}</section>}
        {qrResult && <section className="panel" aria-label="QR link details"><p>QR type: {safeText(qrResult.kind) || "text"}. Review the source before using it.</p>{["whatsapp_link", "instagram_link", "facebook_link", "map_url", "app_url", "qr_redirect_url"].map(key => { const value = safeText(qrResult[key]); return /^https?:\/\//i.test(value) ? <p key={key}><a href={value} target="_blank" rel="noreferrer">{key.replace(/_/g, " ")}: {value}</a></p> : null; })}{Boolean(qrResult.location_coordinates) && <p>Coordinates: {JSON.stringify(qrResult.location_coordinates)}. A street address has not been verified.</p>}{localConflicts.length > 0 && <ul>{localConflicts.map(item => <li key={item}>{item}</li>)}</ul>}</section>}
        {qrResult && <section className="qr-result panel" aria-label="QR result"><div><strong>QR data decoded</strong><p className="muted">{safeText(qrResult.source_text)}</p></div>{safeText(qrResult.website) && <div className="qr-result-actions"><a className="button button-secondary" href={safeText(qrResult.website)} target="_blank" rel="noreferrer">Open website</a><button className="button button-secondary" type="button" onClick={() => void previewQrWebsite()} disabled={busy}>Read public page</button></div>}{websitePreview && <details open><summary>Website preview</summary><p>{websitePreview.source_text || "No readable metadata was found."}</p>{websitePreview.warnings.map((warning) => <p className="camera-error" key={warning}>{warning}</p>)}</details>}</section>}
        {viewingLead && <div className="details-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) setViewingLead(null); }}>
          <section className="details-dialog" role="dialog" aria-modal="true" aria-labelledby="details-title">
            <header className="details-heading"><div><span className="eyebrow">LEAD DETAILS</span><h2 id="details-title">{viewingLead.company_name}</h2><p>{viewingLead.lead_number}</p></div><button className="details-close" aria-label="Close details" onClick={() => setViewingLead(null)}>×</button></header>
            <dl className="details-grid">
              <div><dt>Data check</dt><dd>{viewingLead.verification_status === "verified" ? "Checked" : viewingLead.document_id ? "OCR needs review" : "Imported data needs review"}</dd></div>
              <div><dt>Owner / contact</dt><dd>{viewingLead.owner_name || "—"}</dd></div>
              <div><dt>Designation</dt><dd>{viewingLead.designation || "—"}</dd></div>
              <div><dt>Business description / services</dt><dd>{viewingLead.business_description || "—"}</dd></div>
              <div><dt>Address</dt><dd>{viewingLead.company_address || "—"}</dd></div>
              <div><dt>Contact numbers</dt><dd>{(viewingLead.contact_numbers?.length ? viewingLead.contact_numbers : viewingLead.contact_number ? [viewingLead.contact_number] : []).length ? (viewingLead.contact_numbers?.length ? viewingLead.contact_numbers : [viewingLead.contact_number!]).map((number) => <a className="detail-value" key={number} href={`tel:${number.replace(/[^+\d]/g, "")}`}>{indianPhone(number)}</a>) : "—"}</dd></div>
              <div><dt>Email addresses</dt><dd>{(viewingLead.email_addresses?.length ? viewingLead.email_addresses : viewingLead.email ? [viewingLead.email] : []).length ? (viewingLead.email_addresses?.length ? viewingLead.email_addresses : [viewingLead.email!]).map((address) => <a className="detail-value" key={address} href={`mailto:${address}`}>{address}</a>) : "—"}</dd></div>
              <div><dt>Website</dt><dd>{viewingLead.website ? <a href={viewingLead.website.startsWith("http") ? viewingLead.website : `https://${viewingLead.website}`} target="_blank" rel="noreferrer">{viewingLead.website}</a> : "—"}</dd></div>
              <div><dt>Facebook</dt><dd>{viewingLead.facebook_link ? <a href={viewingLead.facebook_link} target="_blank" rel="noreferrer">{viewingLead.facebook_link}</a> : "—"}</dd></div>
              <div><dt>Instagram</dt><dd>{viewingLead.instagram_link ? <a href={viewingLead.instagram_link} target="_blank" rel="noreferrer">{viewingLead.instagram_link}</a> : "—"}</dd></div>
              <div><dt>WhatsApp</dt><dd>{viewingLead.whatsapp_link ? <a href={viewingLead.whatsapp_link} target="_blank" rel="noreferrer">{viewingLead.whatsapp_link}</a> : "—"}</dd></div>
            </dl>
            {viewingLead.raw_text && <div className="details-raw"><h3>Full text from document</h3><pre>{viewingLead.raw_text}</pre></div>}
            {viewingLead.original_ocr_text && <div className="details-raw"><h3>Original OCR transcript</h3><pre>{viewingLead.original_ocr_text}</pre></div>}
            {viewingLead.document_id && <a className="details-source" href={`${API}/documents/${viewingLead.document_id}/file`} target="_blank" rel="noreferrer">Open source document ↗</a>}
            {viewingLead.source_document_ids?.filter((id) => id !== viewingLead.document_id).map((id, index) => <a className="details-source" key={id} href={`${API}/documents/${id}/file`} target="_blank" rel="noreferrer">Open {index ? "additional" : "back-side"} source image ↗</a>)}
            <footer className="details-actions"><button className="button button-secondary" onClick={() => setViewingLead(null)}>Close</button><button className="button button-primary" onClick={() => { setViewingLead(null); void editLead(viewingLead); }}>Edit lead</button></footer>
          </section>
        </div>}
        {exportDialogOpen && (
          <div
            className="export-backdrop"
            role="presentation"
            onMouseDown={(event) => {
              if (event.target === event.currentTarget && !exporting) {
                handleCancelExport();
              }
            }}
          >
            <section
              className="export-dialog"
              role="dialog"
              aria-modal="true"
              aria-labelledby="export-dialog-title"
              onKeyDown={(event) => {
                if (event.key === "Escape" && !exporting) {
                  event.stopPropagation();
                  handleCancelExport();
                }
              }}
            >
              <header>
                <div>
                  <span className="eyebrow">SPREADSHEET EXPORT</span>
                  <h2 id="export-dialog-title">Export leads</h2>
                  <p className="export-dialog-copy">
                    {selected.size
                      ? `Exporting ${selected.size} selected lead${selected.size === 1 ? "" : "s"}.`
                      : exportCount.trim()
                      ? `Exporting up to ${exportCount.trim()} matching lead${Number(exportCount.trim()) === 1 ? "" : "s"}.`
                      : `Exporting all ${leadTotal} matching lead${leadTotal === 1 ? "" : "s"}.`}
                  </p>
                </div>
                <button
                  type="button"
                  className="details-close"
                  aria-label="Close export dialog"
                  disabled={exporting}
                  onClick={handleCancelExport}
                >
                  ×
                </button>
              </header>
              <form
                onSubmit={(event) => {
                  event.preventDefault();
                  void handleConfirmExport();
                }}
              >
                <div className="field">
                  <label htmlFor="export-file-name">File name</label>
                  <input
                    ref={exportInputRef}
                    id="export-file-name"
                    type="text"
                    value={exportFileNameInput}
                    onChange={(event) => {
                      setExportFileNameInput(event.target.value);
                      if (exportFileNameError) setExportFileNameError("");
                    }}
                    placeholder={DEFAULT_EXPORT_BASE_NAME}
                    className={exportFileNameError ? "has-error" : ""}
                    aria-invalid={Boolean(exportFileNameError)}
                    aria-describedby={exportFileNameError ? "export-file-error" : undefined}
                    disabled={exporting}
                    autoFocus
                  />
                </div>
                <div className="export-dialog-error" id="export-file-error" role="alert">
                  {exportFileNameError || ""}
                </div>
                <div className="export-dialog-actions">
                  <button
                    type="button"
                    className="button button-secondary"
                    onClick={handleCancelExport}
                    disabled={exporting}
                  >
                    Cancel
                  </button>
                  <button
                    type="submit"
                    className="button button-primary"
                    disabled={exporting}
                  >
                    {exporting ? "Creating Excel file…" : "Export"}
                  </button>
                </div>
              </form>
            </section>
          </div>
        )}
        <footer>Lead Gennie <span>·</span> Scan, save, and organize business contacts</footer>
      </main>
    </div>
  );
}

function AuthScreen({ onAuthenticated }: { onAuthenticated: (user: AuthUser) => void }) {
  const [mode, setMode] = useState<"login" | "register" | "forgot" | "reset">(new URLSearchParams(window.location.search).has("token") ? "reset" : "login");
  const [fullName, setFullName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function submit(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError(""); setMessage("");
    try {
      if (mode === "login") onAuthenticated(await api<AuthUser>("/auth/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ email, password }) }));
      else if (mode === "register") onAuthenticated(await api<AuthUser>("/auth/register", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ full_name: fullName, email, password }) }));
      else if (mode === "forgot") setMessage((await api<{ message: string }>("/auth/forgot-password", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ email }) })).message);
      else setMessage((await api<{ message: string }>("/auth/reset-password", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ token: new URLSearchParams(window.location.search).get("token"), password }) })).message);
    } catch (err) { setError(err instanceof Error ? err.message : "Authentication failed."); } finally { setBusy(false); }
  }
  const title = mode === "login" ? "Welcome back" : mode === "register" ? "Create your account" : mode === "forgot" ? "Forgot password" : "Set a new password";
  return <main className="auth-page"><section className="auth-card"><span className="brand-mark">L</span><p className="eyebrow">LEAD GENNIE</p><h1>{title}</h1><p className="auth-copy">Securely scan, review, and manage your business leads.</p><form onSubmit={submit}>{mode === "register" && <label className="field"><span>Full name</span><input required value={fullName} onChange={(event) => setFullName(event.target.value)} autoComplete="name" /></label>}<label className="field"><span>Email</span><input required type="email" value={email} onChange={(event) => setEmail(event.target.value)} autoComplete="email" /></label>{mode !== "forgot" && <label className="field"><span>{mode === "reset" ? "New password" : "Password"}</span><input required type="password" minLength={8} value={password} onChange={(event) => setPassword(event.target.value)} autoComplete={mode === "login" ? "current-password" : "new-password"} /></label>}<button className="button button-primary auth-submit" disabled={busy}>{busy ? "Please wait…" : mode === "forgot" ? "Send reset instructions" : mode === "reset" ? "Reset password" : mode === "register" ? "Create account" : "Log in"}</button></form>{error && <p className="auth-error" role="alert">{error}</p>}{message && <p className="auth-message" role="status">{message}</p>}<div className="auth-links">{mode === "login" && <><button onClick={() => setMode("forgot")}>Forgot password?</button><button onClick={() => setMode("register")}>Create an account</button></>}{mode !== "login" && <button onClick={() => setMode("login")}>Back to login</button>}</div></section></main>;
}

function App() {
  const [user, setUser] = useState<AuthUser | null>(null);
  const [checking, setChecking] = useState(true);
  useEffect(() => { api<AuthUser>("/auth/me").then(setUser).catch(() => setUser(null)).finally(() => setChecking(false)); }, []);
  if (checking) return <main className="auth-page"><p>Loading…</p></main>;
  if (!user) return <AuthScreen onAuthenticated={setUser} />;
  return <LeadApp user={user} onLogout={() => { void api("/auth/logout", { method: "POST" }).finally(() => setUser(null)); }} />;
}

export default App;
