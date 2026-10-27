"""Local QR payload parsing. URLs are classified, never fetched by this module."""
from __future__ import annotations

import re
import unicodedata
from urllib.parse import parse_qs, unquote, urlparse


def _safe_url(value: str) -> str | None:
    try:
        parsed = urlparse(value.strip())
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        return None
    return value.strip()


def _unescape(value: str) -> str:
    return re.sub(r"\\([nN,;\\:])", lambda match: "\n" if match[1].lower() == "n" else match[1], value).strip()


def _parts(value: str, delimiter: str = ";") -> list[str]:
    return [_unescape(part) for part in re.split(r"(?<!\\)" + re.escape(delimiter), value)]


def _coordinates(latitude: str, longitude: str) -> dict[str, float] | None:
    lat, lon = float(latitude), float(longitude)
    return {"latitude": lat, "longitude": lon} if abs(lat) <= 90 and abs(lon) <= 180 else None


def _social(url: str) -> tuple[str | None, str | None]:
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    if host in {"facebook.com", "m.facebook.com", "fb.com"}:
        return "facebook_link", url
    if host in {"instagram.com", "www.instagram.com"}:
        return "instagram_link", url
    if host in {"wa.me", "api.whatsapp.com", "whatsapp.com", "chat.whatsapp.com"}:
        return "whatsapp_link", url
    return None, None


