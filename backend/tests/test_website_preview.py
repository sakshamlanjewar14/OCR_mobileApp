import json
import os
import unittest
from unittest.mock import patch
os.environ.setdefault("DATABASE_URL", "sqlite://")
from backend.app import main
from backend.app.schemas import QRWebsitePreviewRequest


class WebsitePreviewTests(unittest.TestCase):
    def test_explicit_organization_metadata_and_contacts(self):
        data = {"@context": "https://schema.org", "@type": "Organization", "name": "Example Industries",
                "telephone": "+919876543210", "email": "hello@example.org",
                "address": {"streetAddress": "Unit 7", "addressLocality": "Example City", "postalCode": "411001"}}
        body = ('<title>Login | Example site</title><script type="application/ld+json">' + json.dumps(data)
                + '</script><a href="https://instagram.com/example">Instagram</a>').encode()
        class Headers:
            def get_content_type(self): return "text/html"
        class Response:
            status = 200
            headers = Headers()
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def read(self, size): return body[:size]
        class Opener:
            def open(self, *_args, **_kwargs): return Response()
        with patch.object(main, "_public_url", return_value="https://example.org"), patch.object(main.urllib.request, "build_opener", return_value=Opener()):
            result = main.qr_website_preview(QRWebsitePreviewRequest(url="https://example.org"), user=object())
        self.assertEqual(result["fields"]["company_name"], "Example Industries")
        self.assertEqual(result["fields"]["company_address"], "Unit 7, Example City, 411001")
        self.assertEqual(result["fields"]["contact_numbers"], ["+919876543210"])
        self.assertTrue(result["field_sources"]["company_name"]["needsReview"])

    def test_page_title_is_not_a_verified_company(self):
        parser = main._WebsitePreviewParser()
        parser.feed('<title>Login | Please sign in</title><script>alert(1)</script>')
        self.assertEqual(parser.json_ld, [])
        self.assertEqual(parser.links, [])
