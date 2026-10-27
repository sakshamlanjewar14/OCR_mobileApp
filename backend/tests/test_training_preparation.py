import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from PIL import Image
from backend.scripts.prepare_ocr_training import prepare


class TrainingPreparationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.image = self.root / "image.png"
        Image.new("RGB", (100, 50), "white").save(self.image)
        self.sample = {"id": "synthetic", "path": str(self.image), "sha256": hashlib.sha256(self.image.read_bytes()).hexdigest(),
                       "group": "one-business", "split": "train", "text_regions": [
                           {"verified": True, "polygon": [[1, 1], [90, 1], [90, 40], [1, 40]], "transcript": "Example Industries"}]}

    def write(self, samples):
        path = self.root / "manifest.json"
        path.write_text(json.dumps({"samples": samples}), encoding="utf-8")
        return path

    def test_verified_region_exports_crop_and_paddle_label(self):
        result = prepare(self.write([self.sample]), self.root / "dataset")
        self.assertEqual(result["train"], 1)
        self.assertIn("\tExample Industries", (self.root / "dataset/train.txt").read_text())
        self.assertTrue((self.root / "dataset/crops/synthetic-0.png").is_file())

    def test_unverified_predictions_are_not_labels(self):
        self.sample["text_regions"][0]["verified"] = False
        with self.assertRaisesRegex(ValueError, "No verified"):
            prepare(self.write([self.sample]), self.root / "dataset")

    def test_same_card_design_cannot_leak_into_test_split(self):
        duplicate = {**self.sample, "split": "test"}
        with self.assertRaisesRegex(ValueError, "leaks across splits"):
            prepare(self.write([self.sample, duplicate]), self.root / "dataset")


if __name__ == "__main__": unittest.main()
