# Lead Gennie

A local lead-generation app for scanning business cards and company documents, reviewing extracted details, and exporting saved leads. The stack is React, FastAPI, and PostgreSQL. OCR and imported rows stay as drafts until a person checks and saves them.

The proposed mobile extension is described in [docs/lead-gennie-mobile-prd.md](docs/lead-gennie-mobile-prd.md). It uses a free-software/self-hosted approach and documents the limits of public geocoding services.

## What the MVP supports

- Capture a business card with the device camera, upload multiple files, or choose a folder. Supported scans are JPG, PNG, WebP, BMP, TIFF, GIF, PDF, and ZIP archives containing supported scans.
- Process scans one by one in the review queue. A combined image detected as multiple cards is presented one card at a time. Each card must be checked and saved before it becomes a lead.
- After a card upload finishes processing, the review form loads the completed OCR fields automatically; the uploaded image remains available beside the editable fields for verification.
- The review dropdown is limited to documents from the most recently selected files, folder, or ZIP batch. Older pending drafts cannot inflate the current folder's queue or be mistaken for files in that folder.
- Preview TXT, CSV, and XLSX data files. CSV/XLSX imports find recognized headings within the first 25 rows, so leading blank/title rows are allowed. They use only columns matching Add Lead fields: company, owner, designation, address, phone, email, website, explicitly labeled Other Document Text, and clearly labeled company-description columns. Unrelated columns are ignored, and the uploaded workbook is left unchanged. TXT and supported document-text values can be parsed as labeled text. Review and save rows individually, or use **Generate remaining leads in one click** for an Excel batch. Batch-generated leads appear in the list as **Check data** until reviewed.
- Review and edit full company name, owner, designation, address, any number of phone numbers, any number of email addresses, website, and the full source text. The source remains linked to OCR leads.
- Store each phone and email as a separate database row. Export adds as many numbered columns as required (`Contact Number 1`, `Contact Number 2`, `Email ID 1`, `Email ID 2`, etc.). XLSX headers are bold with a green highlight; data columns size to their contents, and long cell text wraps with readable row heights.
- Normalize a 10-digit contact number without a country prefix to `+91`, including numeric Excel cells. Existing `+91` and `91` prefixes are not duplicated. Normalization is applied when saving, in list/detail responses, and in Excel exports, including older stored values.
- Search, view, edit, and delete leads. The eye button shows all saved details and source text.
- Keep saved company, owner, designation, address, phone, email, and website values visible under clearly named columns in the lead list. Multiple phone numbers and email addresses display on separate lines.
- Show a 1-based **S.No.** as the first lead-list column and the first column in Excel exports. List numbers continue across pages; an export numbers its included rows from 1 in export order.
- Filter leads by company, owner, state, and city, shown in that order. City/state filters search the existing address field, so no extra location columns are required.
- Enter a row count to export only the first matching records; leave it blank to export all filtered leads. If any lead checkboxes are selected, export uses only those selected leads (and honors the row count if supplied).
- Review an uploaded source and edit its lead fields side by side. Draft navigation is kept in the review workspace instead of a separate documents panel.
- Use **Back to leads** from the uploaded-source preview to return to the lead list while keeping the current draft available for review.
- Use a responsive layout and live rear-camera capture when the browser grants camera permission.
- Use a refreshed enterprise-style interface with a clear brand navigation bar, aligned controls, bold field labels, readable lead rows, and responsive review/details views.
- Use the companion Expo mobile app in `mobile/` to capture one or both business-card sides, preview/edit OCR and QR values, save one reviewed lead, call/email/open links, search/filter, and export/share an XLSX file.
- Store Facebook, Instagram, and WhatsApp links as structured lead fields in PostgreSQL and include them in list/detail views and Excel exports.

## Test import file

`test-data/sample-leads-10.xlsx` contains 10 synthetic lead rows for testing the spreadsheet upload flow. It includes separate `Contact Number 1` / `Contact Number 2` and `Email ID 1` / `Email ID 2` columns, with some rows containing multiple values. Upload it with **Upload files**, then review and save rows individually or choose **Generate remaining leads in one click**. Batch-generated leads are flagged for data review. The workbook does not seed PostgreSQL by itself. Autogenerated lead IDs are assigned when rows are saved.

## Local setup

1. Start your local PostgreSQL service and create the database you want to use.
2. Copy `backend/.env.example` to `backend/.env` and set `DATABASE_URL`. Keep credentials in this ignored local file; do not share them.
3. Install backend dependencies once from PowerShell:

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

