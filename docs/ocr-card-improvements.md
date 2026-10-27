# Business-card OCR and QR evaluation

## What changed

- Corrected browser vCard/MeCard handling: folded lines, parameterized TEL/EMAIL,
  repeated contacts, escaped separators and complete addresses.
- QR URLs retain query strings. Company, social, WhatsApp, map, redirect and app
  links are classified separately. Coordinates never become an invented address.
- Eight bounded overlapping crops recover both codes on the supplied salon
  card. Native camera detection falls back locally on exceptions or a timeout.
- Multiple codes produce a selection; unrelated contacts are not combined.
- Card uploads continue OCR after QR detection. Use **Scan QR → Choose QR image**
  for an immediate QR-only review without uploading or starting text OCR.
- Website retrieval is explicit. Public organization metadata and contact links
  fill missing values; existing card values remain intact. A page title is not
  treated as a verified company name. Website suggestions require review.
- Fixed Paddle result adapters for NumPy arrays, rectangular boxes, and the
  legacy `[polygon, (text, confidence)]` result shape. These previously could
  cause exceptions or empty text and silently select the fallback engine.
- Select explicit PP-OCRv5 mobile models, cache engines by script, serialize
  inference and expose model initialization timings in extraction metadata.
  A bounded Indic recovery attempt can now use Paddle's Devanagari model.
- Fixed double-escaped quality-check regexes. Existing EXIF, image-size limits,
  bounded orientation recovery and original preview remain in place.
- Unverified Hindi/Marathi-to-English conversions stay unresolved with the
  original text preserved. Transliteration is not represented as verified English.
- Preserve source and review metadata and report disagreements between QR and OCR.
- Existing authentication, database fields, review-before-save, idempotency,
  and the single **Contact Numbers** Excel column remain intact.

## Local verification and limits

The ten provided images were read without modifying the originals. Private
manifest, baseline, after-results, transcriptions and QR evidence are under
`private-ocr/`, which is ignored by Git. Neither images nor contact labels should
be published. The manifest contains visually verified selected fields, not a full
text-region annotation. All ten seed cards remain held out as `test` data.

Conditions: Windows 11 (reported platform build 26200), Python 3.11, Intel family
6 model 140, 8 logical CPUs, approximately 7.76 GiB physical RAM. OpenCV 4.14,
Pillow 11.3, pytesseract 0.3.13. PaddleOCR/PaddlePaddle are **not installed in the
local environment** and there is no Tesseract executable on PATH. The measured
OCR engine is Windows OCR, not the deployed Paddle engine. No production server
or physical mobile camera benchmark was performed.

Each image ran in a fresh process, sequentially, with a 35-second limit. Timings
are extraction-call wall time; worker launch overhead is excluded. This is not
a simultaneous-user benchmark. Operating-system/runtime caching and other local
activity were not controlled, so lower after-times do not establish a Paddle or
production speed improvement.

| Sample ID | Before ms | After ms |
| --- | ---: | ---: |
| 933eca4cb3777f30 | 3758 | 3019 |
| 2a512c5a8e06bc79 | 1226 | 1666 |
| f83aa04c14b283e5 | 891 | 1552 |
| 851d2f127c9d4b2c | 2144 | 1192 |
| b4783d67eb93d6ab | 2875 | 2386 |
| 31b5bd224d860622 | 2584 | 1701 |
| d0c8db5560eeaade | 2447 | 1762 |
| 301190589a7448a7 | 2106 | 1671 |
| 61b88c93f0ad527a | 1612 | 1724 |
| 10a91522c9b21090 | 2512 | 1754 |

Median: 2295.5 → 1712.5 ms. Nearest-rank p95 for only ten samples equals the
maximum: 3758 → 3019 ms. Technical processing completed for 10/10 in both runs;
this does **not** mean all text was correctly read.

Selected-field exact checks: 25/30 before and after. Phone formatting/country
prefix equivalence and email case are normalized; other values are compared
exactly. Across the annotated nonempty values, precision is 0.844 and recall
0.900 in both runs. These narrow, partial-label results are not overall OCR
accuracy. Unannotated fields are not counted as correct. Full transcription,
character error rate, reliable address completeness, and translation accuracy
cannot be measured until the unresolved annotations are completed.

QR verification on sample 61b88c93f0ad527a: baseline recovered one code; the updated
server and browser decoder strategies recovered the same two exact payloads.
No claim is made that the redirect destinations expose company contact details.

The evaluation runner now records peak process working set using platform APIs.
Earlier before/after files predate that instrumentation and have no peak-RAM
comparison. Warm-worker latency, production model RAM, concurrency throughput,
load p95, device battery impact and cold deployment starts remain unmeasured.

## Reproduce evaluation

From the repository root, using the server Python environment:

```powershell
backend/.venv/Scripts/python.exe backend/scripts/evaluate_cards.py init --images C:/path/to/cards --manifest private-ocr/manifest.json
backend/.venv/Scripts/python.exe backend/scripts/evaluate_cards.py run --manifest private-ocr/manifest.json --output private-ocr/results.json --timeout 60
```

`init` refuses to overwrite an existing annotated manifest. Review every label
against the image and set `verified=true` only for reviewed expected fields.
`verified_text=null` means full-text accuracy is unmeasured. Image IDs are hashes.
Keep the same `group` for the same company/card design, both sides, duplicate
photos and augmented variants. Do not move these test seeds into training.

## Deployment and model provisioning

Keep existing production authentication, CORS, database and storage variables.
The relevant OCR values are:

