import unittest
import re
import tempfile
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw

from backend.app.ocr import _decode_qr_payloads, _merge_ocr_transcripts, _merge_qr_fields, _parse_fields, _read_card_text, _read_image_with_layout, _read_recovery_crops, _split_card_image, extract_fields


SCREENSHOT_PHONES = ["+91 89995 21798", "+91 74994 22904", "+91 98604 67728",
                     "+91 98232 11418", "+91 92701 98052", "+91 91582 21895", "+91 96658 06949"]


class OCRLatencyRegressionTests(unittest.TestCase):
    def test_image_scan_reports_non_overlapping_pipeline_timings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "card.png"
            Image.new("RGB", (800, 400), "white").save(path)
            with patch("backend.app.ocr._read_card_text", return_value=(
                "Acme Industries Inc\n+1 415 555 1212\nhello@acme.com", {}
            )), patch("backend.app.ocr._decode_qr_payloads", return_value=[]) as decode_qr:
                fields = extract_fields(path, "image/png")

        timings = fields["processing_times_ms"]
        for key in ("decode_ms", "preprocess_ms", "ocr_ms", "translation_ms", "translation_model_init_ms", "translation_inference_ms", "field_extraction_ms", "qr_ms", "total_ms"):
            self.assertIn(key, timings)
        self.assertEqual(fields["company_name"], "Acme Industries Inc")
        self.assertEqual(fields["email_addresses"], ["hello@acme.com"])
        decode_qr.assert_called_once()

    def test_landscape_recovery_uses_one_full_card_pass_with_indic_languages(self) -> None:
        image = Image.new("RGB", (900, 500), "white")
        with patch.dict("os.environ", {"TESSERACT_LANGUAGES": "eng+hin+mar"}), \
             patch("backend.app.ocr._uses_windows_ocr_fallback", return_value=False), \
             patch("backend.app.ocr.pytesseract.image_to_string", return_value="कंपनी") as recognize:
            results = _read_recovery_crops(image)
        self.assertEqual(results, ["कंपनी"])
        self.assertEqual(recognize.call_count, 1)
        self.assertEqual({call.kwargs["lang"] for call in recognize.call_args_list}, {"eng+hin+mar"})
        self.assertEqual(recognize.call_args.kwargs["config"], "--psm 11")

    def test_qr_decoder_upscales_small_printed_codes(self) -> None:
        import cv2
        import numpy as np
        payload = "https://example.com/card"
        qr = cv2.QRCodeEncoder_create().encode(payload)
        # A small rendered card QR should still decode after the bounded 2x pass.
        image = Image.fromarray(cv2.cvtColor(cv2.resize(qr, (84, 84)), cv2.COLOR_GRAY2RGB))
        self.assertEqual(_decode_qr_payloads(image), [payload])

    def test_windows_fallback_uses_one_ocr_read_without_layout_retries(self) -> None:
        image = Image.new("RGB", (720, 420), "white")
        with patch("backend.app.ocr._uses_windows_ocr_fallback", return_value=True), \
             patch("backend.app.ocr._windows_ocr", return_value="Acme Business Card") as windows_ocr:
            text, heights = _read_image_with_layout(image)

        self.assertEqual(text, "Acme Business Card")
        self.assertEqual(heights, {})
        windows_ocr.assert_called_once_with(image)

    def test_complete_english_card_skips_recovery_only_for_missing_designation(self) -> None:
        image = Image.new("RGB", (900, 500), "white")
        with patch("backend.app.ocr._read_image_with_layout", return_value=(
            "Acme Industries Pvt Ltd\nJane Doe\n+91 9876543210\njane@acme.com\nwww.acme.com", {}
        )), patch("backend.app.ocr._read_recovery_crops") as recovery:
            _read_card_text(image)
        recovery.assert_not_called()

    def test_found_name_and_phone_still_get_one_crop_for_missing_contact_details(self) -> None:
        image = Image.new("RGB", (900, 500), "white")
        with patch("backend.app.ocr._read_image_with_layout", return_value=(
            "Acme Industries Pvt Ltd\nJane Doe\n+91 9876543210", {}
        )), patch("backend.app.ocr._read_recovery_crops", return_value=["jane@acme.com\nwww.acme.com"]) as recovery:
            text, _ = _read_card_text(image)
        recovery.assert_called_once_with(image, contact_details=True)
        self.assertIn("jane@acme.com", text)


