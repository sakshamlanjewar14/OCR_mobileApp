# Backend deployment and scan capacity

The API runs in the existing FastAPI/Tesseract stack. It stores uploaded source files under `UPLOAD_DIR` for review, so provision persistent private storage and back up that directory only if the application needs those source cards. There is no external OCR API call. Tesseract is reused by the process; the optional translation model is loaded once per process and can be warmed during startup when `TRANSLATION_MODEL_DIR` points to a persistent local model directory.

## Runtime settings

Set these in the backend environment (the example file is `backend/.env.example`):

| Setting | Default | Purpose |
| --- | ---: | --- |
| `MAX_UPLOAD_BYTES` | `10485760` | Maximum size of one uploaded image or PDF (10 MiB). |
| `MAX_BATCH_BYTES` | `31457280` | Maximum expanded ZIP content (30 MiB). |
| `MAX_IMAGE_PIXELS` | `40000000` | Maximum decoded image pixels. |
| `MAX_PDF_PAGES` | `5` | Maximum pages per PDF. |
| `MAX_OCR_CONCURRENCY` | `1` | Maximum active/accepted upload jobs per process. A batch is one sequential job. When full, the API returns `503` and `Retry-After: 3`. |
| `WEB_CONCURRENCY` | `1` | Uvicorn process count. Keep at one until memory and throughput are measured; every process loads its own translation model and OCR state. |

The repository documents Render as the current hosted backend target. In Render, use a Docker Web Service with `backend/` as the root directory, set `DATABASE_URL` and the exact frontend origin in `CORS_ORIGINS`, set `UPLOAD_DIR=/var/data/uploads`, and attach a persistent disk mounted at `/var/data` because the app retains the original card beside the review form. Set `MAX_OCR_CONCURRENCY=1` and `WEB_CONCURRENCY=1` initially. Configure the service health check to `/api/ready`; Docker's built-in `HEALTHCHECK` uses the lighter `/api/health`. Use an always-on instance for predictable first-scan time if the selected Render service supports it, and set the minimum instance count to one. An always-on service and persistent disk can add hosting charges; check Render's current plan and disk prices before enabling them. Keep the backend private or put it behind an authenticated gateway until application identity is implemented.

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

Keep TLS at the proxy. Add proxy-level authentication appropriate to the product and a shared rate limit for `/api/documents`, `/api/documents/batch`, and `/api/documents/*/retry`. The app is still a single-user MVP without built-in identity; do not expose it publicly without that protection. Avoid using a secret embedded in the PWA because it is visible to every browser user. Limit request bodies at the proxy too, since multipart parsers receive bytes before application-level validation. Use per-user rate limits once authentication is available; per-IP limits behind a proxy can group legitimate users together.

The API returns an `X-Request-ID` response header and logs only request ID, path, status, total request duration, and OCR stage durations. It does not log request bodies, recognized text, names, numbers, emails, or filenames. Match frontend upload timing with `X-Request-ID`; OCR completion logs include that same ID and queue, decode, preprocessing, OCR, translation, extraction, and QR stage timings.

## Sizing and performance validation

Use the current single-process setting as the baseline. Record host CPU model/vCPU, RAM, Python/Tesseract versions and language packs, translation model presence, image dimensions/bytes, first request after restart, warm requests, and 2+ simultaneous uploads. Report throughput, p50/p95 end-to-end scan time, and failures across a representative batch before increasing `MAX_OCR_CONCURRENCY` or `WEB_CONCURRENCY`. Multiple Uvicorn processes duplicate the translation model's memory use. More OCR concurrency can improve throughput when CPU is idle but can increase latency and memory pressure; choose it from measurements, not vCPU count alone. GPU is not enabled because this Tesseract pipeline has no supported GPU path in this application.

This workspace cannot inspect the deployed host's CPU/RAM, reverse proxy, minimum-instance setting, or real mobile network. It also cannot establish representative production p50/p95 results. Those items require production access and a controlled load test. For a low-traffic pilot, one always-on instance with at least 2 vCPU and 4 GiB RAM is a starting point, not a measured guarantee; the optional 418M translation model can materially raise memory needs. Check the host's current pricing before scaling or enabling minimum instances; no paid service is required by these code changes.
