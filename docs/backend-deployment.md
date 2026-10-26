# Backend deployment and scan capacity

The API runs in the existing FastAPI/Tesseract stack. It stores uploaded source files under `UPLOAD_DIR` for review, so provision persistent private storage and back up that directory only if the application needs those source cards. There is no external OCR API call. Tesseract is reused by the process; the optional translation model is loaded once per process and can be warmed during startup when `TRANSLATION_MODEL_DIR` points to a persistent local model directory.

Text-based PDFs now use embedded PDF text directly and invoke OCR only for pages without usable text. This reduces processing time and avoids OCR digit errors for selectable phone numbers, emails, and registered names. Image-only PDFs continue through the existing bounded OCR path.

## Runtime settings

Before deploying the persistent save replay protection, run the forward-only
database migration from the `backend/` directory:

```bash
python scripts/migrate.py
```

It is safe to run more than once. The API startup check also creates the table
when permitted, but running the migration explicitly gives production deploys
an auditable database step.

Set these in the backend environment (the example file is `backend/.env.example`):

| Setting | Default | Purpose |
| --- | ---: | --- |
| `MAX_UPLOAD_BYTES` | `10485760` | Maximum size of one uploaded image or PDF (10 MiB). |
| `MAX_BATCH_BYTES` | `31457280` | Maximum expanded ZIP content (30 MiB). |
| `MAX_IMAGE_PIXELS` | `40000000` | Maximum decoded image pixels. |
| `MAX_PDF_PAGES` | `5` | Maximum pages per PDF. |
| `MAX_OCR_CONCURRENCY` | `1` | Maximum active/accepted upload jobs per process. A batch is one sequential job. When full, the API returns `503` and `Retry-After: 3`. |
| `WEB_CONCURRENCY` | `1` | Uvicorn process count. Keep at one until memory and throughput are measured; every process loads its own translation model and OCR state. |
| `EXPORT_STREAM_THRESHOLD` | `50000` | Lead count at which XLSX export switches to write-only, temporary-file streaming to keep RAM bounded. |
| `AUTH_REQUIRED` | `true` | Require a valid session for lead, document, OCR, import, export, QR, and geocoding APIs. Keep enabled in production. |
| `SESSION_TTL_HOURS` | `24` | Session-cookie lifetime. |
| `RESET_TTL_MINUTES` | `30` | Password-reset token lifetime. |
| `COOKIE_SECURE` | `false` | Set to `true` when the API is served over HTTPS. |

Lead list/export ordering uses `(created_at, id)` indexes and contact lookup uses composite lead/type/position indexes. Exports fetch pages with keyset cursors instead of increasingly expensive SQL offsets. Existing databases install these indexes idempotently at startup; schedule the first index build during a low-traffic window because a large production table may briefly use database resources while an index is created. Exports up to the threshold retain the existing formatting; larger exports stream the same XLSX format through a temporary file and clean it up after the response is sent. Excel itself supports up to 1,048,576 rows per sheet, so a single-sheet export has a practical ceiling of 1,048,575 leads plus the header; split larger exports into batches.

Spreadsheet lead generation sends rows in 1,000-row requests and the API flushes each request in 1,000-row ORM chunks. This keeps mobile request bodies and server identity-map memory bounded while retaining the current review workflow. For a sustained million-row ingestion, use a database-native bulk loader or a queued import job after measuring the deployment; the interactive browser import is intentionally bounded for reliability.

Authentication uses an HttpOnly SameSite session cookie and bcrypt password hashes (with a PBKDF2-SHA256 fallback only when the optional bcrypt package is unavailable in a minimal local environment), with per-user ownership checks on leads and documents. Reset tokens are hashed, single-use, and expiring. Configure an HTTPS email provider around `/api/auth/forgot-password` before production; development responses include a reset token so the flow can be tested locally. Existing legacy rows with no owner remain available only to direct maintenance/test access; newly created records are assigned to the logged-in user.

The Vite development server proxies `/api` to `http://127.0.0.1:8000`, so local cookie authentication stays same-origin and does not depend on browser CORS. Production builds continue using `VITE_API_URL` when it is configured.

