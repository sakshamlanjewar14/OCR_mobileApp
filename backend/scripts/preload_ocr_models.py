"""Provision inference model cache outside the scan path. No training performed."""
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend.app import paddle_ocr

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--indic", action="store_true")
    args = parser.parse_args()
    paddle_ocr.get_engine()
    if args.indic: paddle_ocr.get_engine("devanagari")
    print(json.dumps(paddle_ocr.diagnostics(), indent=2))
