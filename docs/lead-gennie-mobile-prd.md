# Lead Gennie Mobile App — Product Requirements Document

**Status:** Core implementation added; platform/model validation remains  
**Date:** 2026-10-21  
**Existing product:** Lead Gennie web app (React + FastAPI + PostgreSQL)

## 1. Purpose

Extend Lead Gennie with a mobile app for capturing business cards and company documents, extracting lead information, reviewing and correcting it, and saving the confirmed lead to the existing system. The mobile app will use the existing FastAPI and PostgreSQL backend. The current web app remains available.

The implementation must use libraries and services with no per-scan or paid API requirement. The user supplies and operates any server or device needed to run the software; “free software” does not mean hosting, hardware, mobile data, or app-store distribution is free.

## Implementation status (2026-10-21)

The core mobile project and additive shared-API work are implemented in `mobile/` and `backend/`. The existing web app remains and has the social-link fields. The API migration ran at startup against the configured local PostgreSQL database without deleting existing lead data. The following local checks passed:

- All 34 backend unit tests, including front/back association, separate contacts, social XLSX export, QR parsing, translation-unavailable preservation, and geocoder cache/configuration.
- Frontend production build; mobile TypeScript check; five mobile QR/review-helper unit tests; Android JavaScript bundle export.
- A synthetic-only PostgreSQL/API smoke flow: explicit save of one lead linked to two source images, list/filter/details, source-image retrieval, separate phone/email values, social-link fields, selected XLSX headers/values, and cleanup of the test lead and images.

Device-specific checks are still pending: ADB reports no connected Android device or configured emulator, and iOS native builds require macOS. As of 2026-10-22, Hindi/Marathi Tesseract data and the local M2M100 model are installed. The shared backend converts descriptions to English and renders names/recognizable addresses in Latin letters, preserving the original transcript separately. Translation errors and stylized OCR omissions remain possible; explicit review is required. Untranslated OCR fields cannot silently be saved as English. Nominatim is opt-in and was tested with a mocked response only; configure a permitted endpoint and identifying User-Agent before use. The API has no authentication, so this build is for a trusted local network only and is not ready for public deployment. Review drafts survive request/network errors while the mobile app remains open, but are not encrypted or persisted across app restarts.

## 2. Users and main outcome

The primary user is a company representative collecting prospects at events or meetings. They can capture one or both sides of a card, review extracted information on the phone, correct mistakes, and explicitly save one lead with its source images.

## 3. Scope

### Included

- Android and iOS mobile client built with React Native and Expo, connected to the existing FastAPI API.
- Camera capture or photo selection for a single card side or a front/back pair.
- On-device QR detection and parsing for supported standard formats.
- OCR for English, Hindi, and Marathi, followed by local English translation for Hindi/Marathi text.
- Editable review before save, with the front/back source images available for comparison.
- Structured company, person, address, contact, email, website, social-link, and other-document-text fields.
- Mobile-friendly lead list and details, with call, email, website, and social-link actions.
- Excel export updated to include the social fields and separate phone/email columns.

### Not included in this phase

- Paid OCR, translation, mapping, or AI APIs.
- Automatically saving a lead as soon as an image is captured or uploaded.
- Crawling/fetching arbitrary URLs found in QR codes.
- Guaranteed OCR or translation accuracy; users must review the draft.
- Offline multi-device synchronization. A draft may be retried after connectivity returns, but full offline sync is a later feature.

## 4. Main mobile workflow

1. The user chooses **Scan card** or **Choose image**.
2. The user captures/selects the front. They can accept a one-sided card or choose **Add back side** and capture/select the reverse. They can retake either side before processing.
3. The app checks the image is readable, detects QR codes, and sends the source image(s) to the existing backend for OCR and draft processing.
4. The app associates both sides with one draft/lead. OCR results from each side are merged field-by-field. A value from one side must not overwrite a value from the other without a clear rule; conflicts are shown for review.
5. If a recognized QR code contains supported contact data, location data, or social links, those values are mapped to their corresponding fields and marked as QR-sourced in the review state.
6. The user reviews the source image(s), original recognized text, translated/normalized text, and editable lead fields. The user corrects values and chooses **Save lead**.
7. Only after an explicit save does the app create the lead in PostgreSQL. The saved lead appears in the mobile and web lists and in future exports.

## 5. Functional requirements

### 5.1 Camera and front/back capture

- Provide a clear one-sided flow and an optional second-side flow.
- Label captured images **Front** and **Back**; permit retake and preview before processing.
- Keep each image attached to the same lead draft and later to the saved lead.
- Correct orientation where possible and reject unreadable/empty image input with a useful message.
- Keep the existing one-document and multi-card upload behavior in the web app.

### 5.2 QR scanning and field mapping

