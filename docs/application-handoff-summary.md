# Lead Gennie application handoff summary

This document is the working summary for the next engineer or agent. The
repository is the source of truth. Do not replace the existing application or
change its routes, authentication, database field names, exports, or review
flow without a demonstrated compatibility reason.

## Product purpose

Lead Gennie converts business-card images and QR codes into reviewable business
lead records. A user can capture a card with the camera, upload one or more
images, scan a QR code, review or correct the extracted fields, and save a lead
to the authenticated database. The application is designed for mobile browsers
and keeps OCR results as editable candidates until the user confirms Save.

## Repository layout

- `frontend/` — React, TypeScript, Vite PWA frontend.
- `frontend/src/App.tsx` — main application flow, authentication screens,
  uploads, review form, lead list, save, export, and QR UI.
- `frontend/src/clientOcr.ts` — optional browser OCR, image QR decoding, and
  local OCR field parsing.
- `frontend/src/qrPayload.ts` — pure local vCard, MeCard, URL, social,
  WhatsApp, map, app-link, and plain-text QR parser.
- `frontend/src/qrScanner.ts` — camera scanning loop with native detector and
  jsQR fallback.
- `frontend/src/draftStore.ts` — IndexedDB drafts and offline lead queue.
- `frontend/public/ocr/` — browser Tesseract runtime assets.
- `backend/app/main.py` — FastAPI routes, authentication, upload processing,
  background OCR jobs, lead save, health/readiness, website preview, and Excel
  export.
- `backend/app/ocr.py` — image preparation, OCR selection, field extraction,
  translation handling, QR merge, confidence/source metadata, and timings.
- `backend/app/paddle_ocr.py` — lazy PaddleOCR model adapter and engine cache.
- `backend/app/qr.py` — backend QR payload parser and classification.
- `backend/app/models.py` — existing SQLAlchemy models.
- `backend/app/database.py` — existing database setup and startup table logic.
- `backend/Dockerfile` — production FastAPI image with Tesseract fallback and
  PaddleOCR dependencies.
- `backend/scripts/` — private evaluation, model preloading, training-data
  preparation, and training-command helpers.
- `docs/ocr-card-improvements.md` — detailed OCR/QR evaluation and training
  notes.

## End-to-end user flow

### Authentication

1. The user registers or logs in through the frontend.
2. FastAPI creates an authenticated session cookie.
3. Frontend API calls use `credentials: "include"`.
4. CORS and cookie settings must allow the deployed frontend origin.
5. The user sees the leads screen only after authentication succeeds.

### Card upload and OCR

1. User selects an image, PDF, ZIP, or camera capture.
2. The frontend keeps a local preview and performs local QR detection.
3. Camera images are reduced to a bounded working size before upload.
4. The backend validates file type, size, image dimensions, and PDF limits.
5. The backend stores the temporary source document and immediately returns a
   document ID with HTTP 202.
6. A bounded background OCR job processes the document.
7. The frontend polls document status with bounded backoff.
8. The user receives a reviewable candidate, never an automatically saved lead.
9. The user corrects fields and clicks Save.
10. Only the structured lead JSON is committed to the database. The original
    source remains available for review/retry and is cleaned up according to
    the existing document lifecycle.

### OCR engine selection

The server path uses PaddleOCR as the primary engine when enabled. Models are
created lazily and cached by language. English is the default. Hindi/Marathi
use the Devanagari route only when requested or when the bounded recovery path
selects it. Tesseract remains the fallback when PaddleOCR or its models are
unavailable. OCR jobs are serialized with `MAX_OCR_CONCURRENCY=1` for the CPU
deployment profile.

The browser Tesseract worker is an optional explicit local fallback. It is not
initialized for every image and has timeouts so a failed WebAssembly worker
does not leave the UI hanging.

### Image processing

- EXIF rotation is corrected before OCR.
- Large images are resized to a bounded longest side.
- QR decoding uses the original or bounded image as appropriate.
- Temporary canvases, ImageBitmaps, blobs, and object URLs are released.
- Corrupt files, unsupported types, oversized files, and oversized pixel counts
  are rejected with a user-readable message.
