export type ClientExtractedFields = {
  company_name?: string | null;
  owner_name?: string | null;
  designation?: string | null;
  business_description?: string | null;
  company_address?: string | null;
  website?: string | null;
  facebook_link?: string | null;
  instagram_link?: string | null;
  whatsapp_link?: string | null;
  contact_numbers?: string[];
  email_addresses?: string[];
  raw_text: string;
  original_ocr_text: string;
  qr_payloads?: string[];
  field_sources?: Record<string, { source_type: "ocr" | "qr" | "website"; source_text: string; confidence: "high" | "medium" | "low" }>;
};

type TesseractApi = { createWorker: (langs: string, oem?: number, options?: Record<string, unknown>) => Promise<{ recognize: (image: Blob, options?: { logger?: (message: { status?: string; progress?: number }) => void }) => Promise<{ data: { text: string } }>; terminate: () => Promise<void> }> };
let tesseractPromise: Promise<TesseractApi> | null = null;
const workerPromises = new Map<string, Promise<Awaited<ReturnType<TesseractApi["createWorker"]>> | null>>();

function loadTesseract(): Promise<TesseractApi> {
  if (tesseractPromise) return tesseractPromise;
  tesseractPromise = new Promise((resolve, reject) => {
    const existing = (window as unknown as { Tesseract?: TesseractApi }).Tesseract;
    if (existing) return resolve(existing);
    const script = document.createElement("script");
    script.src = "/ocr/tesseract.min.js";
    script.onload = () => (window as unknown as { Tesseract?: TesseractApi }).Tesseract ? resolve((window as unknown as { Tesseract: TesseractApi }).Tesseract) : reject(new Error("Browser OCR runtime is unavailable."));
    script.onerror = () => reject(new Error("Browser OCR runtime could not be loaded."));
    document.head.appendChild(script);
  });
  return tesseractPromise;
}

async function getWorker(languages: string, onProgress?: (message: string) => void) {
  let workerPromise = workerPromises.get(languages);
  if (!workerPromise) {
    onProgress?.(`Loading ${languages === "eng" ? "English" : "multilingual"} OCR model…`);
    workerPromise = withTimeout(loadTesseract().then((api) => api.createWorker(languages, 1, {
      workerPath: "/ocr/worker.min.js",
      corePath: "/ocr",
      langPath: "/ocr",
      cachePath: "lead-gennie-ocr",
      logger: (message: { status?: string; progress?: number }) => onProgress?.(`${message.status || "Reading card"}${typeof message.progress === "number" ? ` ${Math.round(message.progress * 100)}%` : ""}`),
    })), 12_000, "Browser OCR model could not start. Retry locally or choose server OCR.").then((worker) => { onProgress?.("OCR model ready"); return worker; });
    workerPromises.set(languages, workerPromise);
  }
  try { return await workerPromise; } catch (error) { workerPromises.delete(languages); throw error; }
}

function cleanLines(text: string): string[] {
  return text.split(/\r?\n/).map((line) => line.replace(/[^\x00-\x7F]/g, " ").replace(/\s+/g, " ").trim()).filter((line) => {
    const letters = (line.match(/[A-Za-z]/g) || []).length;
    const digits = (line.match(/\d/g) || []).length;
    return letters >= 2 || /@|https?:\/\/|www\.|\+?\d[\d .()-]{6,}/i.test(line) || (digits >= 5 && letters >= 1);
  });
}