- Scan QR codes from the live camera or captured image.
- Parse known formats: vCard/MECARD, `tel:`, `mailto:`, `geo:` coordinates, recognized map links, HTTP(S) URLs, and social profile links.
- Map contact name, company, phone, email, website, postal address, and supported social URLs into their matching fields.
- Do not put a map link in Website when it is clearly a location link. Do not treat arbitrary QR text as a verified field.
- Show unsupported or ambiguous QR content as source text and let the user decide what to do with it.
- Do not automatically fetch arbitrary URLs. Show the decoded domain/content type for review and open links only when the user taps them.
- For location QR codes containing coordinates, request an address only after the user chooses **Resolve address**. Use a configurable OpenStreetMap Nominatim endpoint only for modest, user-triggered lookups, with caching, a maximum of one request per second, required OpenStreetMap attribution, and no transmission of card text or contact details. If lookup is unavailable or ambiguous, show the coordinates/map link and leave Address editable. Public Nominatim is subject to its [usage policy](https://operations.osmfoundation.org/policies/nominatim/); self-hosting is required if the usage or privacy rules cannot be met.

### 5.3 OCR, language recognition, and English output

- Recognize printed English, Hindi, and Marathi text from card images.
- Run Tesseract locally with the appropriate `eng`, `hin`, and `mar` language data; do not send source images or OCR text to a paid cloud OCR API.
- Detect Hindi/Marathi script and translate recognized text to English with a self-hosted M2M100 model. The project and model checkpoints are published under MIT; deployments must retain required license notices. [M2M100](https://huggingface.co/facebook/m2m100_418M)
- Keep the original source image and original OCR transcript for audit/review. Store structured lead fields and exported “Other document text” in English. Do not discard original OCR text when translation is applied.
- Do not semantically translate personal or registered company names. Preserve them in their original script and provide a Latin transliteration where available; show both for the user to correct. Leave uncertain translations flagged for review.
- Keep email addresses, phone numbers, web URLs, and QR payloads unchanged by translation.
- User confirmation remains required even when OCR/translation confidence is high.

### 5.4 Lead fields and draft editing

The mobile review form must support these fields, using the existing meanings and storage behavior:

- Full company name, including legal suffixes.
- Owner/contact-person name.
- Designation.
- Complete address in one field.
- Any number of contact numbers, each stored separately; prefix an unprefixed 10-digit Indian mobile number with `+91` when saved.
- Any number of email addresses, each stored separately.
- Website.
- Facebook link.
- Instagram link.
- WhatsApp link or WhatsApp contact link.
- Other document text in English.

Use one draft state as the source of truth for the preview. Editing a structured field updates its corresponding clearly labeled value in the editable normalized text. Editing a clearly labeled line updates the matching field. Preserve unrecognized/unlabeled text without guessing its field. Conflicts must be visible and resolved by the user before saving.

### 5.5 Lead list and mobile actions

- Present saved leads in a responsive mobile layout that avoids requiring horizontal scrolling to perform common actions.
- Show the serial number, company, owner, designation, contact numbers, emails, website, and available social links.
- Put a **Call** action next to each contact number. Tapping it opens the phone dialer with the number filled in; the user confirms the call in the phone app.
- Tapping an email opens the device’s default mail composer through `mailto:`. Gmail is opened only if it is the device’s configured handler or the user explicitly chooses Gmail; do not promise a specific mail app.
- Tapping a website opens the system browser. Facebook, Instagram, and WhatsApp links use standard HTTPS/universal links so the corresponding app can open when installed, otherwise the browser may open.
- Keep current lead search, filters, details, edit, delete, and selection/export behavior available where practical on mobile.

### 5.6 Save and Excel export

- Saving is explicit and occurs only after the user confirms the review.
- Save one lead for a front/back pair; keep both source images associated with that lead.
- Preserve separate contact rows and email rows in PostgreSQL.
- Include Facebook, Instagram, and WhatsApp columns in XLSX export.
- Keep `S.No.` as the first export column and keep contacts/emails in separate numbered columns (`Contact Number 1`, `Contact Number 2`, `Email ID 1`, etc.).
- Keep export headers bold/highlighted and size columns to readable content as in the existing web export.

### 5.7 Mobile list, filtering, and review state

- Search/filter the shared lead list by company, owner, state, and city, with city/state matched in the existing address field.
- Load the lead list in pages and select records for XLSX export. If nothing is selected, export the active filtered set. An optional row count limits the export.
- Open the native share sheet for the generated workbook when supported.
- On a network/save failure, keep the current review state and selected source images in the active app process so the user can retry. This MVP does not persist encrypted offline drafts across app termination.
- The structured fields and clearly labeled lines in “Other document text” synchronize in either direction. Unknown/unlabeled text is preserved and never assigned to a field based on a guess.

## 6. Free-software and service policy

- Mobile client: React Native and Expo; use device camera/QR capabilities without a paid scan API. Expo source is MIT licensed. [Expo repository](https://github.com/expo/expo)
- OCR: self-hosted Tesseract; Apache-2.0. [Tesseract](https://github.com/tesseract-ocr/tesseract)
- Translation: self-hosted M2M100; no paid translation endpoint. Model inference consumes the operator’s own CPU/GPU and storage.
- Geocoding: optional, user-triggered OpenStreetMap Nominatim lookup under its policy, or a self-hosted instance when public-service terms/privacy are unsuitable. No Google Maps Geocoding API or other paid provider is required.
- Calls, email, web, and social navigation use operating-system URL handlers and standard links; no paid link-routing service is needed.
- No card image, personal contact field, or OCR transcript may be sent to a third-party AI/translation service.
- The API and PostgreSQL must remain on infrastructure controlled by the operator. Public mobile access requires HTTPS and authentication before deployment; a free software stack does not provide free, secure hosting by itself.

## 7. Acceptance criteria

1. A user can capture only a front image and complete the review/save flow.
2. A user can capture front and back images and receive one draft and one saved lead with both images retained.
3. QR vCard/contact fields populate the matching fields, while ambiguous content remains for review.
4. A recognized coordinate QR can be resolved through the configured free geocoding option after a user action; failed/ambiguous lookup does not overwrite the address.
5. Hindi and Marathi printed text is recognized and translated locally into English fields; original source remains available, and phone/email/URL values remain unchanged.
6. Every field can be edited before save. Clearly labeled text and structured fields stay synchronized without guessing from unlabeled text.
7. No lead is written before the user taps Save. Front/back pairs create one lead.
8. Each phone/email remains a separate value. Call, email, website, Facebook, Instagram, and WhatsApp actions open the appropriate OS handler or browser.
9. Excel export contains the new social columns, first-column serial number, and separate numbered contact/email columns.
10. The mobile app works against the existing FastAPI/PostgreSQL backend without changing existing web lead behavior.

## 8. Delivery sequence

1. Add mobile project and connect to existing API; implement camera capture, front/back draft, editable review, and explicit save.
2. Add on-device QR reading and safe parsing/mapping for vCard, phone, email, URL, and social links.
3. Add Tesseract Hindi/Marathi language support and self-hosted M2M100 translation; verify English output and preserve original OCR text.
4. Add optional location resolution with policy-compliant Nominatim configuration, attribution, caching, rate limiting, and manual fallback.
5. Add social fields to PostgreSQL/API/web form/mobile details/list/XLSX, plus mobile call/email/browser actions.
6. Test on Android and iOS devices with one-sided cards, front/back pairs, multilingual cards, QR formats, failed network, and export.

## 10. Implemented setup and validation notes

- Install with `cd mobile; npm install`; set `EXPO_PUBLIC_API_URL` in ignored `mobile/.env`. Android emulator commonly uses `http://10.0.2.2:<port>/api`; a physical phone requires a reachable private LAN API address. HTTPS is required for release builds. The Android development config permits HTTP only when `EXPO_PUBLIC_ALLOW_INSECURE_LOCAL_API=true` is explicitly set.
- Run `npm start` for Expo Go, `npm run typecheck`, `npm run test:unit`, or `npm run export:android` for an Android JavaScript bundle check. Bundle export does not install/run a native app.
- Backend migration is additive: three nullable social-link columns, original OCR transcript, and the new `lead_source_images` association table. Existing `document_id` continues to identify the primary/front source for compatibility; the new relation stores all sides with `front`/`back` and order.
- The offline translation adapter needs the local `facebook/m2m100_418M` directory configured by `TRANSLATION_MODEL_DIR` plus optional packages from `backend/requirements-translation.txt` and a compatible local PyTorch build. Weights are not checked in and no model is downloaded automatically.
- Public Nominatim must not be used unless its current policy can be followed. Configure `NOMINATIM_URL` and `NOMINATIM_USER_AGENT`; requests contain coordinates only, are user-triggered, cached, and rate-limited in one API process. Self-host Nominatim when public-service use or privacy is unsuitable.
- Native calls, mail, browser/social intents, device camera, Android emulator, and iOS are not considered manually verified until exercised on real supported devices.

## 9. Key risks and limits

- OCR quality depends on focus, lighting, card print, and the installed language data.
- Translation may alter names or specialized company terms; original image/text and human review are mandatory.
- A QR code may contain arbitrary or malicious links; parse without auto-fetching and require a user tap before navigation.
- A location QR may identify a map pin rather than the company’s mailing address. Show the proposed address for confirmation.
- Free public geocoding is rate-limited and policy-bound. At higher volume or stricter confidentiality, self-hosting requires storage and operational resources.
- A mobile client needs a reachable API. Using a computer on a local network is suitable for development; internet deployment needs operator-controlled hosting, TLS, authentication, backups, and budget monitoring.


## 2026-10-22 conversion fix

Replaced the unavailable translation integration with pinned, local-only MIT M2M100 weights. See README for reproducible installation. The web review shows translation status, original OCR text and a re-scan action. Both web and mobile use the same backend English-save validation. Phone/email/website identifiers are retained from original OCR rather than model-generated values. Original OCR text is retained on explicit save. Existing saved data is not rewritten. CPU inference takes additional time, and all translated fields require human review.