- Recovery OCR is bounded and is not run repeatedly by default.

### QR processing

QR detection supports camera frames and uploaded images. The implementation
tries `BarcodeDetector` first and falls back to jsQR, including bounded
overlapping crops for multiple codes and rotated/complex photos.

Supported payloads include URL, vCard, MeCard, tel, mailto, WhatsApp, Facebook,
Instagram, Google Maps, App Store, Play Store, known redirect links, and plain
text. QR URLs are classified without executing returned HTML or JavaScript.
Website enrichment is available only after an explicit user action.

Multiple QR results are shown separately for review. Unrelated QR contacts are
not merged automatically.

## Field mapping

Only the existing lead fields are populated:

- `company_name`
- `owner_name`
- `designation`
- `business_description`
- `company_address`
- `website`
- `facebook_link`
- `instagram_link`
- `whatsapp_link`
- `contact_numbers`
- `email_addresses`
- `raw_text`

QR values are routed deterministically:

- WhatsApp URL or WhatsApp phone → `whatsapp_link`
- Facebook URL → `facebook_link`
- Instagram URL → `instagram_link`
- Valid company website → `website`
- vCard/MeCard phone → `contact_numbers`
- vCard/MeCard email → `email_addresses`
- vCard/MeCard company → `company_name`
- vCard/MeCard person name → `owner_name`
- vCard/MeCard address → `company_address`
- Google Maps coordinates → review-required location metadata
- App Store/Play Store links → QR metadata
- Unclassified readable content → `raw_text`

Valid social or WhatsApp URLs must not be placed in `website` or mixed into
company/person fields. Values keep source text, source type, confidence, and a
review flag where available. QR values have priority only when valid and
selected. Conflicts remain visible for correction.

## Save and database behavior

- OCR output is a candidate until the user confirms Save.
- Lead creation uses the existing authenticated API and SQLAlchemy transaction.
- Idempotency keys prevent duplicate saves for the same authenticated user.
- The idempotency record is persistent and user-scoped.
- Failed transactions do not create partial leads.
- Offline leads are queued in IndexedDB and synchronized after reconnect.
- Existing lead fields, routes, authentication, and exports are preserved.
- Excel export has one `Contact Numbers` column containing all deduplicated
  numbers, rather than separate phone columns.

## Performance design

The main speed controls are:

- Decode and preprocess each image once.
- Detect QR before expensive OCR where possible.
- Use mobile PaddleOCR models.
- Reuse initialized engines.
- Keep one CPU OCR job active at a time.
- Avoid automatic multi-pass OCR recovery.
- Poll status with bounded backoff and a timeout.
- Keep browser OCR as an explicit fallback, not the default production path.
- Record `decode_ms`, `preprocess_ms`, `qr_ms`, `ocr_ms`, translation time,
  field-extraction time, queue time, total time, source dimensions, and source
  byte size in `processing_times_ms`.
- Server logs include request ID, OCR engine, model, timing values, image size,
  and dimensions without logging card contents or secrets.

The first production scan can still be slower if PaddleOCR models are being
downloaded or initialized. Provision models during the server build/startup
and use one worker per CPU OCR instance. Multiple workers duplicate model RAM.

## Environment variables

Use deployment secrets for the database and authentication values. Never commit
real passwords, database URLs, or tokens.

Required or important backend values:

