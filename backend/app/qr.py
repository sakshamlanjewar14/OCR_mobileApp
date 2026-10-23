"""Local QR payload parsing. URLs are classified, never fetched by this module."""
from __future__ import annotations

import re
from urllib.parse import parse_qs, unquote, urlparse


def _safe_url(value: str) -> str | None:
    parsed = urlparse(value.strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        return None
    return value.strip()


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
    result: dict[str, object] = {"source_text": raw, "contacts": [], "emails": [], "urls": []}
    fields: dict[str, object] = {}
    if not raw:
        return result

    if raw.upper().startswith("BEGIN:VCARD"):
        labels = {"FN": "owner_name", "ORG": "company_name", "TITLE": "designation", "ADR": "company_address", "TEL": "contact_numbers", "EMAIL": "email_addresses", "URL": "website"}
        for line in raw.replace("\\n", "\n").splitlines():
            if ":" not in line:
                continue
            name, value = line.split(":", 1)
            key = name.split(";", 1)[0].split(".")[-1].upper()
            target = labels.get(key)
            if target and value.strip():
                decoded = unquote(value.strip()).replace("\\,", ",").replace("\\;", ";")
                if target == "company_address":
                    decoded = ", ".join(part for part in decoded.split(";") if part.strip())
                if target in {"contact_numbers", "email_addresses"}:
                    values = list(fields.get(target, [])); values.append(decoded); fields[target] = values
                else:
                    fields.setdefault(target, decoded)
    elif raw.upper().startswith("MECARD:"):
        body = raw[7:].rstrip(";")
        mecard = re.findall(r"(?:^|;)\s*([A-Z]+):((?:\\.|[^;])*)", body, re.I)
        labels = {"N": "owner_name", "ORG": "company_name", "TEL": "contact_numbers", "EMAIL": "email_addresses", "ADR": "company_address", "URL": "website"}
        for source_key, value in mecard:
            target = labels.get(source_key.upper())
            if not target or not value:
                continue
            decoded = value.replace("\\,", ",")
            if target == "company_address":
                decoded = ", ".join(part for part in decoded.split(",") if part.strip())
            if target in {"contact_numbers", "email_addresses"}:
                fields.setdefault(target, []).append(decoded)
            else:
                fields.setdefault(target, decoded)
    elif raw.lower().startswith("tel:"):
        fields["contact_numbers"] = [unquote(raw[4:].split("?", 1)[0])]
    elif raw.lower().startswith("mailto:"):
        fields["email_addresses"] = [unquote(raw[7:].split("?", 1)[0])]
    elif raw.lower().startswith("geo:"):
        match = re.match(r"geo:([+-]?\d+(?:\.\d+)?),([+-]?\d+(?:\.\d+)?)", raw, re.I)
        if match:
            fields["location_coordinates"] = {"latitude": float(match.group(1)), "longitude": float(match.group(2))}
    else:
        candidate = raw if re.match(r"https?://", raw, re.I) else None
        safe = _safe_url(candidate) if candidate else None
        if safe:
            parsed = urlparse(safe)
            host = (parsed.hostname or "").lower()
            social_key, social_value = _social(safe)
            if social_key:
                fields[social_key] = social_value
            elif host in {"maps.google.com", "www.google.com", "maps.app.goo.gl", "goo.gl", "openstreetmap.org", "www.openstreetmap.org"}:
                query = parse_qs(parsed.query)
                coord_match = re.search(r"@([+-]?\d+(?:\.\d+)?),([+-]?\d+(?:\.\d+)?)", parsed.path + "?" + parsed.query)
                if coord_match:
                    fields["location_coordinates"] = {"latitude": float(coord_match.group(1)), "longitude": float(coord_match.group(2))}
                elif query.get("q"):
                    q = query["q"][0]
                    geo = re.fullmatch(r"\s*([+-]?\d+(?:\.\d+)?),\s*([+-]?\d+(?:\.\d+)?)\s*", q)
                    if geo:
                        fields["location_coordinates"] = {"latitude": float(geo.group(1)), "longitude": float(geo.group(2))}
                    else:
                        fields["map_url"] = safe
                else:
                    fields["map_url"] = safe
            elif host == "singleqr.in" or host.endswith(".singleqr.in") or host in {"bit.ly", "tinyurl.com", "t.co", "qrco.de"}:
                # Preserve third-party QR redirect URLs as source data without
                # mislabeling them as the card owner's website.
                fields["qr_redirect_url"] = safe
            else:
                fields["website"] = safe
            result["urls"] = [safe]
        elif not re.match(r"^[a-z][a-z0-9+.-]*:", raw, re.I) and "@" in raw and re.search(r"\.[A-Za-z]{2,}", raw):
            fields["email_addresses"] = [raw]

    for key, value in fields.items():
        if key == "contact_numbers": result["contacts"] = value
        elif key == "email_addresses": result["emails"] = value
        else: result[key] = value
    # Retain unsupported payloads intact so the reviewer can inspect them.
    result["unmapped"] = not bool(fields)
    return result
