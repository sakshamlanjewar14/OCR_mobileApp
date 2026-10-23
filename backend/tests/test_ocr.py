import unittest
import re

from PIL import Image, ImageDraw

from backend.app.ocr import _merge_qr_fields, _parse_fields, _split_card_image


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
