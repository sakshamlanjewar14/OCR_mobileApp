"""Explicit, coordinate-only reverse geocoding for configured Nominatim servers."""
from __future__ import annotations

import json
import os
import threading
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

_cache: dict[tuple[float, float], dict[str, str]] = {}
_lock = threading.Lock()
_last_request = 0.0


def reverse_geocode(latitude: float, longitude: float) -> dict[str, str]:
    global _last_request
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        raise ValueError("Coordinates are outside the valid range.")
    endpoint = os.getenv("NOMINATIM_URL", "").strip()
    if not endpoint:
        raise RuntimeError("Reverse geocoding is not configured. Set NOMINATIM_URL to a Nominatim endpoint you are permitted to use.")
    user_agent = os.getenv("NOMINATIM_USER_AGENT", "").strip()
    if not user_agent:
        raise RuntimeError("Set NOMINATIM_USER_AGENT with an application identifier and operator contact before using geocoding.")
    cache_key = (round(latitude, 6), round(longitude, 6))
    with _lock:
        if cache_key in _cache:
            return _cache[cache_key]
        wait = 1.0 - (time.monotonic() - _last_request)
        if wait > 0:
            time.sleep(wait)
        query = urlencode({"format": "jsonv2", "lat": latitude, "lon": longitude})
        request = Request(f"{endpoint.rstrip('/')}?{query}", headers={
            "User-Agent": user_agent,
            "Accept": "application/json",
        })
        _last_request = time.monotonic()
        with urlopen(request, timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))
        address = str(data.get("display_name", "")).strip()
        if not address:
            raise LookupError("No address was found for these coordinates.")
        result = {"address": address, "attribution": "© OpenStreetMap contributors"}
        _cache[cache_key] = result
        return result