def parse_qr_payload(payload: str) -> dict[str, object]:
    raw = payload.strip()
    result: dict[str, object] = {"source_text": raw, "contacts": [], "emails": [], "urls": [], "kind": "text"}
    fields: dict[str, object] = {}
    if not raw:
        return result

    if raw.upper().startswith("BEGIN:VCARD"):
        result["kind"] = "vcard"
        labels = {"FN": "owner_name", "ORG": "company_name", "TITLE": "designation", "ADR": "company_address", "TEL": "contact_numbers", "EMAIL": "email_addresses", "URL": "website"}
        for line in re.sub(r"\r?\n[ \t]", "", raw).splitlines():
            if ":" not in line:
                continue
            name, value = line.split(":", 1)
            key = name.split(";", 1)[0].split(".")[-1].upper()
            if key == "N" and not fields.get("owner_name"):
                fields["owner_name"] = " ".join(part for part in reversed(_parts(value)) if part)
            target = labels.get(key)
            if target and value.strip():
                decoded = _unescape(value.strip())
                if target == "company_address":
                    decoded = ", ".join(part for part in _parts(value) if part)
                if key == "ORG":
                    decoded = " ".join(part for part in _parts(value) if part)
                if target in {"contact_numbers", "email_addresses"}:
                    values = list(fields.get(target, [])); values.append(decoded); fields[target] = values
                else:
                    if key == "FN": fields[target] = decoded
                    else: fields.setdefault(target, decoded)
    elif raw.upper().startswith("MECARD:"):
        result["kind"] = "mecard"
        body = raw[7:].rstrip(";")
        mecard = re.findall(r"(?:^|;)\s*([A-Z]+):((?:\\.|[^;])*)", body, re.I)
        labels = {"N": "owner_name", "ORG": "company_name", "TEL": "contact_numbers", "EMAIL": "email_addresses", "ADR": "company_address", "URL": "website"}
        for source_key, value in mecard:
            target = labels.get(source_key.upper())
            if not target or not value:
                continue
            decoded = _unescape(value)
            if source_key.upper() == "N":
                decoded = " ".join(part for part in reversed(_parts(value, ",")) if part)
            if target == "company_address":
                decoded = ", ".join(part for part in decoded.split(",") if part.strip())
            if target in {"contact_numbers", "email_addresses"}:
                fields.setdefault(target, []).append(decoded)
            else:
                fields.setdefault(target, decoded)
    elif raw.lower().startswith("tel:"):
        result["kind"] = "phone"
        fields["contact_numbers"] = [unquote(raw[4:].split("?", 1)[0])]
    elif raw.lower().startswith("mailto:"):
        result["kind"] = "email"
        fields["email_addresses"] = unquote(raw[7:].split("?", 1)[0]).split(",")
    elif raw.lower().startswith("geo:"):
        result["kind"] = "location"
        match = re.match(r"geo:([+-]?\d+(?:\.\d+)?),([+-]?\d+(?:\.\d+)?)", raw, re.I)
        if match:
            coord = _coordinates(match[1], match[2])
            if coord: fields["location_coordinates"] = coord
    else:
        candidate = raw if re.match(r"https?://", raw, re.I) else None
        safe = _safe_url(candidate) if candidate else None
        if safe:
            parsed = urlparse(safe)
            host = (parsed.hostname or "").lower()
            social_key, social_value = _social(safe)
            if social_key:
                fields[social_key] = social_value
                result["kind"] = social_key.removesuffix("_link")
                if social_key == "whatsapp_link":
                    phone = parsed.path.lstrip("/") if host == "wa.me" else parse_qs(parsed.query).get("phone", [""])[0]
                    if re.fullmatch(r"\+?\d{7,15}", phone): fields["contact_numbers"] = ["+" + phone.lstrip("+")]
            elif host in {"maps.google.com", "maps.app.goo.gl", "openstreetmap.org", "www.openstreetmap.org"} or (re.search(r"(^|\.)google\.[a-z.]+$", host) and parsed.path.startswith("/maps")) or (host == "goo.gl" and parsed.path.startswith("/maps")):
                result["kind"] = "location"
                fields["map_url"] = safe
                query = parse_qs(parsed.query)
                coord_match = re.search(r"@([+-]?\d+(?:\.\d+)?),([+-]?\d+(?:\.\d+)?)", parsed.path + "?" + parsed.query)
                if coord_match:
                    coord = _coordinates(coord_match[1], coord_match[2])
                    if coord: fields["location_coordinates"] = coord
                elif query.get("q"):
                    q = query["q"][0]
                    geo = re.fullmatch(r"\s*([+-]?\d+(?:\.\d+)?),\s*([+-]?\d+(?:\.\d+)?)\s*", q)
                    if geo:
                        coord = _coordinates(geo[1], geo[2])
                        if coord: fields["location_coordinates"] = coord
                    else:
                        fields["map_url"] = safe
                else:
                    fields["map_url"] = safe
            elif host in {"apps.apple.com", "itunes.apple.com"} or (host == "play.google.com" and parsed.path.startswith("/store")):
                fields["app_url"] = safe
                result["kind"] = "app"
            elif host == "singleqr.in" or host.endswith(".singleqr.in") or host in {"bit.ly", "tinyurl.com", "t.co", "qrco.de"}:
                # Preserve third-party QR redirect URLs as source data without
                # mislabeling them as the card owner's website.
                fields["qr_redirect_url"] = safe
                result["kind"] = "redirect"
            else:
                fields["website"] = safe
                result["kind"] = "website"
            result["urls"] = [safe]
        elif not re.match(r"^[a-z][a-z0-9+.-]*:", raw, re.I):
            fields["email_addresses"] = re.findall(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", raw, re.I)
            fields["contact_numbers"] = re.findall(r"(?<!\d)\+?\d(?:[ \t().-]*\d){6,14}(?!\d)", raw)

    # Classify URLs inside contact records too; app/map links are not websites.
    if fields.get("website") and result["kind"] in {"vcard", "mecard"}:
        nested = parse_qr_payload(str(fields.pop("website")))
        for key in ("website", "map_url", "app_url", "qr_redirect_url", "instagram_link", "facebook_link", "whatsapp_link"):
            if nested.get(key): fields[key] = nested[key]
        result["urls"] = nested["urls"]
    phones = []
    phone_keys = set()
    for phone in fields.pop("contact_numbers", []):
        phone = "".join(str(unicodedata.decimal(c)) if c.isdecimal() else c for c in str(phone)).removeprefix("tel:").strip()
        digit_key = re.sub(r"\D", "", phone)
        if re.fullmatch(r"\+?[\d ().-]+", phone) and 7 <= len(digit_key) <= 15:
            if len(digit_key) == 10 and digit_key[0] in "6789": digit_key = "91" + digit_key
            if digit_key not in phone_keys:
                phone_keys.add(digit_key)
                phones.append(phone)
    if phones: fields["contact_numbers"] = phones
    emails = list(dict.fromkeys(email.strip() for email in fields.pop("email_addresses", []) if re.fullmatch(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", email.strip(), re.I)))
    if emails: fields["email_addresses"] = emails
    for key in ("company_name", "owner_name", "designation", "company_address"):
        if re.search(r"[\u0900-\u097f]", str(fields.get(key) or "")):
            fields.pop(key)
            result.setdefault("review_warnings", []).append(f"{key.replace('_', ' ')}: review the original QR text and enter a verified English value.")

    for key, value in fields.items():
        if key == "contact_numbers": result["contacts"] = value
        elif key == "email_addresses": result["emails"] = value
        else: result[key] = value
    # Retain unsupported payloads intact so the reviewer can inspect them.
    result["unmapped"] = not bool(fields)
    return result
