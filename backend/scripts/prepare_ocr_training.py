"""Convert HUMAN-VERIFIED quadrilateral text regions into Paddle recognition data.

No training, dependency downloads, or automatic pseudo-labeling are performed.
Requires Pillow, OpenCV, numpy from the existing server requirements.
"""
import argparse
import hashlib
import json
from pathlib import Path


def prepare(manifest, output):
    import cv2
    import numpy as np
    from PIL import Image, ImageOps
    samples = json.loads(manifest.read_text(encoding="utf-8"))["samples"]
    groups, signatures, records = {}, {}, {"train": [], "val": [], "test": []}
    for sample in samples:
        group = sample["group"]
        # Hash by business/card design, not image. User test seeds stay test.
        bucket = int(hashlib.sha256(group.encode()).hexdigest()[:8], 16) % 100
        split = sample.get("split") or ("train" if bucket < 80 else "val" if bucket < 90 else "test")
        if split not in records: raise ValueError("Unsupported split")
        if group in groups and groups[group] != split: raise ValueError("Card group leaks across splits")
        groups[group] = split
        digest = hashlib.sha256(Path(sample["path"]).read_bytes()).hexdigest()
        if sample.get("sha256") and sample["sha256"] != digest: raise ValueError("Image changed after annotation")
        if digest in signatures and signatures[digest] != split: raise ValueError("Duplicate image leaks across splits")
        signatures[digest] = split
        verified = [region for region in sample.get("text_regions", []) if region.get("verified")]
        if not verified: continue
        with Image.open(sample["path"]) as source:
            image = np.asarray(ImageOps.exif_transpose(source).convert("RGB"))
        for index, region in enumerate(verified):
            text = region.get("transcript", "")
            if not text or any(char in text for char in "\t\r\n"): raise ValueError("Recognition labels must be nonempty single lines")
            polygon = np.asarray(region["polygon"], dtype=np.float32)
            if polygon.shape != (4, 2) or not np.isfinite(polygon).all(): raise ValueError("Need four ordered polygon points")
            if (polygon < 0).any() or (polygon[:, 0] >= image.shape[1]).any() or (polygon[:, 1] >= image.shape[0]).any(): raise ValueError("Polygon outside EXIF-oriented image")
            width = round(max(np.linalg.norm(polygon[1] - polygon[0]), np.linalg.norm(polygon[2] - polygon[3])))
            height = round(max(np.linalg.norm(polygon[3] - polygon[0]), np.linalg.norm(polygon[2] - polygon[1])))
            if min(width, height) < 2: raise ValueError("Degenerate text crop")
            target = np.asarray([[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]], dtype=np.float32)
            crop = cv2.warpPerspective(image, cv2.getPerspectiveTransform(polygon, target), (width, height))
            filename = f"crops/{sample['id']}-{index}.png"
            (output / "crops").mkdir(parents=True, exist_ok=True)
            Image.fromarray(crop).save(output / filename)
            records[split].append(f"{filename}\t{text}")
    if not any(records.values()): raise ValueError("No verified text regions. Annotate polygons and transcripts first; OCR predictions are not ground truth.")
    for split, rows in records.items():
        (output / f"{split}.txt").write_text("\n".join(rows) + ("\n" if rows else ""), encoding="utf-8")
    return {split: len(rows) for split, rows in records.items()}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(prepare(args.manifest, args.output))