4. Start the API from `backend/`:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --env-file .env --host 127.0.0.1 --port 8000
```

5. In a second PowerShell window, install and start the frontend:

```powershell
cd frontend
npm install
npm run dev -- --host 127.0.0.1
```

Open http://localhost:5173. API documentation is available at http://localhost:8000/docs.

If port 8000 is already used by another local application, run this app's API on port 8001 and set `VITE_API_URL=http://127.0.0.1:8001/api` in `frontend/.env.local`, then restart Vite. The API's local CORS defaults allow Vite ports 5173 through 5176 on `localhost` and `127.0.0.1`; keep any custom frontend port in `CORS_ORIGINS` or browser requests will fail even when the API is healthy. Keep the frontend API URL pointed to this app's API; a different service on port 8000 can also cause upload requests to fail.

## Mobile app setup

The Expo client is in `mobile/` and reuses the same API and PostgreSQL data. From PowerShell:

```powershell
cd mobile
npm install
Copy-Item .env.example .env
```

Set `EXPO_PUBLIC_API_URL` in `mobile/.env` before launching. Use `http://10.0.2.2:8001/api` for the Android Studio emulator, `http://127.0.0.1:8001/api` for an iOS simulator when the API is on the same Mac, or the API computer's private LAN address for a phone on the same trusted Wi-Fi. The API must be reachable from the device. For a physical Android device using a local HTTP API, start Expo in development mode with `EXPO_PUBLIC_ALLOW_INSECURE_LOCAL_API=true`; use HTTPS for any release build. iOS devices should use HTTPS for the API. Do not expose this unauthenticated MVP to the public internet.

Start the Expo development server with `npm start` and scan its QR code using Expo Go. `npm run typecheck`, `npm run test:unit`, and `npm run export:android` check the TypeScript, local QR/review helpers, and Android JavaScript bundle. A bundle export is not an installed Android app; a device/emulator is required for camera and native link-handler verification. iOS native builds require macOS.

The phone must reach the API computer over a private network. The mobile client sends card images to the operator's FastAPI server for OCR and lead storage; QR decoding is local on the phone. Call opens the dialer with a prefilled number, email uses the device's configured mail app, and website/social actions use standard OS links. Excel export opens the native share sheet when supported.

## OCR and data review

### Installing the web app on a phone (PWA)

The frontend is PWA-ready. Build it with `npm run build`, serve the `frontend/dist` directory over HTTPS (or use `localhost` while testing on the same computer), then open it in Chrome or Edge and choose **Install app** / **Add to Home screen**. For a phone on the same Wi-Fi network, use the computer's LAN address and serve the frontend over HTTPS; `localhost` on the phone means the phone itself. The API must also be reachable at the computer's LAN address, and its CORS list and firewall must allow the phone. Camera access requires HTTPS or localhost. The service worker caches only the app shell; OCR uploads and lead data still use the FastAPI server.

Local installation verified on 2026-10-22: Tesseract 5.4.0 is installed for the Windows user. The ignored `backend/.env` points `TESSERACT_CMD` to its executable and `TESSDATA_PREFIX` to the ignored `backend/ocr-runtime/tessdata` folder containing official `tessdata_fast` English, Hindi, and Marathi models. These machine-specific files are not bundled with the source; other servers need their own installation. Check availability with `tesseract --list-langs` using the configured executable and model directory, then restart FastAPI after changing OCR settings.

Installation checks recognized Devanagari in the supplied Hindi card and passed a synthetic Marathi text check. The stylized Hindi card still had incomplete field extraction (including a missed phone number), so installing language data does not guarantee complete or accurate lead fields. Review the original image before saving. Local English translation is now configured; OCR omissions still need manual correction.

The API uses Tesseract when installed, requesting `eng+hin+mar` by default. Install the corresponding Tesseract traineddata files and configure `TESSERACT_LANGUAGES` if needed. If one or more language packs are unavailable, it retries English OCR. On Windows, it falls back to Windows' built-in OCR API if Tesseract is unavailable; install an OCR-capable language in Windows Settings under **Time & language > Language & region**. To point to a Tesseract installation explicitly, set `TESSERACT_CMD` in `backend/.env` and restart the API.

Hindi/Marathi OCR uses the free, MIT-licensed local `facebook/m2m100_418M` model for English translation. No card text is sent to an external translation service. Names and recognizable address lines use Latin transliteration to avoid changing their identity or location; spellings require review. The exact original OCR transcript remains separately available. The review page shows conversion status and **Re-scan and translate** for older drafts. Saving OCR leads with remaining Devanagari in structured fields or document text is blocked until corrected; the original transcript is exempt.

