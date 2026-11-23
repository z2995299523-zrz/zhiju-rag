import hashlib
import io
import os
import tempfile
import unittest
from unittest.mock import patch

from ragcore import ocr
from ragcore.documents import parse_document


def scan_image():
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", (960, 220), "white")
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 44)
        content = "材料预算 2026   1250"
    except OSError:
        font = ImageFont.load_default(size=44)
        content = "Budget 2026   1250"
    draw.text((50, 55), content, font=font, fill="black")
    return image


def image_bytes(image, fmt, **kwargs):
    buffer = io.BytesIO()
    image.save(buffer, format=fmt, **kwargs)
    return buffer.getvalue()


class OcrPolicyTests(unittest.TestCase):
    def test_disabled_image_has_actionable_error(self):
        with patch.dict(os.environ, {"RAG_OCR_ENABLED": "0"}):
            with self.assertRaisesRegex(ValueError, "RAG_OCR_ENABLED"):
                parse_document("invoice.jpg", b"image")

    def test_pdf_guard_precedes_native_render(self):
        with patch.dict(os.environ, {"RAG_OCR_ENABLED": "1"}):
            with self.assertRaisesRegex(ValueError, "拆分"):
                list(ocr.pdf_pages(b"", list(range(1, ocr.MAX_OCR_PAGES + 2))))


@unittest.skipUnless(os.getenv("RAG_TEST_OCR") == "1", "run optional offline OCR fixtures with RAG_TEST_OCR=1")
class OcrAcceptanceTests(unittest.TestCase):
    def setUp(self):
        flag = patch.dict(os.environ, {"RAG_OCR_ENABLED": "1"})
        flag.start()
        self.addCleanup(flag.stop)

    def test_real_png_in_constrained_process(self):
        from fastapi.testclient import TestClient
        from main import create_app

        with scan_image() as image:
            original = image_bytes(image, "PNG")
        config = {"RAG_ADMIN_PASSWORD": "ocr-test", "RAG_COOKIE_SECURE": "0", "RAG_LLM_BASE_URL": "",
                  "RAG_EMBED_BASE_URL": ""}
        with tempfile.TemporaryDirectory() as path, patch.dict(os.environ, config):
            with TestClient(create_app(path, seed=False), base_url="http://127.0.0.1") as client:
                self.assertEqual(client.post("/api/login", json={"password": "ocr-test"}).status_code, 200)
                uploaded = client.post("/api/documents", files={"files": ("budget.png", original)}).json()
                self.assertEqual(uploaded["errors"], [])
                question = "材料预算 2026" if os.path.isfile("C:/Windows/Fonts/msyh.ttc") else "Budget 2026"
                result = client.post("/api/query", json={"question": question, "mode": "documents"}).json()
                self.assertEqual(result["status"], "answered")
                source, = result["citations"]
                self.assertIn("请核对", source["locator"])
                self.assertIn(question.split()[0], source["text"])
                self.assertIn("1250", source["text"])
                self.assertIn(source["id"], result["answer"])
                download = client.get(f"/api/documents/{source['document_id']}/download")
                self.assertEqual(download.content, original)
                self.assertEqual(hashlib.sha256(download.content).hexdigest(), source["sha256"])

    @patch("socket.socket.connect", side_effect=AssertionError("OCR attempted to use the network"))
    def test_real_jpeg_tiff_and_mixed_pdf(self, blocked_socket):
        from pypdf import PdfReader, PdfWriter
        from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

        with scan_image() as image:
            jpeg = parse_document("budget.jpeg", image_bytes(image, "JPEG"))
            self.assertIn("1250", jpeg[0]["text"])
            with image.copy() as copy:
                tiff = parse_document("budget.tiff", image_bytes(image, "TIFF", save_all=True, append_images=[copy]))
            self.assertEqual([e["locator"].split(" · ")[0] for e in tiff], ["第 1 页", "第 2 页"])
            self.assertTrue(all("2026" in e["text"] for e in tiff))
            scan_pdf = image_bytes(image, "PDF")
        scan, = parse_document("scanned.pdf", scan_pdf)
        self.assertIn("1250", scan["text"])
        self.assertIn("OCR", scan["locator"])
        writer = PdfWriter()
        page = writer.add_blank_page(width=500, height=500)
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                                 NameObject("/Subtype"): NameObject("/Type1"),
                                 NameObject("/BaseFont"): NameObject("/Helvetica")})
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
        stream = DecodedStreamObject()
        stream.set_data(b"BT /F1 13 Tf 10 20 Td (Budget approved) Tj ET")
        page[NameObject("/Contents")] = writer._add_object(stream)
        writer.append(PdfReader(io.BytesIO(scan_pdf)))
        output = io.BytesIO()
        writer.write(output)
        native, scanned = parse_document("mixed.pdf", output.getvalue())
        self.assertEqual(native, {"locator": "第 1 页", "text": "Budget approved"})
        self.assertIn("第 2 页", scanned["locator"])
        self.assertIn("OCR", scanned["locator"])
        self.assertIn("1250", scanned["text"])
