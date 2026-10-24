import io
import os
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "sqlite://")

from fastapi import HTTPException
from PIL import Image

from backend.app import main


class _Db:
    def add(self, _value):
        pass


class BackendSafeguardTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.upload_dir = Path(self.directory.name)
        self.addCleanup(self.directory.cleanup)
        self.original_upload_dir = main.UPLOAD_DIR
        main.UPLOAD_DIR = self.upload_dir
        self.addCleanup(setattr, main, "UPLOAD_DIR", self.original_upload_dir)

    def test_rejects_oversized_stream_and_removes_partial_file(self):
        payload = io.BytesIO(b"\x89PNG\r\n\x1a\n" + b"x" * 40)
        with patch.object(main, "MAX_UPLOAD_BYTES", 24):
            with self.assertRaises(HTTPException) as error:
                main._create_document_from_stream(_Db(), "large.png", payload)
        self.assertEqual(error.exception.status_code, 413)
        self.assertEqual(list(self.upload_dir.iterdir()), [])

    def test_rejects_corrupt_image_content(self):
        with self.assertRaises(HTTPException) as error:
            main._create_document_from_stream(_Db(), "broken.png", io.BytesIO(b"\x89PNG\r\n\x1a\nnot-an-image"))
        self.assertEqual(error.exception.status_code, 400)
        self.assertEqual(list(self.upload_dir.iterdir()), [])

    def test_rejects_image_over_pixel_limit(self):
        stream = io.BytesIO()
        Image.new("RGB", (20, 20), "white").save(stream, format="PNG")
        stream.seek(0)
        with patch.object(main, "MAX_IMAGE_PIXELS", 100):
            with self.assertRaises(HTTPException) as error:
                main._create_document_from_stream(_Db(), "large-dimensions.png", stream)
        self.assertEqual(error.exception.status_code, 413)
        self.assertEqual(list(self.upload_dir.iterdir()), [])

    def test_zip_expansion_limits_are_checked_before_extracting(self):
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w") as archive:
            for index in range(26):
                archive.writestr(f"{index}.jpg", b"x")
        payload.seek(0)
        with self.assertRaises(HTTPException) as error:
            main._documents_from_zip(_Db(), "batch.zip", payload)
        self.assertEqual(error.exception.status_code, 413)
        self.assertEqual(list(self.upload_dir.iterdir()), [])

    def test_ocr_capacity_rejects_without_waiting_or_leaking_partial_reservations(self):
        semaphore = threading.BoundedSemaphore(1)
        with patch.object(main, "_ocr_slots", semaphore):
            self.assertTrue(main._reserve_ocr_slots(1))
            self.assertFalse(main._reserve_ocr_slots(1))
            semaphore.release()
            self.assertTrue(main._reserve_ocr_slots(1))
            semaphore.release()

    def test_multi_document_batch_uses_one_reserved_worker_slot(self):
        semaphore = threading.BoundedSemaphore(1)
        processed = []
        with patch.object(main, "_ocr_slots", semaphore), patch.object(
            main, "_process_document_with_request_id", side_effect=lambda document_id, _rid, _started: processed.append(document_id)
        ):
            self.assertTrue(main._reserve_ocr_slots(1))
            main._process_reserved_documents(["first", "second"], "request-id")
            self.assertEqual(processed, ["first", "second"])
            self.assertTrue(semaphore.acquire(blocking=False))
            semaphore.release()


if __name__ == "__main__":
    unittest.main()
