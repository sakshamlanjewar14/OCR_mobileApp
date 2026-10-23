import unittest
import re
from unittest.mock import patch

from PIL import Image, ImageDraw

from backend.app.ocr import _merge_ocr_transcripts, _merge_qr_fields, _parse_fields, _read_card_text, _read_image_with_layout, _split_card_image


class OCRLatencyRegressionTests(unittest.TestCase):
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
            "Acme Industries Pvt Ltd\nJane Doe\n+91 9876543210", {}
        )), patch("backend.app.ocr._read_recovery_crops") as recovery:
            _read_card_text(image)
        recovery.assert_not_called()


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
    def test_sample_barbowws_cta_does_not_become_owner_or_designation(self) -> None:
        fields = _parse_fields(
            "SALON PARTNERS\nJoin as a Partner\nkapil@barbowws.com\n"
            "www.barbowws.com\nPune, India\nBook your appointment\n"
            "Check estimated waiting time\nAlert before 30 minutes"
        )

        self.assertEqual(fields["company_name"], "Barbowws")
        self.assertIsNone(fields["owner_name"])
        self.assertIsNone(fields["designation"])

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
