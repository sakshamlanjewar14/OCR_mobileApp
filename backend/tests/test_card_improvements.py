import json
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image
from backend.app import ocr, paddle_ocr
from backend.app.qr import parse_qr_payload


class CardImprovementsTests(unittest.TestCase):
    def test_indic_constructor_uses_language_code_and_explicit_model(self):
        calls = []
        def factory(**kwargs):
            calls.append(kwargs)
            return object()
        with patch.dict("sys.modules", {"paddleocr": SimpleNamespace(PaddleOCR=factory)}):
            paddle_ocr._create_engine("devanagari")
        self.assertEqual(calls[0]["lang"], "hi")
        self.assertEqual(calls[0]["text_recognition_model_name"], "devanagari_PP-OCRv5_mobile_rec")

    def test_cross_platform_qr_cases(self):
        cases = json.loads((Path(__file__).parent / "fixtures" / "qr_payloads.json").read_text(encoding="utf-8"))
        for case in cases:
            with self.subTest(case=case["payload"]):
                result = parse_qr_payload(case["payload"])
                for key, value in case["expected"].items(): self.assertEqual(result.get(key), value, key)
                for key in case.get("absent", []): self.assertNotIn(key, result)

    def test_paddle_numpy_boxes_and_flat_boxes(self):
        for boxes in (np.asarray([[1, 2, 50, 22]]), np.asarray([[[1, 2], [50, 2], [50, 22], [1, 22]]])):
            lines = paddle_ocr._v3_lines({"res": {"rec_texts": ["Example Industries"], "rec_boxes": boxes}})
            self.assertEqual(lines, [("Example Industries", 20)])

    def test_paddle_legacy_tuple_shape(self):
        item = [[[1, 2], [50, 2], [50, 22], [1, 22]], ("Example Industries", .99)]
        self.assertEqual(paddle_ocr._legacy_lines([[item]]), [("Example Industries", 20)])

    def test_indic_routes_to_reused_paddle_engine(self):
        class Engine:
            def predict(self, pixels):
                np.testing.assert_array_equal(pixels[0, 0], [0, 0, 255])
                return [{"rec_texts": ["उदाहरण उद्योग"], "rec_boxes": [[1, 2, 50, 22]]}]
        with patch.object(paddle_ocr, "get_engine", return_value=Engine()) as get:
            text, _ = paddle_ocr.read(Image.new("RGB", (60, 30), "red"), language="hin+mar")
        get.assert_called_once_with("devanagari")
        self.assertEqual(text, "उदाहरण उद्योग")

    def test_multiple_qr_codes_do_not_merge_different_contacts(self):
        result = ocr._merge_qr_fields({"company_name": "Example Industries", "contact_numbers": []},
                                     ["tel:+919876543210", "tel:+919876543211"])
        self.assertEqual(result["contact_numbers"], [])
        self.assertEqual(len(result["qr_payloads"]), 2)
        self.assertTrue(result["review_warnings"])

    def test_map_and_app_links_are_not_company_websites(self):
        for url in ("https://www.google.com/maps?q=19,72", "https://apps.apple.com/app/id123456789"):
            self.assertIsNone(ocr._parse_fields("Example Industries\n" + url)["website"])

    def test_missing_translation_keeps_original_script_evidence(self):
        original = "उदाहरण उद्योग\nपुणे महाराष्ट्र\nमोबाइल: ९८७६५४३२१०"
        with patch.object(ocr, "translate_document_text", return_value=(None, "unavailable")):
            fields = ocr._fields_from_ocr_text(original)
        self.assertEqual(fields["original_ocr_text"], original)
        for key in ("company_name", "owner_name", "company_address"):
            self.assertIsNone(fields.get(key))
        self.assertTrue(fields["review_warnings"])


if __name__ == "__main__": unittest.main()
