/** Classify QR content without fetching links or interpreting HTML. */
export type ParsedQr = {
  source_text: string; raw_text: string; kind: string; urls: string[];
  contact_numbers: string[]; email_addresses: string[]; contacts: string[]; emails: string[];
  company_name?: string; owner_name?: string; designation?: string; company_address?: string;
  website?: string; facebook_link?: string; instagram_link?: string; whatsapp_link?: string;
  map_url?: string; app_url?: string; qr_redirect_url?: string;
  location_coordinates?: { latitude: number; longitude: number };
  review_warnings?: string[];
  field_sources: Record<string, { source_type: "qr"; source_text: string; confidence: "high"; needsReview: true }>;
};
const digits = (value: string) => value.replace(/[०-९]/g, char => String(char.charCodeAt(0) - 0x966));
const unescape = (value: string) => value.replace(/\\([nN,;\\:])/g, (_, char: string) => /n/i.test(char) ? "\n" : char).trim();
const split = (value: string, delimiter: string) => value.split(new RegExp(`(?<!\\\\)${delimiter}`)).map(unescape);
const decode = (value: string) => { try { return decodeURIComponent(value); } catch { return value; } };
export function parseQrPayload(payload: string): ParsedQr {
  const raw = payload.trim();
  const result: ParsedQr = { source_text: raw, raw_text: raw, kind: "text", urls: [], contact_numbers: [], email_addresses: [], contacts: [], emails: [], field_sources: {} };
  const addPhone = (input: string) => {
    const value = digits(decode(input).replace(/^tel:/i, "")).trim();
    if (!/^\+?[\d\s().-]+$/.test(value)) return;
    let key = value.replace(/\D/g, "");
    if (key.length < 7 || key.length > 15) return;
    if (key.length === 10 && /^[6-9]/.test(key)) key = `91${key}`;
    if (!result.contact_numbers.some(item => {
      let other = item.replace(/\D/g, "");
      if (other.length === 10 && /^[6-9]/.test(other)) other = `91${other}`;
      return other === key;
    })) result.contact_numbers.push(value);
  };
  const addEmail = (input: string) => {
    const value = decode(input.replace(/^mailto:/i, "")).trim();
    if (/^[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}$/i.test(value) && !result.email_addresses.includes(value)) result.email_addresses.push(value);
  };
  const coordinates = (lat: string, lon: string) => {
    const latitude = Number(lat), longitude = Number(lon);
    if (Number.isFinite(latitude) && Number.isFinite(longitude) && Math.abs(latitude) <= 90 && Math.abs(longitude) <= 180) result.location_coordinates = { latitude, longitude };
  };
  const addUrl = (value: string) => {
    let url: URL;
    try { url = new URL(value); } catch { return; }
    if (!/^https?:$/.test(url.protocol) || !url.hostname || url.username || url.password) return;
    if (!result.urls.includes(value)) result.urls.push(value);
    const host = url.hostname.toLowerCase().replace(/^www\./, "");
    if (["instagram.com", "m.instagram.com"].includes(host)) { result.instagram_link = value; result.kind = "instagram"; }
    else if (["facebook.com", "m.facebook.com", "fb.com"].includes(host)) { result.facebook_link = value; result.kind = "facebook"; }
    else if (["wa.me", "api.whatsapp.com", "whatsapp.com", "chat.whatsapp.com"].includes(host)) {
      result.whatsapp_link = value; result.kind = "whatsapp";
      const phone = host === "wa.me" ? url.pathname.slice(1) : url.searchParams.get("phone");
      if (phone && /^\+?\d{7,15}$/.test(phone)) addPhone(`+${phone.replace(/^\+/, "")}`);
    } else if (host === "maps.app.goo.gl" || host === "maps.google.com" || host === "openstreetmap.org" || (/(^|\.)google\.[a-z.]+$/.test(host) && /^\/maps(?:\/|$)/.test(url.pathname)) || (host === "goo.gl" && /^\/maps/.test(url.pathname))) {
      result.map_url = value; result.kind = "location";
      const match = (decode(url.pathname) + " " + (url.searchParams.get("q") || url.searchParams.get("query") || "")).match(/@?([+-]?\d+(?:\.\d+)?),\s*([+-]?\d+(?:\.\d+)?)/);
      if (match) coordinates(match[1], match[2]);
    } else if (["apps.apple.com", "itunes.apple.com"].includes(host) || (host === "play.google.com" && url.pathname.startsWith("/store"))) { result.app_url = value; result.kind = "app"; }
    else if (host === "singleqr.in" || host.endsWith(".singleqr.in") || ["bit.ly", "tinyurl.com", "t.co", "qrco.de"].includes(host)) { result.qr_redirect_url = value; result.kind = "redirect"; }
    else { result.website ||= value; result.kind = "website"; }
  };
  const assign = (key: string, value: string) => {
    if (!value.trim()) return;
    if (key === "TEL") addPhone(unescape(value));
    else if (key === "EMAIL") addEmail(unescape(value));
    else if (key === "URL") addUrl(unescape(value));
    else if (key === "FN") result.owner_name = unescape(value);
    else if (key === "N" && !result.owner_name) result.owner_name = split(value, ";").reverse().filter(Boolean).join(" ");
    else if (key === "ORG") result.company_name ||= split(value, ";").filter(Boolean).join(" ");
    else if (key === "TITLE") result.designation ||= unescape(value);
    else if (key === "ADR") result.company_address ||= split(value, ";").filter(Boolean).join(", ");
  };
  if (/^BEGIN:VCARD/i.test(raw)) {
    for (const line of raw.replace(/\r?\n[ \t]/g, "").split(/\r?\n/)) {
      const match = line.match(/^(?:[\w-]+\.)?([\w-]+)(?:;[^:]*)?:(.*)$/);
      if (match) assign(match[1].toUpperCase(), match[2]);
    }
    result.kind = "vcard";
  } else if (/^MECARD:/i.test(raw)) {
    for (const entry of raw.slice(7).matchAll(/(?:^|;)\s*([A-Z]+):((?:\\.|[^;])*)/gi)) {
      if (entry[1].toUpperCase() === "N") result.owner_name = split(entry[2], ",").reverse().join(" ");
      else assign(entry[1].toUpperCase(), entry[2]);
    }
    result.kind = "mecard";
  } else if (/^tel:/i.test(raw)) { addPhone(raw.split("?")[0]); result.kind = "phone"; }
  else if (/^mailto:/i.test(raw)) { raw.slice(7).split("?")[0].split(",").forEach(addEmail); result.kind = "email"; }
  else if (/^geo:/i.test(raw)) {
    const match = raw.match(/^geo:([+-]?\d+(?:\.\d+)?),([+-]?\d+(?:\.\d+)?)/i);
    if (match) coordinates(match[1], match[2]);
    result.kind = "location";
  } else if (/^https?:\/\/\S+$/i.test(raw)) addUrl(raw);
  else if (!/^[a-z][a-z0-9+.-]*:/i.test(raw)) {
    for (const match of raw.matchAll(/[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/gi)) addEmail(match[0]);
    for (const match of digits(raw).matchAll(/(?<!\d)\+?\d(?:[ \t().-]*\d){6,14}(?!\d)/g)) addPhone(match[0]);
    for (const match of raw.matchAll(/https?:\/\/[^\s<>]+/gi)) addUrl(match[0]);
  }
  result.contacts = result.contact_numbers;
  result.emails = result.email_addresses;
  for (const key of ["company_name", "owner_name", "designation", "company_address"] as const) {
    if (/[\u0900-\u097f]/.test(result[key] || "")) {
      delete result[key];
      result.review_warnings = [...(result.review_warnings || []), `${key.replace(/_/g, " ")}: review the original QR text and enter a verified English value.`];
    }
  }
  for (const key of ["company_name", "owner_name", "designation", "company_address", "website", "facebook_link", "instagram_link", "whatsapp_link", "contact_numbers", "email_addresses"] as const) {
    const value = result[key];
    if (Array.isArray(value) ? value.length : value) result.field_sources[key] = { source_type: "qr", source_text: raw, confidence: "high", needsReview: true };
  }
  return result;
}