The repository documents Render as the current hosted backend target. In Render, use a Docker Web Service with `backend/` as the root directory, set `DATABASE_URL` and the exact frontend origin in `CORS_ORIGINS`, set `UPLOAD_DIR=/var/data/uploads`, and attach a persistent disk mounted at `/var/data` because the app retains the original card beside the review form. Set `MAX_OCR_CONCURRENCY=1` and `WEB_CONCURRENCY=1` initially. Configure the service health check to `/api/ready`; Docker's built-in `HEALTHCHECK` uses the lighter `/api/health`. Use an always-on instance for predictable first-scan time if the selected Render service supports it, and set the minimum instance count to one. An always-on instance and persistent disk can add hosting charges; check Render's current plan and disk prices before enabling them. Keep the backend private or put it behind an authenticated gateway until application identity is implemented.

### Render setup and persistence

1. Create a PostgreSQL database and a Docker Web Service rooted at `backend/`. Set `DATABASE_URL` from the database's internal connection string, `CORS_ORIGINS` to the exact deployed PWA origin (scheme and hostname only), `UPLOAD_DIR=/var/data/uploads`, `MAX_OCR_CONCURRENCY=1`, and `WEB_CONCURRENCY=1`.
2. Attach a private persistent disk mounted at `/var/data`. Uploaded source cards then live at `/var/data/uploads`; without the disk, local-container files can disappear on replacement or redeploy. Keep the disk private because it contains personal contact data. Set an explicit retention and backup policy that matches the review workflow; do not delete files while documents/leads still link to them.
3. The default Docker image includes Tesseract English, Hindi, and Marathi language packs, but it does **not** include the optional M2M100 translation model or its large PyTorch/Transformers runtime. Docker's writable layer is not persistent, so do not place a downloaded model there. To enable translation, add Docker build argument `INSTALL_TRANSLATION=1`, set service environment variable `TRANSLATION_MODEL_DIR=/var/data/models/m2m100_418M`, then run `python scripts/setup_translation.py` once from Render Shell after the disk is mounted. This adds CPU-only PyTorch and `requirements-translation.txt` to the image and downloads model files onto the persistent disk. The model download is large (about 2 GB) and needs outbound access during setup; verify disk space first. Keep the default lightweight image if transliteration fallback meets the use case. The app continues to work without the model.
4. Set the service's health-check path to `/api/ready`; use `/api/health` for liveness if the platform supports separate checks. Keep TLS enabled at the platform edge and never put database, model-download, or gateway credentials in PWA variables.
5. Confirm in the Render dashboard that the disk is mounted and the service has one always-on instance if predictable first scans are required. Persistent disks and always-on compute may cost extra; check current plan and disk pricing before changing the plan. The repository cannot inspect or modify those dashboard settings.

Run `uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-10000} --workers ${WEB_CONCURRENCY:-1} --timeout-keep-alive 10` from `backend/`, or build `backend/Dockerfile`. The Docker image exposes `/api/health` as its liveness check. The load balancer should use `/api/ready` for readiness; it checks both the database and OCR executable. `/api/health` deliberately avoids dependency calls so an unhealthy database does not cause restart loops.

For Nginx with the defaults, set `client_max_body_size 32m` to allow the 30 MiB batch cap plus multipart overhead; each individual file is still capped at 10 MiB by the app. Keep the proxy limit no higher than the host's memory/disk budget. `proxy_request_buffering off` avoids storing the full body at the proxy before forwarding; use a client-body temp directory on persistent fast storage if buffering is required. A 30-second upstream read timeout is sufficient for upload acceptance because OCR continues as a background task and returns `202`; clients poll document status. Do not set a two-minute request timeout to cover OCR.

An Nginx starting point for upload limiting is below. Put `limit_req_zone` inside `http {}` and the `location` inside the API `server {}`. Configure trusted `real_ip` handling first if another load balancer sits in front; never trust arbitrary forwarded client IP headers.

```nginx
limit_req_zone $binary_remote_addr zone=scan_limit:10m rate=10r/m;

location ~ ^/api/documents(?:/batch|/[^/]+/retry)?$ {
    client_max_body_size 32m;
    client_body_timeout 30s;
    limit_req zone=scan_limit burst=5 nodelay;
    limit_req_status 429;
    proxy_request_buffering off;
    proxy_connect_timeout 5s;
    proxy_send_timeout 30s;
    proxy_read_timeout 30s;
    proxy_set_header X-Request-ID $request_id;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_pass http://lead_gennie_backend;
}
```

