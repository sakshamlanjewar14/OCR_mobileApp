import io
import json
import os
import unittest
from unittest.mock import patch

from backend.app import geocoding
from backend.app.qr import parse_qr_payload


class QRParsingTests(unittest.TestCase):
    def test_vcard_maps_named_fields_and_separate_contacts(self):
        parsed = parse_qr_payload("BEGIN:VCARD\nFN:Jane Doe\nORG:Acme Solutions Pvt Ltd\nTITLE:Director\nTEL;TYPE=CELL:+919876543210\nTEL;TYPE=WORK:+912241310098\nEMAIL:one@acme.example\nURL:https://acme.example\nEND:VCARD")
        self.assertEqual(parsed["owner_name"], "Jane Doe")
        self.assertEqual(parsed["company_name"], "Acme Solutions Pvt Ltd")
        self.assertEqual(parsed["contacts"], ["+919876543210", "+912241310098"])
        self.assertEqual(parsed["emails"], ["one@acme.example"])

    def test_phone_email_and_coordinates_parse_without_external_lookup(self):
        self.assertEqual(parse_qr_payload("tel:+919876543210")["contacts"], ["+919876543210"])
        self.assertEqual(parse_qr_payload("mailto:team@acme.example")["emails"], ["team@acme.example"])
        parsed = parse_qr_payload("geo:19.076,72.8777")
        self.assertEqual(parsed["location_coordinates"], {"latitude": 19.076, "longitude": 72.8777})

    def test_social_links_are_separated_and_map_urls_are_not_websites(self):
        self.assertIn("facebook_link", parse_qr_payload("https://www.facebook.com/acme"))
        parsed = parse_qr_payload("https://maps.google.com/?q=19.076,72.8777")
        self.assertNotIn("website", parsed)
        self.assertEqual(parsed["location_coordinates"]["latitude"], 19.076)

    def test_unsafe_and_unknown_payloads_are_not_opened_or_guessed(self):
        parsed = parse_qr_payload("javascript:alert(1)")
        self.assertTrue(parsed["unmapped"])
        self.assertEqual(parsed["urls"], [])
        self.assertEqual(parsed["source_text"], "javascript:alert(1)")


class ReverseGeocodingTests(unittest.TestCase):
    def test_user_triggered_lookup_sends_only_coordinates_and_caches_results(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def read(self): return json.dumps({"display_name": "Synthetic address"}).encode()

        geocoding._cache.clear()
        geocoding._last_request = 0
        with patch.dict(os.environ, {"NOMINATIM_URL": "https://geocoder.example/reverse", "NOMINATIM_USER_AGENT": "LeadGennieTest/1.0 test@example.test"}), patch.object(geocoding, "urlopen", return_value=Response()) as fetch:
            result = geocoding.reverse_geocode(19.076, 72.8777)
            second = geocoding.reverse_geocode(19.076, 72.8777)
        self.assertEqual(result, second)
        self.assertEqual(result["attribution"], "© OpenStreetMap contributors")
        self.assertEqual(fetch.call_count, 1)
        request = fetch.call_args.args[0]
        self.assertIn("lat=19.076", request.full_url)
        self.assertNotIn("contact", request.full_url)

    def test_lookup_requires_configuration_and_valid_coordinates(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError):
                geocoding.reverse_geocode(1, 2)
        with self.assertRaises(ValueError):
            geocoding.reverse_geocode(91, 2)

    def test_vcard_mecard_and_unsafe_url_parsing(self):
        parsed = parse_qr_payload("MECARD:N:Jane Doe;TEL:+919876543210;TEL:+919876543211;EMAIL:jane@example.test;;")
        self.assertEqual(parsed["contacts"], ["+919876543210", "+919876543211"])
        self.assertEqual(parsed["emails"], ["jane@example.test"])
        self.assertTrue(parse_qr_payload("https://user:pass@example.test/path")["unmapped"])

    def test_model_missing_flags_translation_without_losing_original_text(self):
        from backend.app import ocr
        original = "माझे व्यवसाय कार्ड"
        with patch.object(ocr, "translate_document_text", return_value=(None, "unavailable")):
            fields = ocr._fields_from_ocr_text(original)
        self.assertEqual(fields["original_ocr_text"], original)
        self.assertEqual(fields["translation_status"], "unavailable")
        self.assertIn("English translation is unavailable", fields["translation_message"])


if __name__ == "__main__":
    unittest.main()