class CombinedCardImageTests(unittest.TestCase):
    def test_touching_cards_in_portrait_photo_are_split(self) -> None:
        # Two landscape cards photographed sideways on a dark, textured surface.
        image = Image.new("RGB", (110, 320), (32, 38, 48))
        draw = ImageDraw.Draw(image)
        draw.rectangle((8, 8, 101, 159), fill=(205, 205, 198))
        draw.rectangle((8, 160, 101, 311), fill=(205, 205, 198))
        draw.line((8, 159, 101, 159), fill=(45, 45, 45), width=1)
        for y in (36, 54, 72, 94, 120, 194, 212, 230, 252, 278):
            draw.line((20, y, 85, y), fill=(65, 65, 65), width=2)

        regions = _split_card_image(image)

        self.assertIsNotNone(regions)
        self.assertEqual(len(regions or []), 2)
        for region in regions or []:
            self.assertGreater(region.width, region.height)


class LeadFieldParsingTests(unittest.TestCase):
    def test_screenshot_contacts_are_not_discarded_without_company(self) -> None:
        text = "WhatsApp\nSearch Chats Calls Updates Tools\nWw Ok\n" + "\n".join(
            f"{phone} 03/10/2026" for phone in SCREENSHOT_PHONES
        )
        fields = _parse_fields(text)
        self.assertEqual(fields["contact_numbers"], SCREENSHOT_PHONES)
        self.assertIsNone(fields["company_name"])
        self.assertIsNone(fields["owner_name"])
        self.assertIsNone(fields["whatsapp_link"])

    def test_upright_screenshot_does_not_rotate_or_run_card_recovery(self) -> None:
        image = Image.new("RGB", (716, 1600), "white")
        text = "WhatsApp\nChats Calls Updates\n" + "\n".join(SCREENSHOT_PHONES)
        with patch("backend.app.ocr._read_image_with_layout", return_value=(text, {})) as read, \
             patch("backend.app.ocr._read_recovery_crops") as recover:
            actual, _ = _read_card_text(image)
        self.assertEqual(actual, text)
        read.assert_called_once_with(image)
        recover.assert_not_called()

    def test_sideways_photo_can_recover_using_orientation_evidence(self) -> None:
        image = Image.new("RGB", (600, 1000), "white")
        text = "Acme Industries\nJane Doe\n+91 9876543210\njane@acme.com"
        with patch("backend.app.ocr._read_image_with_layout", side_effect=[("illegible", {}), (text, {})]) as read:
            actual, _ = _read_card_text(image)
        self.assertEqual(actual, text)
        self.assertEqual(read.call_args.args[0].size, (1000, 600))

    def test_equivalent_indian_phone_formats_are_deduplicated(self) -> None:
        fields = _parse_fields(
            "Acme Technologies Pvt Ltd\n"
            "Mobile: 06769085996\n"
            "Contact: +91 67690 85996"
        )
        self.assertEqual(len(fields["contact_numbers"]), 1)

    def test_each_populated_field_has_a_source_line_or_qr_payload(self) -> None:
        result = _merge_qr_fields({
            "company_name": "Acme Systems Pvt Ltd",
            "contact_numbers": ["+91 9876543210"],
            "email_addresses": ["jane@acme.com"],
            "website": "www.acme.com",
            "original_ocr_text": "Acme Systems Pvt Ltd\nMobile: +91 9876543210\njane@acme.com\nwww.acme.com",
            "qr_data": [{"source_text": "BEGIN:VCARD\\nFN:Jane Doe", "contacts": ["+91 9876543210"], "emails": [], "urls": []}],
        }, [])
        sources = result["field_sources"]
        self.assertEqual(sources["company_name"]["source_text"], "Acme Systems Pvt Ltd")
        self.assertEqual(sources["contact_numbers"]["source_type"], "QR")
        self.assertIn("BEGIN:VCARD", sources["contact_numbers"]["source_text"])
        self.assertEqual(sources["email_addresses"]["source_text"], "jane@acme.com")

    def test_translation_focus_transliterates_names_and_addresses_but_keeps_service_copy(self) -> None:
        from backend.app.ocr import _translation_focus_text
        source = "श्री अमित पाटील\nपुणे महाराष्ट्र 411001\nसर्व प्रकारची लाईट फिटिंग व मेंटेनन्सची कामे"
        focused = _translation_focus_text(source)
        self.assertNotRegex(focused.replace("सर्व प्रकारची लाईट फिटिंग व मेंटेनन्सची कामे", ""), r"[\u0900-\u097f]")
        self.assertIn("shrii", focused.lower())
        self.assertIn("सर्व प्रकारची लाईट फिटिंग व मेंटेनन्सची कामे", focused)

    def test_sample_barbowws_cta_does_not_become_owner_or_designation(self) -> None:
        fields = _parse_fields(
            "SALON PARTNERS\nJoin as a Partner\nkapil@barbowws.com\n"
            "www.barbowws.com\nPune, India\nBook your appointment\n"
            "Check estimated waiting time\nAlert before 30 minutes"
        )

        self.assertEqual(fields["company_name"], "Barbowws")
        self.assertIsNone(fields["owner_name"])
        self.assertIsNone(fields["designation"])

    def test_app_download_qr_ocr_fragments_do_not_become_owner_name(self) -> None:
        fields = _parse_fields("SALON PARTNERS\nDownload the App\nJoin as a Partner\nwww.barbowws.com")
        self.assertIsNone(fields["owner_name"])

    def test_app_store_caption_does_not_replace_contact_person(self) -> None:
        fields = _parse_fields("GET IT ON\nGoogle Play\nKAPIL KADAM\nFounder & CEO\nkapil@barbowws.com\nwww.barbowws.com")
        self.assertEqual(fields["owner_name"], "KAPIL KADAM")

    def test_contact_recovery_preserves_top_of_card(self) -> None:
        image = Image.new("RGB", (1000, 600), "white")
        image.putpixel((0, 0), (0, 0, 0))
        with patch("backend.app.ocr._uses_windows_ocr_fallback", return_value=False), \
             patch("backend.app.ocr.pytesseract.image_to_string", return_value="+91 7066336455") as read:
            _read_recovery_crops(image, contact_details=True)
        crop = read.call_args.args[0]
        self.assertEqual(crop.size, (1100, 1200))
        self.assertLess(crop.getpixel((0, 0)), 100)

    def test_ambiguous_uppercase_and_lowercase_ocr_fragments_stay_blank_as_owner(self) -> None:
        for fragment in ("ALARM IGE MAI", "tele ss"):
            with self.subTest(fragment=fragment):
                self.assertIsNone(_parse_fields(f"Company Services\n{fragment}") ["owner_name"])

    def test_ocr_contact_number_leaking_into_owner_is_left_blank_and_flagged(self) -> None:
        from backend.app.ocr import _fields_from_ocr_text
        source = "Company Services\nJohn Doe call 9876543210"
        with patch("backend.app.ocr._parse_fields", return_value={"company_name": "Company Services", "owner_name": "John Doe 9876543210", "raw_text": source}), \
             patch("backend.app.ocr.translate_document_text", return_value=(source, "not_needed")):
            fields = _fields_from_ocr_text(source)
        self.assertIsNone(fields["owner_name"])
        self.assertIn("Owner Name", " ".join(fields["review_warnings"]))
        self.assertEqual(fields["original_ocr_text"], source)

    def test_company_brand_is_cleaned_of_leading_ocr_symbols(self) -> None:
        fields = _parse_fields("> 7 \\s SM Electricals\nShubham Mande\n9158578322")
        self.assertEqual(fields["company_name"], "SM Electricals")

    def test_non_mobile_ocr_number_is_not_saved_as_an_indian_contact(self) -> None:
        fields = _parse_fields("SM Electricals\nShubham Mande\n9158578322\n3940400322")
        self.assertEqual(fields["contact_numbers"], ["9158578322"])

    def test_social_domains_are_mapped_to_social_fields_not_website(self) -> None:
        fields = _parse_fields("Acme Services\nhttps://facebook.com/acme\nhttps://instagram.com/acme\nwww.acme.com")
        self.assertEqual(fields["website"], "www.acme.com")
        self.assertEqual(fields["facebook_link"], "https://facebook.com/acme")
        self.assertEqual(fields["instagram_link"], "https://instagram.com/acme")

    def test_labeled_business_description_is_kept_separate_from_address_and_raw_text(self) -> None:
        fields = _parse_fields("Company Name: Acme Electricals\nDescription: Lighting installation and maintenance\nAddress: Pune")
        self.assertEqual(fields["business_description"], "Lighting installation and maintenance")
        self.assertEqual(fields["company_address"], "Pune")

    def test_clear_service_statement_is_mapped_to_business_description(self) -> None:
        fields = _parse_fields("SM Electricals\nShubham Mande\nAll types of lighting fitting and maintenance work")
        self.assertEqual(fields["business_description"], "All types of lighting fitting and maintenance work")

    def test_card_service_lines_map_to_description_without_becoming_address(self) -> None:
        fields = _parse_fields(
            "Morya Refrigeration\nWe have a freezer, deep freezer and visible cooler\n"
            "Near City Hospital, Pune 413102"
        )
        self.assertEqual(fields["business_description"], "We have a freezer, deep freezer and visible cooler")
        self.assertEqual(fields["company_address"], "Near City Hospital, Pune 413102")

    def test_marathi_and_hindi_service_terms_get_english_descriptions(self) -> None:
        electrical = _parse_fields("SM Electricals\nआमच्याकडे सर्व प्रकारची लाईट फिटींगची व मेंटेनन्सूची कामे")
        refrigeration = _parse_fields("आमच्याकडे फ्रीज, डिप फ्रीज, विसीबल कुलर, एसी, व्हीआरव्ही कामे")
        fashion = _parse_fields("सभी प्रकार डिजाइनर सलवार ब्लाउज सिलाई कार्य होते हैं")
        self.assertEqual(electrical["business_description"], "Lighting fitting and maintenance services")
        self.assertEqual(refrigeration["business_description"], "Refrigeration, freezer, cooler and air-conditioning services")
        self.assertEqual(fashion["business_description"], "Designer clothing, blouse and tailoring services")

    def test_indic_proprietor_prefix_is_separated_from_the_person_name(self) -> None:
        fields = _parse_fields("प्रो.प्रा.तेनम काळे ine मो.9373 51 2003")
        self.assertEqual(fields["owner_name"], "तेनम काळे")
        self.assertEqual(fields["designation"], "Proprietor")
        self.assertEqual(fields["contact_numbers"], ["9373 51 2003"])

    def test_conflicting_indic_owner_reads_are_left_blank_for_review(self) -> None:
        fields = _parse_fields(
            "प्रो.प्रा.तेनम काळे मो.9373 51 2003\nओो,प्रा.तेजम काळे AIO ime 1.9373 51 200"
        )
        self.assertIsNone(fields["owner_name"])
        self.assertEqual(fields["designation"], "Proprietor")

    def test_recovery_ocr_adds_missed_card_lines_without_dropping_primary_data(self) -> None:
        primary = "SALON PARTNERS\nJoin as a Partner\nkapil@barbowws.com\nwww.barbowws.com"
        recovered = "KAPIL KADAM\nFounder & CEO\n+91 7066336455"
        text = _merge_ocr_transcripts(primary, recovered)
        fields = _parse_fields(text)

        self.assertIn("kapil@barbowws.com", text)
        self.assertIn("KAPIL KADAM", text)
        self.assertEqual(fields["company_name"], "Barbowws")
        self.assertEqual(fields["owner_name"], "KAPIL KADAM")
        self.assertEqual(fields["designation"], "Founder & CEO")
        self.assertIn("7066336455", fields["contact_numbers"][0])

    def test_sample_anmol_card_transliterated_english_field_mapping(self) -> None:
        fields = _parse_fields(
            "Anmol Fashion Boutique\nMeenakshi Verma\n"
            "9305365913\n106/2A, Gupta Market, Near Gopal Talkies, "
            "Above Mummy Papa Showroom, P. Road, Kanpur 208012"
        )

        self.assertEqual(fields["company_name"], "Anmol Fashion Boutique")
        self.assertEqual(fields["owner_name"], "Meenakshi Verma")
        self.assertEqual(fields["contact_numbers"], ["9305365913"])
        self.assertIn("Kanpur 208012", fields["company_address"])

    def test_anmol_ocr_company_fragment_is_left_blank_for_review(self) -> None:
        source = (
            "106/2ए, गुप्ता मार्केट, निकट गोपाल टाकीज\nमम्मी पापा शोरूम के ऊपर, पी.रोड, कानपुर-208012\n"
            "मीनाक्षी वर्मा\n9305365913\nअलमोल\nशन बूटीक\nसभी प्रकार के डिजाइनर सलवार सूट"
        )
        fields = _parse_fields(source)
        self.assertIsNone(fields["company_name"])
        self.assertEqual(fields["owner_name"], "मीनाक्षी वर्मा")
        self.assertEqual(fields["contact_numbers"], ["9305365913"])
        self.assertIn("कानपुर-208012", fields["company_address"])
        from backend.app.ocr import _fields_from_ocr_text
        with patch("backend.app.ocr.translate_document_text", return_value=(None, "unavailable")):
            preview = _fields_from_ocr_text(source)
        self.assertTrue(any("Company name" in warning for warning in preview["review_warnings"]))

    def test_sample_barbowws_full_ocr_text_maps_owner_title_contacts_and_website(self) -> None:
        fields = _parse_fields(
            "KAPIL KADAM\nFounder & CEO\n+91 7066336455\nkapil@barbowws.com\n"
            "www.barbowws.com\nPune, India\nSALON PARTNERS\nJoin as a Partner\n"
            "Book your appointment | Check estimated waiting time | Alert before 30 minutes"
        )
        self.assertEqual(fields["company_name"], "Barbowws")
        self.assertEqual(fields["owner_name"], "KAPIL KADAM")
        self.assertEqual(fields["designation"], "Founder & CEO")
        self.assertEqual(fields["contact_numbers"], ["+91 7066336455"])
        self.assertEqual(fields["email_addresses"], ["kapil@barbowws.com"])
        self.assertEqual(fields["website"], "www.barbowws.com")

    def test_sample_vastu_card_uses_adjacent_brand_words_validated_by_website(self) -> None:
        fields = _parse_fields(
            "VASTU\nRAVIRAJ\nAmit Nalawade\nMarketing Executive\n"
            "+91 86558 73467\namit@vasturaviraj.in\nwwwvasturaviraj.co.in"
        )
        self.assertEqual(fields["company_name"], "VASTU RAVIRAJ")
        self.assertEqual(fields["owner_name"], "Amit Nalawade")
        self.assertEqual(fields["designation"], "Marketing Executive")
        self.assertEqual(fields["contact_numbers"], ["+91 86558 73467"])
        self.assertEqual(fields["website"], "www.vasturaviraj.co.in")

    def test_sample_vastu_noisy_marketing_executive_line_is_normalized(self) -> None:
        fields = _parse_fields(
            "VASTU\nRAVIRAJ\nChanging Lives, Changing Karma\nAmit Nalawade\n"
            "Marketing Execatbeg | oh =\n+91 86558 73467"
        )
        self.assertEqual(fields["company_name"], "VASTU RAVIRAJ")
        self.assertEqual(fields["designation"], "Marketing Executive")

    def test_sample_aditi_card_recovers_company_suffix_from_matching_email(self) -> None:
        fields = _parse_fields(
            "Yuvraj Phalke\nAditi Sales Corpor\nHouse of O Rings & Seals\n"
            "Shop No. 210, Swapnanagari, Udyam Nagar, Pimpri Pune - 18.\n"
            "Tel. 020-27502355 Telefax: 020-27124465\n"
            "aditisalescorporation@gmail.com\nu.asc@rediffmail.com"
        )
        self.assertEqual(fields["company_name"], "Aditi Sales Corporation")
        self.assertEqual(fields["owner_name"], "Yuvraj Phalke")
        self.assertIn("Pimpri Pune", fields["company_address"])
        self.assertEqual(fields["telephone_numbers"], ["020-27502355"])
        self.assertEqual(fields["email_addresses"], ["aditisalescorporation@gmail.com", "u.asc@rediffmail.com"])

    def test_explicit_qr_contact_overrides_ambiguous_ocr_value(self) -> None:
        fields = _merge_qr_fields(
            {"company_name": "Wrong OCR Brand", "owner_name": None, "contact_numbers": []},
            ["BEGIN:VCARD\nFN:Jane Doe\nORG:Acme Solutions Pvt Ltd\nTITLE:Director\nTEL:+919876543210\nEND:VCARD"],
        )
        self.assertEqual(fields["company_name"], "Acme Solutions Pvt Ltd")
        self.assertEqual(fields["owner_name"], "Jane Doe")
        self.assertEqual(fields["designation"], "Director")
        self.assertEqual(fields["contact_numbers"], ["+919876543210"])

    def test_certification_tagline_is_not_mapped_to_owner_or_designation(self) -> None:
        fields = _parse_fields(
            "Acme Solutions\nCertified Partner Of Premium Brands\nJane Doe\nDirector\n"
            "jane@acme.example\nwww.acme.example"
        )

        self.assertEqual(fields["company_name"], "Acme Solutions")
        self.assertEqual(fields["owner_name"], "Jane Doe")
        self.assertEqual(fields["designation"], "Director")

    def test_certification_tagline_is_left_unassigned_without_a_person_name(self) -> None:
        fields = _parse_fields("Acme Solutions\nCertified Partner Of Premium Brands\n")

        self.assertIsNone(fields["owner_name"])
        self.assertIsNone(fields["designation"])

    def test_stock_card_template_placeholders_never_become_lead_data(self) -> None:
        source = (
            "ELECTRONIC\nBUSINESS COMPANY\nLorem Ipsum dolorem stratia\n"
            "NAME SURNAME\nCreative director\n+123 456 7890\n"
            "lorem@ipsum.com\nsitename.domain\n123 Address Avenue\n"
            "Sample City, 45678, Country Land\nID 160982199 © Serkorkin"
        )

        fields = _parse_fields(source)
        self.assertIsNone(fields["company_name"])
        self.assertIsNone(fields["owner_name"])
        self.assertIsNone(fields["designation"])
        self.assertIsNone(fields["company_address"])
        self.assertEqual(fields["contact_numbers"], [])
        self.assertEqual(fields["email_addresses"], [])
        self.assertIsNone(fields["website"])
        self.assertTrue(fields["template_detected"])
        self.assertIn("BUSINESS COMPANY", fields["raw_text"])

        from backend.app.ocr import _fields_from_ocr_text
        with patch("backend.app.ocr.translate_document_text", return_value=(None, "not_required")):
            preview = _fields_from_ocr_text(source)
        self.assertIn("placeholder details", preview["review_warnings"][0])
        self.assertEqual(preview["original_ocr_text"], source)

    def test_foundation_card_keeps_company_person_title_contacts_and_address_separate(self) -> None:
        fields = _parse_fields(
            "Hemendra Mhatre\nZonal Mobilisation Manager\nIdeas to Impact Foundation\n"
            "India's Dream Team for Self-Reliant Bharat\n+91 9820782043\n"
            "hemendra.manohar@i2ifoundation.org\n"
            "Building NO. 30, 2nd Floor, Basant Lok Community Centre, Vasant Vihar, New Delhi\n"
            "India - 110057\n+91 40956600\nwww.iifoundation.org\nImplementation partner of Walmart Vriddhi"
        )

        self.assertEqual(fields["company_name"], "Ideas to Impact Foundation")
        self.assertEqual(fields["owner_name"], "Hemendra Mhatre")
        self.assertEqual(fields["designation"], "Zonal Mobilisation Manager")
        self.assertIn("Building NO. 30", fields["company_address"])
        self.assertIn("110057", fields["company_address"])
        self.assertNotIn("Dream Team", fields["company_address"])
        self.assertEqual(fields["contact_numbers"], ["+91 9820782043", "+91 40956600"])
        self.assertEqual(fields["email_addresses"], ["hemendra.manohar@i2ifoundation.org"])

    def test_screenshot_ocr_recovers_email_dot_only_from_matching_website(self) -> None:
        source = (
            "I do, w\nNayan Kagade\nDirector\n9112227422/8237478379\n"
            "https://www.sanmolsoftware.com\n= nkagade@sanmolsoftwarecom\n"
            "9 Office No 402, 4th Floor, 93 Avenue Solapur"
        )
        fields = _parse_fields(source)

        self.assertEqual(fields["company_name"], "Sanmol Software")
        self.assertEqual(fields["owner_name"], "Nayan Kagade")
        self.assertEqual(fields["designation"], "Director")
        self.assertEqual(fields["email_addresses"], ["nkagade@sanmolsoftware.com"])
        self.assertTrue(any("suggestion from the website domain" in warning for warning in fields["review_warnings"]))
        self.assertTrue(any("separator was restored" in warning for warning in fields["review_warnings"]))

    def test_globefirm_screenshot_uses_domain_words_without_claiming_registered_name(self) -> None:
        fields = _parse_fields(
            "GLOBEFIRM\n+91- 8600150891 (/WhatsApp)\n"
            "rohit shivraj@globefirmexports.com\nwww.globefirmexports.com"
        )

        self.assertEqual(fields["company_name"], "Globe Firm Exports")
        self.assertEqual(fields["email_addresses"], ["shivraj@globefirmexports.com"])
        self.assertTrue(any("complete registered name" in warning for warning in fields["review_warnings"]))

    def test_broken_email_domain_is_not_repaired_without_matching_website(self) -> None:
        fields = _parse_fields("Name Surname\nname@companycom")

        self.assertEqual(fields["email_addresses"], [])

    def test_honorific_identifies_owner_and_larger_brand_line_identifies_company(self) -> None:
        fields = _parse_fields(
            "Mr. Rajesh Kumar\nBusiness Development Manager\nNivara Brand\n"
            "+91 9876543210\nrajesh@nivara.example.com\nwww.nivara.example.com",
            {"mr. rajesh kumar": 12, "business development manager": 10, "nivara brand": 28},
        )

        self.assertEqual(fields["company_name"], "Nivara Brand")
        self.assertEqual(fields["owner_name"], "Mr. Rajesh Kumar")
        self.assertEqual(fields["designation"], "Business Development Manager")
        self.assertEqual(fields["contact_numbers"], ["+91 9876543210"])
        self.assertEqual(fields["email_addresses"], ["rajesh@nivara.example.com"])
        self.assertEqual(fields["website"], "www.nivara.example.com")

    def test_business_categories_and_legal_terms_identify_company_name(self) -> None:
        company_names = [
            "Acme Real Estate", "Acme Logistics Pvt Ltd", "Acme Industries Limited",
            "Acme Technology Solutions", "Acme Pharmaceuticals", "Acme Corporation",
            "Acme Business Services", "Acme Innovation Advertising", "Acme Enterprises",
            "Acme Hotel Group", "Acme Finance Systems", "Acme Developers",
            "Acme Associates Education", "Acme Consultancy", "Acme Society",
            "Acme Movers and Packagers", "Acme Automation", "Acme Hospital",
            "Acme NGO Institute", "Acme Classes", "Acme Spaces",
        ]

        for name in company_names:
            with self.subTest(company_name=name):
                self.assertEqual(_parse_fields(name)["company_name"], name)

    def test_business_job_title_does_not_override_company_classification(self) -> None:
        fields = _parse_fields("Acme Solutions\nBusiness Development Manager")

        self.assertEqual(fields["company_name"], "Acme Solutions")
        self.assertEqual(fields["designation"], "Business Development Manager")

    def test_labeled_source_fields_with_values_on_following_lines_are_joined_for_parsing(self) -> None:
        fields = _parse_fields(
            "Company Name:\nAcme Widgets\nOwner Name:\nMs. Jane Doe\nDesignation:\nSales Manager\n"
            "Contact Number:\n+91 9876543210\nEmail ID:\njane@acme.example.com\nWebsite:\nwww.acme.example.com"
        )

        self.assertEqual(fields["company_name"], "Acme Widgets")
        self.assertEqual(fields["owner_name"], "Ms. Jane Doe")
        self.assertEqual(fields["designation"], "Sales Manager")
        self.assertEqual(fields["contact_numbers"], ["+91 9876543210"])
        self.assertEqual(fields["email_addresses"], ["jane@acme.example.com"])
        self.assertEqual(fields["website"], "www.acme.example.com")
        self.assertIn("Company Name: Acme Widgets", fields["raw_text"])
        self.assertIn("Email ID: jane@acme.example.com", fields["raw_text"])

    def test_explicit_labels_override_heuristics_and_keep_contact_types_separate(self) -> None:
        fields = _parse_fields(
            "Company Name: Sunrise Logistics Pvt Ltd\nOwner Name: Mr. Amit Shah\n"
            "Designation: Director\nAddress: 12 Main Road, Pune\n"
            "Mobile: 9876543210\nTelephone: 020-12345678\nFax: 020-87654321\n"
            "Email ID: amit@example.test\nWebsite: www.sunrise.example\n"
            "Facebook: https://facebook.com/sunrise\nInstagram: https://instagram.com/sunrise\n"
            "WhatsApp: https://wa.me/919876543210\nCity: Pune\nState: Maharashtra\nPincode: 411001"
        )
        self.assertEqual(fields["company_name"], "Sunrise Logistics Pvt Ltd")
        self.assertEqual(fields["owner_name"], "Mr. Amit Shah")
        self.assertEqual(fields["designation"], "Director")
        self.assertEqual(fields["company_address"], "12 Main Road, Pune")
        self.assertEqual(fields["contact_numbers"], ["9876543210"])
        self.assertEqual(fields["telephone_numbers"], ["020-12345678"])
        self.assertEqual(fields["fax_numbers"], ["020-87654321"])
        self.assertEqual(fields["email_addresses"], ["amit@example.test"])
        self.assertEqual(fields["facebook_link"], "https://facebook.com/sunrise")
        self.assertEqual(fields["instagram_link"], "https://instagram.com/sunrise")
        self.assertEqual(fields["whatsapp_link"], "https://wa.me/919876543210")
        self.assertEqual(fields["pincode"], "411001")

    def test_short_ocr_suffix_is_not_added_to_an_indian_mobile_number(self) -> None:
        fields = _parse_fields("Amit Nalawade\nMarketing Executive\n+91 86558 73467 7-8")
        self.assertEqual(fields["contact_numbers"], [])
        self.assertTrue(any("truncating" in warning for warning in fields["review_warnings"]))

    def test_company_line_wins_over_authorised_product_brand_and_pipe_layout(self) -> None:
        fields = _parse_fields(
            "GAWADE\nGREEN POWER\nDeepak Shivaji Gawade\nManaging Director\n"
            "www.gawadegreenpower.com\nAuthorised OEM for\n"
            "Escorts-Kubota Genset Escorts Kubota Limited\n"
            "Deepak Shivaji Gawade | Gawade Green Power Pvt. Ltd.\n"
            "Managing Director gawadegreenpower@gmail.com\n"
            "Office No. 210, 2nd Floor, Laxmi Complex, Near Smart Bajar, Pune"
        )
        self.assertEqual(fields["company_name"], "Gawade Green Power Pvt. Ltd.")
        self.assertEqual(fields["owner_name"], "Deepak Shivaji Gawade")
        self.assertEqual(fields["designation"], "Managing Director")

    def test_company_uses_brand_domain_when_person_name_is_prominent(self) -> None:
        fields = _parse_fields(
            "KAPIL KADAM\nFounder & CEO\nwww.barbows.com\nPune, India"
        )
        self.assertEqual(fields["company_name"], "Barbows")
        self.assertEqual(fields["owner_name"], "KAPIL KADAM")

    def test_gawade_card_keeps_registered_company_owner_and_address_separate(self) -> None:
        fields = _parse_fields(
            "Gawade Green Power Pvt. Ltd.\nDeepak Shivaji Gawade\nManaging Director\n"
            "Mob - +91-9922512000\nceo@gawadegreenpower.com\nwww.gawadegreenpower.com\n"
            "Office No. 210, 2nd Floor, Laxmi Complex, Chinchwad Pune - 411019, Maharashtra India"
        )
        self.assertEqual(fields["company_name"], "Gawade Green Power Pvt. Ltd.")
        self.assertEqual(fields["owner_name"], "Deepak Shivaji Gawade")
        self.assertEqual(fields["designation"], "Managing Director")
        self.assertEqual(fields["contact_numbers"], ["+91-9922512000"])
        self.assertIn("Laxmi Complex", fields["company_address"])

    def test_lucky_marine_legal_suffix_is_kept_when_ocr_reads_it(self) -> None:
        fields = _parse_fields("LUCKY MARINE SHIPPING & LOGISTICS PVT. LTD.\nHrishikesh Mhatre\nDirector")
        self.assertEqual(fields["company_name"], "LUCKY MARINE SHIPPING & LOGISTICS PVT. LTD.")

    def test_truncated_company_ocr_warns_instead_of_claiming_a_complete_registered_name(self) -> None:
        from backend.app.ocr import _fields_from_ocr_text
        fields = _fields_from_ocr_text("LUCKY MARINE SHIPPING & LOGIS'\nwww.luckymarineshipping.com")
        self.assertTrue(any("truncated the company line" in warning for warning in fields["review_warnings"]))

    def test_business_cards_extract_non_latin_owner_and_title_as_latin_text(self) -> None:
        from backend.app.ocr import _fields_from_ocr_text
        marathi_source = "कंपनी लिमिटेड\nश्री अमित पाटील\nसंचालक\nपुणे महाराष्ट्र 411001\n+91 9876543210"
        hindi_source = "अनमोल फैशन बुटीक\nश्रीमती मीनाक्षी वर्मा\nमालकिन\nपी रोड कानपुर 208012\n9305365913"
        for source in (marathi_source, hindi_source):
            with self.subTest(source=source):
                fields = _fields_from_ocr_text(source)
                for key in ("company_name", "owner_name", "designation", "company_address"):
                    self.assertFalse(re.search(r"[\u0900-\u097f]", fields.get(key) or ""))
                self.assertTrue(fields["contact_numbers"])
                self.assertEqual(fields["original_ocr_text"], source)


if __name__ == "__main__":
    unittest.main()