function parseText(text: string): ClientExtractedFields {
  const lines = cleanLines(text);
  const emails = [...text.matchAll(/[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/gi)].map((match) => match[0].toLowerCase());
  const websites = [...text.matchAll(/(?:https?:\/\/|www\.)[^\s<>]+/gi)].map((match) => match[0].replace(/[),.;]+$/, ""));
  const phones = [...text.matchAll(/(?<!\d)\+?\d(?:[\s().-]*\d){6,14}(?!\d)/g)].map((match) => match[0].replace(/\s+/g, " ").trim()).filter((value, index, values) => values.findIndex((item) => item.replace(/\D/g, "").slice(-10) === value.replace(/\D/g, "").slice(-10)) === index);
  const companyIndex = lines.findIndex((line) => /\b(?:pvt\.?|private|ltd\.?|limited|llc|inc\.?|corporation|corp\.?|industr(?:y|ies)|technolog(?:y|ies)|services|group)\b/i.test(line));
  const address = lines.filter((line) => /\b(?:plot|road|street|near|phase|pune|india|maharashtra|address|tal\.?|dist\.?|pin|floor|nagar|chakan)\b/i.test(line)).join(" ");
  const social = (name: string) => websites.find((url) => new RegExp(`${name}\\.com`, "i").test(url)) || lines.find((line) => new RegExp(`${name}\\.com`, "i").test(line)) || "";
  const company = companyIndex >= 0 ? lines[companyIndex] : lines.find((line) => /^[A-Z][A-Z .&'-]{4,}$/.test(line)) || "";
  const owner = lines.find((line, index) => index !== companyIndex && /^[A-Z][A-Za-z.'-]+(?:\s+[A-Z][A-Za-z.'-]+){1,3}$/.test(line)) || "";
  const designation = lines.find((line) => /\b(?:director|manager|founder|owner|engineer|executive|ceo|sales|marketing)\b/i.test(line)) || "";
  const field_sources: ClientExtractedFields["field_sources"] = {};
  for (const [key, value] of Object.entries({ company_name: company, owner_name: owner, designation, company_address: address, website: websites.find((url) => !/facebook|instagram|linkedin/i.test(url)) || "", facebook_link: social("facebook"), instagram_link: social("instagram") })) {
    if (value) field_sources[key] = { source_type: "ocr", source_text: value, confidence: key === "company_address" ? "medium" : "high" };
  }
  if (phones.length) field_sources.contact_numbers = { source_type: "ocr", source_text: phones.join("; "), confidence: "high" };
  if (emails.length) field_sources.email_addresses = { source_type: "ocr", source_text: emails.join("; "), confidence: "high" };
  return { company_name: company || null, owner_name: owner || null, designation: designation || null, company_address: address || null, website: websites.find((url) => !/facebook|instagram|linkedin/i.test(url)) || null, facebook_link: social("facebook") || null, instagram_link: social("instagram") || null, whatsapp_link: null, contact_numbers: phones, email_addresses: emails, raw_text: lines.join("\n"), original_ocr_text: text, field_sources };
}

export async function recognizeCard(file: Blob, onProgress?: (message: string) => void): Promise<ClientExtractedFields> {
  const worker = await getWorker("eng", onProgress);
  if (!worker) throw new Error("Browser OCR worker is unavailable.");
  const result = await withTimeout(worker.recognize(file, { logger: (message) => onProgress?.(`${message.status || "Reading card"}${typeof message.progress === "number" ? ` ${Math.round(message.progress * 100)}%` : ""}`) }), 45_000, "Browser OCR timed out. Retry locally or choose server OCR.");
  let fields = parseText(result.data.text);
  const needsIndicModel = !fields.company_name && !fields.owner_name && !fields.designation && !fields.company_address && (!/[A-Za-z]{3,}/.test(fields.raw_text) || fields.raw_text.length < 40);
  if (needsIndicModel) {
    const multilingualWorker = await getWorker("eng+hin+mar", onProgress);
    if (multilingualWorker) {
      const multilingualResult = await multilingualWorker.recognize(file, { logger: (message) => onProgress?.(`${message.status || "Reading card"}${typeof message.progress === "number" ? ` ${Math.round(message.progress * 100)}%` : ""}`) });
      const multilingualFields = parseText(multilingualResult.data.text);
      if (multilingualFields.raw_text.length > fields.raw_text.length || multilingualFields.contact_numbers?.length || multilingualFields.email_addresses?.length) fields = multilingualFields;
    }
  }
  const qrPayloads = await detectQrPayloads(file).catch(() => []);
  for (const payload of qrPayloads) {
    const qr = parseQrLocally(payload);
    for (const key of ["company_name", "owner_name", "designation", "company_address", "website", "facebook_link", "instagram_link", "whatsapp_link"] as const) {
      if (qr[key] && !fields[key]) fields[key] = qr[key] as string;
    }
    fields.contact_numbers = [...new Set([...(fields.contact_numbers || []), ...(qr.contact_numbers || [])])];
    fields.email_addresses = [...new Set([...(fields.email_addresses || []), ...(qr.email_addresses || [])])];
    fields.field_sources = { ...(fields.field_sources || {}), ...(qr.field_sources || {}) };
  }
  fields.qr_payloads = qrPayloads;
  return fields;
}

/** Warm the fast English model while the user is on the leads screen. */
async function detectQrPayloads(file: Blob): Promise<string[]> {
  const BarcodeDetectorCtor = (window as unknown as { BarcodeDetector?: new (options?: { formats?: string[] }) => { detect: (source: ImageBitmap) => Promise<Array<{ rawValue?: string }>> } }).BarcodeDetector;
  if (!BarcodeDetectorCtor || typeof createImageBitmap !== "function") return [];
  const bitmap = await createImageBitmap(file);
  try {
    const detector = new BarcodeDetectorCtor({ formats: ["qr_code"] });
    const results = await withTimeout(detector.detect(bitmap), 2_000, "QR detection timed out");
    return [...new Set(results.map((item) => item.rawValue?.trim()).filter((item): item is string => Boolean(item)))];
  } finally {
    bitmap.close();
  }
}

async function withTimeout<T>(promise: Promise<T>, timeoutMs: number, message: string): Promise<T> {
  let timer: number | undefined;
  try {
    return await Promise.race([
      promise,
      new Promise<T>((_, reject) => { timer = window.setTimeout(() => reject(new Error(message)), timeoutMs); }),
    ]);
  } finally {
    if (timer !== undefined) window.clearTimeout(timer);
  }
}

export function parseQrLocally(payload: string): Partial<ClientExtractedFields> & { source_text: string; website?: string } {
  const value = payload.trim();
  const result: Partial<ClientExtractedFields> & { source_text: string; website?: string } = { source_text: value };
  const emails = [...value.matchAll(/[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/gi)].map((match) => match[0].toLowerCase());
  const phones = [...value.matchAll(/(?<!\d)\+?\d(?:[\s().-]*\d){6,14}(?!\d)/g)].map((match) => match[0].trim());
  const urls = [...value.matchAll(/https?:\/\/[^\s;,]+/gi)].map((match) => match[0].replace(/[),.;]+$/, ""));
  const lines = value.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  const get = (key: string) => lines.find((line) => new RegExp(`^${key}[:;]`, "i").test(line))?.replace(new RegExp(`^${key}[:;]`, "i"), "").split(";")[0].trim() || "";
  result.email_addresses = emails;
  result.contact_numbers = phones;
  result.website = urls[0] || undefined;
  if (/^BEGIN:VCARD/i.test(value)) {
    result.owner_name = get("FN") || get("N") || null;
    result.company_name = get("ORG") || null;
    result.company_address = get("ADR")?.replace(/;/g, ", ") || null;
    result.designation = get("TITLE") || null;
    result.website = get("URL") || result.website;
  } else if (/^MECARD:/i.test(value)) {
    result.owner_name = get("N") || null;
    result.company_name = get("ORG") || null;
    result.company_address = get("ADR") || null;
    result.website = get("URL") || result.website;
  }
  result.raw_text = lines.join("\n");
  result.field_sources = {};
  for (const key of ["company_name", "owner_name", "designation", "company_address", "website"] as const) {
    const value = result[key];
    if (typeof value === "string" && value) result.field_sources[key] = { source_type: "qr", source_text: value, confidence: "high" };
  }
  if (emails.length) result.field_sources.email_addresses = { source_type: "qr", source_text: emails.join("; "), confidence: "high" };
  if (phones.length) result.field_sources.contact_numbers = { source_type: "qr", source_text: phones.join("; "), confidence: "high" };
  return result;
}