Keep TLS at the proxy. Add authentication at the gateway for the PWA and API, and a shared rate limit for `/api/documents`, `/api/documents/batch`, and `/api/documents/*/retry`. The app does not implement user identity, sessions, or API authorization, so **a public deployment is not production-secure until an authenticated gateway is configured and direct access to the origin is restricted**. A static API key in frontend code is public to every browser user and is not authentication. Configure trusted proxy IP forwarding before IP-based limits; otherwise all users may share the proxy's address. Per-IP limits can also group legitimate users behind mobile carriers or office NAT. Use per-user limits once authenticated identity is available. Limit request bodies at the proxy too, since multipart parsers receive bytes before application-level validation.

The application rejects a retry unless the scan is in `failed` state and uses a conditional database state update to ensure only one retry request can claim that document, including when requests reach separate workers. A retry with a missing source file returns `410` and asks for a fresh upload. It preserves the source image on scan failure for review/retry. OCR uses in-process FastAPI background tasks: client disconnects after the `202` response do not cancel an accepted scan, while process restarts mark interrupted scans failed and leave source files available. There is no job cancellation API or automatic source-image expiration; adding either requires an explicit product retention/cancellation policy.

The API returns an `X-Request-ID` response header and logs only request ID, path, status, total request duration, and OCR stage durations. It does not log request bodies, recognized text, names, numbers, emails, or filenames. Match frontend upload timing with `X-Request-ID`; OCR completion logs include that same ID and queue, decode, preprocessing, OCR, translation, extraction, and QR stage timings.

## Sizing and performance validation

Use the current single-process setting as the baseline. Record host CPU model/vCPU, RAM, Python/Tesseract versions and language packs, translation model presence, image dimensions/bytes, first request after restart, warm requests, and 2+ simultaneous uploads. Report throughput, p50/p95 end-to-end scan time, and failures across a representative batch before increasing `MAX_OCR_CONCURRENCY` or `WEB_CONCURRENCY`. Multiple Uvicorn processes duplicate the translation model's memory use. More OCR concurrency can improve throughput when CPU is idle but can increase latency and memory pressure; choose it from measurements, not vCPU count alone. GPU is not enabled because this Tesseract pipeline has no supported GPU path in this application.

This workspace cannot inspect the deployed host's CPU/RAM, reverse proxy, minimum-instance setting, or real mobile network. It also cannot establish representative production p50/p95 results. Those items require production access and a controlled load test. For a low-traffic pilot, one always-on instance with at least 2 vCPU and 4 GiB RAM is a starting point, not a measured guarantee; the optional 418M translation model can materially raise memory needs. Check the host's current pricing before scaling or enabling minimum instances; no paid service is required by these code changes.

## Current verification and remaining operator checks

On 2026-10-24, the local backend suite passed 96 tests, the frontend production build passed, and the mobile TypeScript check plus 6 unit tests passed. The previous documented local OCR sample timings are in `docs/ocr-lead-scanner-plan.md`; they are too small and hardware-specific to claim production p50/p95. Existing image checks still leave the Anmol company name blank, the refrigeration card company and owner blank, and Lucky Marine's legal suffix incomplete; these are intentionally reviewable OCR/mapping misses, not confirmed correct values. OCR cannot confidently invent what the source image failed to capture.

Local HTTP endpoint checks, Docker image build, production benchmarking, and physical phone testing could not be completed in this run: the workspace denied localhost socket access and the Docker build lock was inaccessible, and no Render dashboard or Android/iPhone device was connected. After deployment, verify `GET /api/health` and `GET /api/ready`, scan one English, one Hindi, and one Marathi card, and compare every populated field against the image. For a useful performance report, restart the service, capture one cold scan and at least five warm scans per language, then run two concurrent uploads; record request IDs, image dimensions/bytes, model warm state, host vCPU/RAM, end-to-end latency and failures. Do not call the results p50/p95 until the sample is large enough to support those percentiles. On mobile, repeat over Wi-Fi and cellular and check camera permission/focus, upload and review latency. Public traffic remains blocked on your gateway authentication, TLS/origin restriction, and proxy rate/body limits being configured in Render or the chosen edge.