Install from the backend directory:
```powershell
.\.venv\Scripts\python.exe -m pip install torch --index-url https://download.pytorch.org/whl/cpu
.\.venv\Scripts\python.exe -m pip install -r requirements-translation.txt
.\.venv\Scripts\python.exe scripts/setup_translation.py
```
Set `TRANSLATION_MODEL_DIR` in the ignored `backend/.env` to the absolute downloaded `backend/ocr-runtime/m2m100_418M` directory and restart FastAPI. The setup script downloads a pinned official model (approximately 2 GB); inference uses local-only files. The first scan loads the model and can be slower on CPU. Preserve the downloaded model card/license. Model translation and language detection are best-effort; Hindi and Marathi share a script, so ambiguous short phrases still need review. Existing saved leads are not automatically rewritten.

OCR and raw-data parsing are best-effort. The app keeps the recognized OCR source text for review. Spreadsheet imports find supported headers within the first 25 rows and retain only recognized Add Lead columns and supported document-text columns; unrelated columns are ignored. The user must compare fields with the source before saving. Pattern matching cannot guarantee that a value belongs in the right field. Do not treat unreviewed data as verified.

Field mapping gives explicit labels priority over visual heuristics. This prevents labelled company, owner/director, designation, address, email, website, social, city, state, and pincode values from being reassigned by a nearby line. Labeled mobile/contact values are kept separate from telephone, fax, WhatsApp URL digits, and pincode values. Re-scan existing drafts after parser changes; saved leads are not rewritten automatically.

The supplied card regression set also covers rotated phone photos, pipe-separated owner/company lines, registered names competing with authorised product brands, branded domains, and Hindi/Marathi OCR fallback. A source line is not treated as certain solely because it is large; the review page remains the final confirmation step.

Field extraction recognizes email addresses and websites by their patterns, collects phone numbers including `+91` and `91` prefixes, and treats `Mr.`, `Mrs.`, `Ms.`, `Miss`, `Dr.`, or `Prof.` names as likely owners. Company-name clues include legal suffixes and business terms such as estate, logistics, industries, technology/tech, pharmaceuticals, corporation, services, advertising, enterprises, hotel, restaurants, finance, developers, education, consultancy, movers/packagers, automation, hospital, NGO, institutes, and classes. A line that reads like a job title is excluded from company selection. When Tesseract layout data is available, the larger brand text remains a fallback clue. Labeled source text keeps each label and value on the same line. The review screen shows possible mismatches and requires the user to confirm the source check before saving.

Use HTTPS or localhost for camera access; browsers block camera access on most insecure origins. Camera availability also depends on device permission and browser support. File and folder upload remain available if camera capture is unavailable.

## Data and sharing

Phone and email values are stored in `lead_contacts`, one row per value, linked to a lead. The `leads` table stores structured company, social-link, English/other-document, and original OCR fields. `lead_source_images` links one or more front/back source documents to the same lead. Startup creates new tables and additively migrates missing lead columns without resetting existing records.

The leads list and detail view both include these separate phone and email values, the three social links, and original OCR transcript. Saving or editing a lead refreshes the list so the values appear immediately. The mobile client uses the same records, requires an explicit save after review, and can attach front and back images to one lead.

Export selected leads or all filtered results as an `.xlsx` file, then share that file using your normal file-sharing method. Mobile export uses the device share sheet where supported. QR links are classified locally and never opened or fetched automatically. Coordinate reverse-geocoding is disabled until an operator configures a permitted Nominatim endpoint and an identifying User-Agent; the lookup is user-triggered, cached, limited to one request per second, and sends only coordinates.

This local single-user MVP has no sign-in or access control. Keep it on a trusted local machine/network and do not expose the API or uploaded source files to a public network. Public deployment still requires authentication, TLS, and operational safeguards.

## Current limits

Raw files are supported as TXT, CSV, and XLSX in the web app. Scanned documents are supported as images, PDF, and ZIP. Folder selection depends on browser support for directory upload. Very large archives may take time to scan because documents are processed sequentially. OCR, card splitting, QR field mapping, geocoding, translation, and unstructured-file mapping always need human review before saving.

The mobile implementation was checked with unit/API/export tests and an Android bundle build. This Windows workspace has no connected Android device or configured emulator, so camera/OS-link interaction was not physically exercised; iOS build/device verification was unavailable. Hindi/Marathi Tesseract data and M2M100 are installed locally; synthetic Hindi/Marathi translation and the supplied Hindi-card pipeline were checked. This does not establish accuracy for all cards. The API has no authentication, so public deployment is not ready.