```ini
APP_ENV=production
AUTH_REQUIRED=true
DATABASE_URL=postgresql+psycopg://USER:PASSWORD@HOST:5432/DATABASE
CORS_ORIGINS=https://YOUR-FRONTEND-DOMAIN
COOKIE_SAMESITE=none
COOKIE_SECURE=true
SESSION_TTL_HOURS=24
IDEMPOTENCY_TTL_HOURS=48
UPLOAD_DIR=/tmp/uploads
MAX_UPLOAD_BYTES=10485760
MAX_BATCH_BYTES=31457280
MAX_IMAGE_PIXELS=40000000
MAX_PDF_PAGES=5
MAX_OCR_CONCURRENCY=1
PADDLEOCR_ENABLED=true
PADDLEOCR_LANGUAGES=en
PADDLEOCR_DEVICE=cpu
PADDLEOCR_CPU_THREADS=2
PADDLEOCR_DETECTION_MODEL=PP-OCRv5_mobile_det
PADDLEOCR_RECOGNITION_MODEL=en_PP-OCRv5_mobile_rec
PADDLEOCR_INDIC_RECOGNITION_MODEL=devanagari_PP-OCRv5_mobile_rec
PADDLEOCR_DOC_ORIENTATION=true
PADDLEOCR_USE_ANGLE_CLASSIFIER=true
PADDLEOCR_PRELOAD_INDIC=false
TESSERACT_LANGUAGES=eng
WEB_CONCURRENCY=1
```

Optional model directories can be configured with:

```ini
PADDLEOCR_DETECTION_MODEL_DIR=/opt/models/detection
PADDLEOCR_RECOGNITION_MODEL_DIR=/opt/models/english
PADDLEOCR_INDIC_MODEL_DIR=/opt/models/devanagari
```

The frontend should set the production API URL at build time:

```ini
VITE_API_URL=https://YOUR-BACKEND-DOMAIN/api
```

If it is omitted in a production build, the current fallback points to the
configured Render backend. Set it explicitly when using another backend.

## Deployment order

1. Deploy the backend from the repository `main` branch.
2. Set backend environment variables and production database URL.
3. Verify:

   - `GET /api/health` returns `{"status":"ok"}`.
   - `GET /api/ready` returns `{"status":"ready"}`.

4. Provision PaddleOCR models in the backend environment. Use the existing
   `backend/scripts/preload_ocr_models.py` helper where the deployment allows
   persistent model caching.
5. Set the frontend `VITE_API_URL` to the deployed backend `/api` URL.
6. Deploy the frontend.
7. Clear old browser cache/service-worker data after a frontend deployment if
   an old bundle is still displayed.
8. Test login, upload, OCR review, QR scanning, Save, offline queue, and Excel
   export on a real Android and iOS browser.

## Verification commands

From the repository root:

```powershell
cd frontend
npm test
npm run build

cd ..
backend/.venv/Scripts/python.exe -m unittest discover -s backend/tests
backend/.venv/Scripts/python.exe -m compileall -q backend/app backend/scripts
```

The current verified results are:

- Backend tests: 124 passed.
- Frontend QR tests: 22 passed.
- Frontend TypeScript production build: passed.
- Python compile check: passed.

## Evaluation and training status

No new OCR model has been trained. The private evaluation scripts and partial
labels are not a production training dataset. More diverse, permissioned,
fully verified card images and a separate validation set are required before
training or claiming an accuracy improvement.

The existing private evaluation measured ten seed images with partial labels.
Those results are documented in `docs/ocr-card-improvements.md`; they do not
prove production PaddleOCR accuracy, mobile speed, memory usage, or concurrent
throughput. Do not publish the private images, phone numbers, email addresses,
or labels.

## Known limitations and next validation

- PaddleOCR installed-runtime compatibility and model downloads must be tested
  on the deployed server.
- Hindi/Marathi mixed-script accuracy needs real held-out mobile-card testing.
- First-request cold-start time depends on the deployment provider and model
  cache persistence.
- Physical camera focus, permissions, low-light behavior, battery usage, and
  mobile share/download sheets require real devices.
- Website metadata is only a suggestion and requires review.
- OCR can never guarantee exact reading on blurred, obstructed, decorative, or
  very low-resolution cards; uncertain fields remain blank or review-required.

## Current Git handoff

The latest pushed implementation is on the `main` branch at commit:

```text
bece82f Add OCR processing diagnostics
```

The preceding implementation commit is `2f2736b`. The repository contains the
OCR, QR, PaddleOCR, mobile export, authentication, timing, evaluation, and
deployment-readiness changes described above. The next agent should inspect the
working tree and run the verification commands before making new changes.
