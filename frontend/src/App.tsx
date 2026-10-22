import { ChangeEvent, FormEvent, useCallback, useEffect, useRef, useState } from "react";
import "./details.css";

const API = import.meta.env.VITE_API_URL || `http://${window.location.hostname}:8000/api`;

type Lead = {
  id: string;
  lead_number: string;
  company_name: string;
  owner_name: string | null;
  designation: string | null;
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
type LeadPage = { leads: Lead[]; total: number; page: number; page_size: number };
const LEADS_PAGE_SIZE = 50;

const emptyForm: LeadForm = {
  company_name: "",
  owner_name: "",
  designation: "",
  company_address: "",
  contact_numbers: [],
  email_addresses: [],
  website: "",
  facebook_link: "",
  instagram_link: "",
  whatsapp_link: "",
  raw_text: "",
};

const fields: { key: "company_name" | "owner_name" | "designation" | "company_address" | "website" | "facebook_link" | "instagram_link" | "whatsapp_link"; label: string; placeholder: string }[] = [
  { key: "company_name", label: "Full company name", placeholder: "Example Technologies Pvt Ltd" },
  { key: "owner_name", label: "Owner name", placeholder: "Contact person" },
  { key: "designation", label: "Designation", placeholder: "Director, Manager, etc." },
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

function stringValues(values: string[] | null | undefined, fallback?: string | null): string[] {
  const source = Array.isArray(values) ? values : fallback ? [fallback] : [];
  return [...new Set(source.filter((value): value is string => typeof value === "string").map((value) => value.trim()).filter(Boolean))];
}

function listFromText(value: string, separators: RegExp = /[\n;]+/): string[] {
  return value.split(separators).map((item) => item.trim()).filter(Boolean);
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
  const response = await fetch(`${API}${path}`, init);
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new Error(body?.detail || `Request failed (${response.status})`);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

function App() {
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
  const [form, setForm] = useState<LeadForm>(emptyForm);
  const [editingLead, setEditingLead] = useState<Lead | null>(null);
  const [viewingLead, setViewingLead] = useState<Lead | null>(null);
  const [reviewedFingerprint, setReviewedFingerprint] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const videoRef = useRef<HTMLVideoElement>(null);
  const cameraStreamRef = useRef<MediaStream | null>(null);

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
    navigator.mediaDevices?.getUserMedia({ video: { facingMode: { ideal: "environment" } }, audio: false })
      .then((stream) => {
        if (cancelled) {
          stream.getTracks().forEach((track) => track.stop());
          return;
        }
        cameraStreamRef.current = stream;
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
    if (!pendingDocuments.some((item) => ["uploaded", "processing"].includes(item.processing_status))) return;
    const timer = window.setTimeout(async () => {
      try {
        const statuses = await api<DocumentStatus[]>("/documents/status", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ids: pendingDocuments.map((item) => item.id) }),
        });
        const statusById = new Map(statuses.map((item) => [item.id, item]));
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
          const completed = updated.find((item) => (!reviewBatchIds.length || reviewBatchIds.includes(item.id)) && (item.processing_status === "needs_review" || item.processing_status === "failed"));
          if (completed && !rawCandidates.length && (!document || document.id === completed.id)) {
            try {
              const fullDocument = await api<DocumentRecord>(`/documents/${completed.id}`);
              setDocument(fullDocument);
              setForm(formFromExtracted(fullDocument.extracted_fields || {}));
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
        setError(err instanceof Error ? `${err.message}. Status checks will retry automatically.` : "Could not check document status. Status checks will retry automatically.");
        setPendingDocuments((current) => [...current]);
      }
    }, 1400);
    return () => window.clearTimeout(timer);
  }, [pendingDocuments, loadScanIssues, rawCandidates.length, document, reviewBatchIds]);

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

  async function upload(event: ChangeEvent<HTMLInputElement>) {
    const files = Array.from(event.target.files || []);
    event.target.value = "";
    if (files.length) await uploadFiles(files);
  }

  async function uploadFiles(files: File[]) {
    if (!files.length) return;
    setError("");
    setNotice("");
    setBusy(true);
    setEditingLead(null);
    setReviewedFingerprint("");
    setForm(emptyForm);
    setRawCandidates([]);
    setReviewBatchIds([]);
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
      const uploadedDocuments: DocumentRecord[] = [];
      const skippedFiles: string[] = [];
      for (let start = 0; start < documentFiles.length; start += 20) {
        const data = new FormData();
        documentFiles.slice(start, start + 20).forEach((file) => data.append("files", file));
        const result = await api<DocumentBatchResult>("/documents/batch", { method: "POST", body: data });
        uploadedDocuments.push(...result.documents);
        skippedFiles.push(...result.skipped_files.map((item) => `${item.filename}: ${item.reason}`));
      }
      if (!rawFiles.length) setDocument(uploadedDocuments[0] || null);
      setActiveCardIndex(0);
      setPendingDocuments(uploadedDocuments);
      setScanIssues((current) => mergeDocuments(current, uploadedDocuments));
      setReviewBatchIds(uploadedDocuments.map((item) => item.id));
      if (!rawFiles.length && uploadedDocuments.length) {
        setForm(formFromExtracted(uploadedDocuments[0].extracted_fields || {}));
        setTab("form");
      }
      if (uploadedDocuments.length) setNotice(`${uploadedDocuments.length} document${uploadedDocuments.length === 1 ? "" : "s"} queued. OCR results will open for review and are saved only when you confirm each lead.`);
      if (!uploadedDocuments.length) setBusy(false);
      if (unsupportedCount || skippedFiles.length) setError([...skippedFiles, ...(unsupportedCount ? [`${unsupportedCount} unsupported file${unsupportedCount === 1 ? " was" : "s were"} skipped`] : [])].join("\n"));
    } catch (err) {
      setBusy(false);
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
      const result = await api<{ created: number }>("/leads/batch", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(validCandidates),
      });
      setLeadPage(1);
      await loadLeads(1);
      setRawCandidates([]);
      setRawCandidateIndex(0);
      setForm(emptyForm);
      setTab("leads");
      setNotice(`${result.created} lead${result.created === 1 ? "" : "s"} generated. ${skipped ? `${skipped} row${skipped === 1 ? " was" : "s were"} skipped because the company name was blank. ` : ""}Generated records are marked Check data until reviewed.`);
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
    const canvas = window.document.createElement("canvas");
    canvas.width = video.videoWidth;
    canvas.height = video.videoHeight;
    canvas.getContext("2d")?.drawImage(video, 0, 0, canvas.width, canvas.height);
    const blob = await new Promise<Blob | null>((resolve) => canvas.toBlob(resolve, "image/jpeg", 0.92));
    if (!blob) {
      setCameraError("Could not capture the photo. Try again or upload an image.");
      return;
    }
    setCameraOpen(false);
    await uploadFiles([new File([blob], `camera-${Date.now()}.jpg`, { type: "image/jpeg" })]);
  }

  async function retryDocument(source: DocumentRecord) {
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
    if (needsSourceReview && !sourceReviewConfirmed) {
      setError("Compare the lead details with the source, correct any mismatches, and confirm the check before saving.");
      return;
    }
    setBusy(true);
    const payload = {
      ...form,
      owner_name: form.owner_name || null,
      designation: form.designation || null,
      company_address: form.company_address || null,
      contact_numbers: form.contact_numbers,
      contact_number: form.contact_numbers[0] || null,
      email_addresses: form.email_addresses,
      email: form.email_addresses[0] || null,
      raw_text: form.raw_text || null,
      website: form.website || null,
      facebook_link: form.facebook_link || null,
      instagram_link: form.instagram_link || null,
      whatsapp_link: form.whatsapp_link || null,
      document_id: document?.id || null,
      document_card_index: document?.extracted_fields.cards && document.extracted_fields.cards.length > 1 ? activeCardIndex : null,
      original_ocr_text: editingLead?.original_ocr_text || textValue(document?.extracted_fields.cards?.[activeCardIndex]?.original_ocr_text || document?.extracted_fields.original_ocr_text) || null,
      source_review_confirmed: !needsSourceReview || sourceReviewConfirmed,
    };
    try {
      if (editingLead) {
        await api<Lead>(`/leads/${editingLead.id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
        setNotice("Lead updated.");
      } else {
        await api<Lead>("/leads", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
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
        setEditingLead(null);
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
      setError(err instanceof Error ? err.message : "Could not save lead.");
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

  async function exportLeads() {
    try {
      setError("");
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
      const response = await fetch(`${API}/exports/leads?${params.toString()}`);
      if (!response.ok) throw new Error("Could not create spreadsheet.");
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const anchor = window.document.createElement("a");
      anchor.href = url;
      anchor.download = "leads.xlsx";
      anchor.click();
      URL.revokeObjectURL(url);
      setNotice("Spreadsheet downloaded. You can now share the file.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Export failed.");
    }
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
        <a className="brand" href="#" onClick={(event) => { event.preventDefault(); setTab("leads"); }}>
          <span className="brand-mark">L</span>
          <span>Lead Gennie<small>Document to lead</small></span>
        </a>
      </header>

      <main className="page">
        <div className="page-heading">
          <div><p className="eyebrow">BUSINESS CONTACTS</p><h1>{tab === "form" ? title : "Your leads"}</h1>
            <p className="subheading">Scan business documents and keep your contacts in one place.</p></div>
          <div className="upload-actions">
            <button className="button button-secondary" type="button" onClick={() => setCameraOpen(true)}>Open camera</button>
            <label className="button button-primary upload-button"><span aria-hidden="true">＋</span> Upload files
              <input type="file" multiple accept=".jpg,.jpeg,.png,.webp,.bmp,.tif,.tiff,.gif,.pdf,.zip,.txt,.csv,.xlsx" onChange={upload} />
            </label>
            <label className="button button-secondary upload-button">Choose folder
              <input type="file" multiple {...({ webkitdirectory: "" } as Record<string, string>)} accept=".jpg,.jpeg,.png,.webp,.bmp,.tif,.tiff,.gif,.pdf,.zip,.txt,.csv,.xlsx" onChange={upload} />
            </label>
          </div>
        </div>

        <nav className="tabs" aria-label="Main navigation">
          <button className={tab === "leads" ? "tab active" : "tab"} onClick={() => setTab("leads")}>Leads <span className="count">{leadTotal}</span></button>
          <button className={tab === "form" ? "tab active" : "tab"} onClick={startManualEntry}>Add manually</button>
        </nav>

        {error && <div className="alert alert-error" role="alert">{error}<button aria-label="Dismiss" onClick={() => setError("")}>×</button></div>}
        {notice && <div className="alert alert-success" role="status">{notice}<button aria-label="Dismiss" onClick={() => setNotice("")}>×</button></div>}
        {pendingDocuments.length > 0 && <div className="scan-progress" role="status"><span className="spinner" />Reading document. You will check the details before saving.</div>}

        {tab === "leads" ? (
          <>
          {(selectableScanIssues.length > 0 || rawCandidates.length > 0) && <div className="draft-queue-strip"><span>{rawCandidates.length ? <><strong>{rawCandidates.length} spreadsheet rows</strong> · {rawCandidateIndex} saved · row {rawCandidates[rawCandidateIndex]?.row} ready</> : <><strong>{selectableScanIssues.length} uploaded draft{selectableScanIssues.length === 1 ? "" : "s"}</strong> ready to review</>}</span><button className="button button-secondary" onClick={() => rawCandidates.length || document ? setTab("form") : void reviewDocument(selectableScanIssues.find((item) => item.processing_status === "needs_review") || selectableScanIssues[0])}>{rawCandidates.length ? `Continue row ${rawCandidates[rawCandidateIndex]?.row}` : document ? "Continue current draft" : "Open next draft"}</button></div>}
          <section className="panel">
            <div className="toolbar">
              <div className="search-wrap"><span aria-hidden="true">⌕</span><input value={search} onChange={(event) => { setSearch(event.target.value); setLeadPage(1); }} placeholder="Search company, contact, location..." aria-label="Search leads" /></div>
              <div className="toolbar-actions"><span className="result-count">{leadTotal} {leadTotal === 1 ? "lead" : "leads"}</span>
                <label className="export-count-field"><span>Rows to export</span><input type="number" min="1" step="1" inputMode="numeric" value={exportCount} onChange={(event) => setExportCount(event.target.value)} placeholder="All" aria-label="Number of leads to export" /></label>
                <button className="button button-secondary" onClick={() => void exportLeads()} disabled={!leadTotal && !selected.size}>Export {selected.size ? `${selected.size} selected` : "Excel"}</button>
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
              <td><span className={`verification-pill ${lead.verification_status === "verified" ? "verified" : "needs-review"}`}>{lead.verification_status === "verified" ? "Checked" : lead.document_id ? "Check OCR" : "Check data"}</span></td>
              <td>{lead.owner_name || <span className="muted">—</span>}</td>
              <td>{lead.designation || <span className="muted">—</span>}</td>
              <td className="address-cell">{lead.company_address || <span className="muted">—</span>}</td>
              <td><div className="cell-stack">{(lead.contact_numbers?.length ? lead.contact_numbers : lead.contact_number ? [lead.contact_number] : []).map((number) => <span key={number}>{indianPhone(number)}</span>)}{!(lead.contact_numbers?.length || lead.contact_number) && <span className="muted">—</span>}</div></td>
              <td><div className="cell-stack">{lead.email_addresses?.length ? lead.email_addresses.map((address) => <a key={address} href={`mailto:${address}`}>{address}</a>) : lead.email ? <a href={`mailto:${lead.email}`}>{lead.email}</a> : <span className="muted">—</span>}</div></td>
              <td>{lead.website ? <a href={lead.website.startsWith("http") ? lead.website : `https://${lead.website}`} target="_blank" rel="noreferrer">{lead.website}</a> : <span className="muted">—</span>}</td>
              <td><div className="cell-stack">{lead.facebook_link && <a href={lead.facebook_link} target="_blank" rel="noreferrer">Facebook</a>}{lead.instagram_link && <a href={lead.instagram_link} target="_blank" rel="noreferrer">Instagram</a>}{lead.whatsapp_link && <a href={lead.whatsapp_link} target="_blank" rel="noreferrer">WhatsApp</a>}{!lead.facebook_link && !lead.instagram_link && !lead.whatsapp_link && <span className="muted">—</span>}</div></td>
              <td className="row-actions"><button className="view-action" aria-label={`View details for ${lead.company_name}`} title="View details" onClick={() => setViewingLead(lead)}><svg aria-hidden="true" viewBox="0 0 24 24"><path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/></svg></button><button onClick={() => editLead(lead)}>Edit</button><button className="delete-action" onClick={() => void removeLead(lead)}>Delete</button></td>
            </tr>)}</tbody></table></div> : <div className="empty-state"><div className="empty-icon">↗</div><h2>{search ? "No matching leads" : "Your lead list is ready"}</h2><p>{search ? "Try a different search." : "Scan cards, upload files or a folder, or add a lead by hand."}</p><div className="empty-actions"><button className="button button-secondary" onClick={startManualEntry}>Add manually</button></div></div>}
            <div className="pagination-controls"><span>{leadTotal ? `Showing ${(leadPage - 1) * LEADS_PAGE_SIZE + 1}–${Math.min(leadPage * LEADS_PAGE_SIZE, leadTotal)} of ${leadTotal} leads` : "No leads to show"}</span><div><button className="button button-secondary" disabled={leadPage <= 1} onClick={() => setLeadPage((page) => Math.max(1, page - 1))}>Previous</button><span>Page {leadPage} of {Math.max(1, Math.ceil(leadTotal / LEADS_PAGE_SIZE))}</span><button className="button button-secondary" disabled={leadPage >= Math.ceil(leadTotal / LEADS_PAGE_SIZE)} onClick={() => setLeadPage((page) => page + 1)}>Next</button></div></div>
            </section>
          </>
        ) : (
          <section className={`review-layout${document ? " with-source-preview" : ""}`}>
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
                {busy && <p className="processing-note"><span className="spinner" />Reading document…</p>}
                {(document.extracted_fields.cards?.[activeCardIndex]?.translation_message || document.extracted_fields.translation_message) && <p role="status" className="review-warning">{textValue(document.extracted_fields.cards?.[activeCardIndex]?.translation_message || document.extracted_fields.translation_message)}</p>}
                {!!(document.extracted_fields.cards?.[activeCardIndex]?.original_ocr_text || document.extracted_fields.original_ocr_text) && <details><summary>Original OCR text</summary><pre style={{ whiteSpace: "pre-wrap" }}>{textValue(document.extracted_fields.cards?.[activeCardIndex]?.original_ocr_text || document.extracted_fields.original_ocr_text)}</pre></details>}
                {!busy && document.processing_status !== "uploaded" && document.processing_status !== "processing" && !editingLead && <button type="button" className="text-button" onClick={() => void retryDocument(document)}>Re-scan and translate</button>}
              </div>}
              {!document && !editingLead && <div className="candidate-summary"><span className="eyebrow">{rawCandidates.length ? "RAW DATA REVIEW" : "MANUAL ENTRY"}</span><p>{rawCandidates.length ? `${rawCandidates[rawCandidateIndex]?.source} · Row ${rawCandidates[rawCandidateIndex]?.row} · ${rawCandidateIndex + 1} of ${rawCandidates.length}. Review rows individually or generate the remaining Excel leads at once.` : "Enter company information below, then select Save lead."}</p>{canBulkGenerate && <button type="button" className="button button-primary generate-batch-button" onClick={() => void generateAllExcelLeads()} disabled={busy}>{busy ? "Generating leads…" : `Generate ${rawCandidates.slice(rawCandidateIndex).length} remaining leads in one click`}</button>}</div>}
              <div className="form-heading"><span className="eyebrow">LEAD INFORMATION</span><h2>{editingLead ? editingLead.lead_number : document?.extracted_fields.cards && document.extracted_fields.cards.length > 1 ? `Card ${activeCardIndex + 1} of ${document.extracted_fields.cards.length}` : "Add lead"}</h2><p>{editingLead ? "Correct any details, then save your changes." : "Enter the company information and save the lead."}</p></div>
              <form onSubmit={saveLead}>
                {fields.map((field) => <label className="field" key={field.key}><span>{field.label}{field.key === "company_name" && <b>Required</b>}</span>
                {field.key === "company_address" ? <textarea rows={3} value={form.company_address} onChange={(event) => setForm((current) => ({ ...current, company_address: event.target.value }))} placeholder={field.placeholder} /> : <input value={form[field.key] || ""} onChange={(event) => setForm((current) => ({ ...current, [field.key]: event.target.value }))} placeholder={field.placeholder} required={field.key === "company_name"} />}
                </label>)}
                <label className="field"><span>Contact numbers <small>One per line</small></span><textarea rows={3} value={form.contact_numbers.join("\n")} onChange={(event) => setForm((current) => ({ ...current, contact_numbers: listFromText(event.target.value) }))} placeholder={"+91 98765 43210\n+91 98765 43211"} /></label>
                <label className="field"><span>Email addresses <small>One per line</small></span><textarea rows={3} value={form.email_addresses.join("\n")} onChange={(event) => setForm((current) => ({ ...current, email_addresses: listFromText(event.target.value, /[\n;,]+/) }))} placeholder="name@company.com&#10;sales@company.com" /></label>
                <label className="field"><span>Other text from document <small>Editable OCR text; keeps details that do not fit another field</small></span><textarea className="raw-text-area" rows={6} value={form.raw_text} onChange={(event) => setForm((current) => ({ ...current, raw_text: event.target.value }))} placeholder="All readable text from the source document appears here." /></label>
                {needsSourceReview && <section className="review-check" aria-labelledby="review-check-title">
                  <h3 id="review-check-title">Check details before saving</h3>
                  {!hasOcrText ? <p className="review-warning">Source text is unavailable. Compare every field directly with the original source.</p> : mismatches.length ? <><p className="review-warning">Check these possible mismatches against the original. Correct any errors before saving:</p><ul>{mismatches.map((item) => <li key={item}>{item}</li>)}</ul></> : <p className="review-match">Structured values are consistent with the stored source text. OCR can still misread characters, so compare them with the original source.</p>}
                  <label className="review-confirm"><input type="checkbox" checked={sourceReviewConfirmed} onChange={(event) => setReviewedFingerprint(event.target.checked ? formFingerprint : "")} /><span>I compared the lead details with the available source and corrected any mismatches.</span></label>
                </section>}
                <div className="form-actions">{rawCandidates.length > 0 && !editingLead && <button type="button" className="button button-secondary" onClick={() => setTab("leads")}>Back to leads</button>}<button type="button" className="button button-secondary" onClick={() => { setTab("leads"); setDocument(null); setEditingLead(null); setError(""); setBusy(false); }}>Cancel</button><button className="button button-primary" disabled={busy || Boolean(editingLead?.document_id && !document)}>{busy ? "Working…" : editingLead ? "Save changes" : rawCandidates.length > 0 && rawCandidateIndex + 1 < rawCandidates.length ? "Save & continue" : "Save lead"}</button></div>
              </form>
            </div>
          </section>
        )}
        {cameraOpen && <div className="camera-backdrop" role="presentation"><section className="camera-dialog" role="dialog" aria-modal="true" aria-labelledby="camera-title"><header><div><span className="eyebrow">LIVE SCANNER</span><h2 id="camera-title">Capture a business card</h2></div><button className="details-close" aria-label="Close camera" onClick={() => setCameraOpen(false)}>×</button></header><div className="camera-preview"><video ref={videoRef} playsInline autoPlay muted /><div className="scanner-frame" /></div>{cameraError && <p className="camera-error" role="alert">{cameraError}</p>}<div className="camera-actions"><button className="button button-secondary" onClick={() => setCameraOpen(false)}>Cancel</button><button className="button button-primary" onClick={() => void capturePhoto()}>Capture and scan</button></div></section></div>}
        {viewingLead && <div className="details-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) setViewingLead(null); }}>
          <section className="details-dialog" role="dialog" aria-modal="true" aria-labelledby="details-title">
            <header className="details-heading"><div><span className="eyebrow">LEAD DETAILS</span><h2 id="details-title">{viewingLead.company_name}</h2><p>{viewingLead.lead_number}</p></div><button className="details-close" aria-label="Close details" onClick={() => setViewingLead(null)}>×</button></header>
            <dl className="details-grid">
              <div><dt>Data check</dt><dd>{viewingLead.verification_status === "verified" ? "Checked" : viewingLead.document_id ? "OCR needs review" : "Imported data needs review"}</dd></div>
              <div><dt>Owner / contact</dt><dd>{viewingLead.owner_name || "—"}</dd></div>
              <div><dt>Designation</dt><dd>{viewingLead.designation || "—"}</dd></div>
              <div><dt>Address</dt><dd>{viewingLead.company_address || "—"}</dd></div>
              <div><dt>Contact numbers</dt><dd>{(viewingLead.contact_numbers?.length ? viewingLead.contact_numbers : viewingLead.contact_number ? [viewingLead.contact_number] : []).length ? (viewingLead.contact_numbers?.length ? viewingLead.contact_numbers : [viewingLead.contact_number!]).map((number) => <span className="detail-value" key={number}>{indianPhone(number)}</span>) : "—"}</dd></div>
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
        <footer>Lead Gennie <span>·</span> Scan, save, and organize business contacts</footer>
      </main>
    </div>
  );
}

export default App;
