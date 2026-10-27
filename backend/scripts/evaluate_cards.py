"""Private OCR regression runner. Run from repository root; never saves images.

python backend/scripts/evaluate_cards.py init --images PATH --manifest private-ocr/manifest.json
python backend/scripts/evaluate_cards.py run --manifest private-ocr/manifest.json --output private-ocr/baseline.json
"""
import argparse
import hashlib
import importlib.metadata
import json
import mimetypes
import os
import platform
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def process_memory():
    if os.name == "nt":
        import ctypes
        class Counters(ctypes.Structure):
            _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong)] + [(name, ctypes.c_size_t) for name in (
                "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        ctypes.windll.kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        handle = ctypes.windll.kernel32.GetCurrentProcess()
        if ctypes.windll.psapi.GetProcessMemoryInfo(ctypes.c_void_p(handle), ctypes.byref(counters), counters.cb):
            return {"peak_working_set": counters.PeakWorkingSetSize, "working_set": counters.WorkingSetSize}
    else:
        import resource
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return {"peak_working_set": peak if sys.platform == "darwin" else peak * 1024}
    return None


def distance(a, b):
    row = list(range(len(b) + 1))
    for i, char in enumerate(a, 1):
        new = [i]
        for j, other in enumerate(b, 1):
            new.append(min(new[-1] + 1, row[j] + 1, row[j - 1] + (char != other)))
        row = new
    return row[-1]


def compare(expected, actual):
    """Score only human-verified labels; missing labels are not successes."""
    exact = {}
    tp = fp = fn = 0
    for field, value in expected.items():
        prediction = actual.get(field)
        def canonical(item):
            if field == "contact_numbers":
                import re
                number = re.sub(r"\D", "", item)
                return "91" + number if len(number) == 10 and number[0] in "6789" else number
            if field == "email_addresses": return item.casefold()
            return item
        if isinstance(value, list):
            prediction = [canonical(item) for item in (prediction or [])]
            value = [canonical(item) for item in value]
        exact[field] = set(value) == set(prediction or []) if isinstance(value, list) else value == prediction
        if isinstance(value, list):
            truth = set(value)
            guess = set(prediction or [])
        else:
            truth = {value} if value else set()
            guess = {prediction} if prediction else set()
        tp += len(truth & guess)
        fp += len(guess - truth)
        fn += len(truth - guess)
    return {"field_exact": exact, "true_positive": tp, "false_positive": fp, "false_negative": fn}


def init_manifest(images):
    from PIL import Image
    samples = []
    for path in sorted(images.iterdir()):
        if path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        with Image.open(path) as image:
            size = list(image.size)
        samples.append({"id": digest[:16], "sha256": digest, "path": str(path.resolve()), "size": size,
                        "group": digest[:16], "split": "test", "scripts": [], "rotation": None,
                        "card_polygons": [], "template": None, "verified_text": None,
                        "expected_fields": {}, "qr_regions": [], "expected_qr": None,
                        "verified": False, "text_regions": [], "unresolved": ["Human transcription required"]})
    return {"schema_version": 1, "samples": samples}


def run_sample(sample):
    from backend.app import ocr, paddle_ocr
    started = time.perf_counter()
    try:
        fields = ocr.extract_fields(Path(sample["path"]), mimetypes.guess_type(sample["path"])[0] or "image/jpeg")
        result = {"id": sample["id"], "status": "ok", "fields": fields,
                  "timings_ms": fields.get("processing_times_ms", {}),
                  "model": paddle_ocr.diagnostics() if hasattr(paddle_ocr, "diagnostics") else {"paddle_available": paddle_ocr.available()}}
        if sample.get("verified"):
            result["metrics"] = compare(sample.get("expected_fields", {}), fields)
            if sample.get("verified_text") is not None:
                truth = sample["verified_text"]
                result["metrics"]["character_error_rate"] = distance(truth, fields.get("original_ocr_text", "")) / max(1, len(truth))
            if sample.get("expected_qr") is not None:
                result["metrics"]["qr_exact"] = set(sample["expected_qr"]) == set(fields.get("qr_payloads", []))
    except Exception as exc:
        result = {"id": sample["id"], "status": "error", "error": str(exc)}
    result["elapsed_ms"] = round((time.perf_counter() - started) * 1000)
    result["process_memory_bytes"] = process_memory()
    return result


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["init", "run", "worker"])
    parser.add_argument("--images", type=Path)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--id")
    parser.add_argument("--timeout", type=int, default=60)
    args = parser.parse_args()
    if args.mode == "init":
        if args.manifest.exists():
            parser.error("Manifest exists; do not overwrite verified labels")
        args.manifest.parent.mkdir(parents=True, exist_ok=True)
        args.manifest.write_text(json.dumps(init_manifest(args.images), indent=2, ensure_ascii=False), encoding="utf-8")
        return
    samples = json.loads(args.manifest.read_text(encoding="utf-8"))["samples"]
    if args.mode == "worker":
        print(json.dumps(run_sample(next(s for s in samples if s["id"] == args.id)), ensure_ascii=False))
        return
    if not args.output:
        parser.error("--output required")
    versions = {}
    for name in ["paddleocr", "paddlepaddle", "opencv-python-headless", "pytesseract", "Pillow"]:
        try: versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError: versions[name] = None
    report = {"conditions": {"platform": platform.platform(), "cpu": platform.processor(), "logical_cpus": os.cpu_count(),
                             "python": sys.version, "versions": versions, "concurrency": 1,
                             "mode": "isolated cold process per image; not warm or load benchmark",
                             "paddle_enabled": os.getenv("PADDLEOCR_ENABLED", "true")}, "results": []}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    for sample in samples:
        try:
            result = subprocess.run([sys.executable, __file__, "worker", "--manifest", str(args.manifest), "--id", sample["id"]],
                                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=args.timeout,
                                    env={**os.environ, "PYTHONIOENCODING": "utf-8"})
            item = json.loads(result.stdout.strip().splitlines()[-1]) if result.returncode == 0 else {"id": sample["id"], "status": "error", "error": result.stderr[-2000:]}
        except (subprocess.TimeoutExpired, ValueError, IndexError) as exc:
            item = {"id": sample["id"], "status": "timeout_or_error", "error": str(exc)}
        report["results"].append(item)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(sample["id"], item["status"], flush=True)


if __name__ == "__main__":
    main()
