import re
import unittest
from unittest.mock import patch

from backend.app import translation, ocr


class EnglishReviewTests(unittest.TestCase):
    def test_address_names_are_transliterated_without_model_guessing(self):
        with patch.object(translation, '_translate_parts', return_value=[]) as engine:
            output, status = translation.translate_document_text('पत्ता: पुणे महाराष्ट्र 411001\n')
        self.assertEqual(status, 'translated')
        self.assertIn('411001', output)
        self.assertIn(translation.latin_name('पुणे'), output)
        self.assertFalse(translation.DEVANAGARI.search(output))
        self.assertEqual(engine.call_args.args[0], [])

    def test_mixed_lines_translate_around_identifiers_and_keep_line_breaks(self):
        source = 'पत्ता: १२३ मुख्य रस्ता\nसंपर्क: +91 9876543210 test@example.test www.example.test\n'
        with patch.object(translation, '_translate_parts', side_effect=lambda parts, language: ['English text' for _ in parts]) as engine:
            english, status = translation.translate_document_text(source)
        self.assertEqual(status, 'translated')
        for value in ('123', '+91 9876543210', 'test@example.test', 'www.example.test'):
            self.assertIn(value, english)
            self.assertNotIn(value, ' '.join(engine.call_args.args[0]))
        self.assertEqual(english.count('\n'), source.count('\n'))
        self.assertFalse(translation.DEVANAGARI.search(english))

    def test_failure_and_partial_translation_are_not_reported_as_success(self):
        for result in (['अपूर्ण'], [], ['']):
            with patch.object(translation, '_translate_parts', return_value=result):
                self.assertEqual(translation.translate_document_text('नमस्कार'), (None, 'unavailable'))
        with patch.object(translation, '_translate_parts', side_effect=RuntimeError('missing model')):
            self.assertEqual(translation.translate_document_text('नमस्कार'), (None, 'unavailable'))

    def test_english_does_not_load_translation_model(self):
        with patch.object(translation, '_load_model') as loader:
            self.assertEqual(translation.translate_document_text('Example Ltd'), ('Example Ltd', 'not_needed'))
            loader.assert_not_called()

    def test_translated_preview_uses_latin_fields_and_original_identifiers(self):
        source = 'अनमोल टेक्नोलॉजी\nश्री राम शर्मा\nसंचालक\nमुख्य सड़क, पुणे 411001\n९८७६५४३२१०\ninfo@example.test'
        english = 'Priceless Technologies\nMr Ram Sharma\nDirector\nMain Road, Pune 411001\n1111111111\ninvented@example.test'
        with patch.object(ocr, 'translate_document_text', return_value=(english, 'translated')):
            fields = ocr._fields_from_ocr_text(source)
        self.assertEqual(fields['original_ocr_text'], source)
        self.assertEqual(fields['contact_numbers'], ['9876543210'])
        self.assertEqual(fields['email_addresses'], ['info@example.test'])
        for key in ('company_name','owner_name','designation','company_address','raw_text'):
            self.assertFalse(re.search(r'[\u0900-\u097f]', fields.get(key) or ''))
        self.assertNotEqual(fields['company_name'], 'Priceless Technologies')
        self.assertEqual(fields['designation'], 'Director')

    def test_marathi_sentence_uses_marathi_source_language(self):
        with patch.object(translation, '_translate_parts', return_value=['We sell clothes.']) as engine:
            self.assertEqual(translation.translate_document_text('आम्ही कपडे विकतो.')[1], 'translated')
            self.assertEqual(engine.call_args.args[1], 'mr')