```ini
PADDLEOCR_ENABLED=true
PADDLEOCR_LANGUAGES=en
PADDLEOCR_DEVICE=cpu
PADDLEOCR_DETECTION_MODEL=PP-OCRv5_mobile_det
PADDLEOCR_RECOGNITION_MODEL=en_PP-OCRv5_mobile_rec
PADDLEOCR_INDIC_RECOGNITION_MODEL=devanagari_PP-OCRv5_mobile_rec
PADDLEOCR_CPU_THREADS=2
PADDLEOCR_DOC_ORIENTATION=true
PADDLEOCR_USE_ANGLE_CLASSIFIER=true
PADDLEOCR_PRELOAD_INDIC=false
MAX_OCR_CONCURRENCY=1
WEB_CONCURRENCY=1
TESSERACT_LANGUAGES=eng
```

The mobile-named models run on the **server** in this app. Hindi/Marathi
recognition still needs validation on the actual installed model and held-out
samples. Script routing is a bounded heuristic, not a proven script classifier.
An English recognizer cannot prove that a card contains no Devanagari text.
For a dedicated Indic deployment, `PADDLEOCR_LANGUAGES=devanagari` selects the
Indic primary; compare mixed-script results before choosing a production default.

Provision inference models on the server build/setup environment:

```bash
python scripts/preload_ocr_models.py --indic
```

Persist the configured Paddle cache across deployments, or set local exported
model directories with `PADDLEOCR_DETECTION_MODEL_DIR`,
`PADDLEOCR_RECOGNITION_MODEL_DIR`, and `PADDLEOCR_INDIC_MODEL_DIR`. This avoids
model downloads during a user's scan. Set `PADDLEOCR_PRELOAD_INDIC=true` only
after measuring that both pipelines fit the instance memory. Each process
duplicates model memory. Model provisioning was not executed locally.

Do not claim a deployment is ready merely because an engine constructor loads.
Run English, Devanagari, rotated-card and QR smoke tests on the actual image,
inspect the reported `ocr_engine` and `ocr_models`, and verify fallback behavior.
Redeploy frontend and backend together after review. No dashboard deployment was
performed by this work.

## Training preparation

No model was trained. These ten partially annotated cards cannot establish a
generalizable fine-tuned model. More diverse verified real-card samples, full
text-line crops and a separate validation set are required. Use only images
you have permission to use; stock-watermarked samples stay local evaluation
examples until their training rights are established.

Each private manifest sample may contain:

```json
{
  "id": "synthetic-example",
  "path": "/private/cards/example.png",
  "group": "one-business-and-card-design",
  "split": "train",
  "text_regions": [{
    "polygon": [[10,10],[200,10],[200,45],[10,45]],
    "transcript": "Example Industries",
    "verified": true
  }]
}
```

Polygons are in the EXIF-oriented original image, ordered by text reading
orientation: top-left, top-right, bottom-right, bottom-left. The converter
perspective-rectifies these verified text regions for training. It does not
automatically crop production cards from unverified polygons.

```bash
python backend/scripts/prepare_ocr_training.py --manifest private-ocr/manifest.json --output private-ocr/recognition
```

The converter checks image hashes, bounds, transcripts, group/duplicate leakage,
and refuses to use unverified predictions as labels. It intentionally rejects
the current seed manifest because no text-region polygons have been verified.

Use a **separate training environment**, a recorded official PaddleOCR source
revision, its matching dependencies, official recognition configuration,
character dictionary and matching pretrained weights. This project does not
install training dependencies or rent hardware. Training may require substantial
RAM/time or a GPU; no paid services are required or provisioned by these scripts.
Do not change the character dictionary casually: weights and output dimensions
must match. Keep English and Devanagari datasets/configurations separate where
the recognizers use different dictionaries. Use the official config's realistic
blur, lighting, scale and geometric augmentation on training data only.

Generate commands first (no training happens without `--execute`):

```bash
python backend/scripts/paddle_training_command.py train --repo /training/PaddleOCR --config /training/official-recognition.yml --weights /training/pretrained.pdparams --data private-ocr/recognition --output private-ocr/checkpoints
python backend/scripts/paddle_training_command.py eval --repo /training/PaddleOCR --config /training/official-recognition.yml --weights private-ocr/checkpoints/best_accuracy.pdparams --data private-ocr/recognition --output private-ocr/evaluation
python backend/scripts/paddle_training_command.py export --repo /training/PaddleOCR --config /training/official-recognition.yml --weights private-ocr/checkpoints/best_accuracy.pdparams --data private-ocr/recognition --output private-ocr/inference
```

Paths above identify operator-provided files, not bundled/trained artifacts.
Use `--execute` only once the data, source version, dependencies and compute are
ready. Train detection only if verified text polygons show detection failures;
current local fallback results cannot establish Paddle detector failures.
Deploy exported inference models only after held-out exact contact accuracy,
CER by script, memory, warm/cold latency and load results improve acceptably.
Retain the previous model paths for rollback.

Official references:
- [General OCR model configuration](https://github.com/PaddlePaddle/PaddleOCR/blob/main/docs/version3.x/pipeline_usage/OCR.en.md)
- [Recognition models and training](https://github.com/PaddlePaddle/PaddleOCR/blob/main/docs/version3.x/module_usage/text_recognition.en.md)

## Automated checks

```bash
cd frontend
node --test tests/qr*.test.cjs
npm run build
cd ..
backend/.venv/Scripts/python.exe -m unittest discover -s backend/tests
```

Tests cover QR parsing parity, unsafe/unknown payloads, location/app separation,
multiple-code selection, delayed/hung detector handling, Paddle result shapes,
Indic routing with mocked inference, conflicting QR values, missing translation,
verified crop export, dataset split leakage, and explicit website metadata.
Mocked Paddle tests do not prove installed-runtime compatibility or language
accuracy. Real mobile camera permissions/focus, downloads/share sheets and
production load still require device/deployment verification.
