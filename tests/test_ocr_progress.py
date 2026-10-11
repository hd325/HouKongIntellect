"""Offline OCR progress and stop-on-failure contracts; no cloud API calls."""
from __future__ import annotations

import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from services.ocr.tencent_ocr import OCRRecognitionError, TencentOCR, TencentOCRNotConfigured


def response(text: str = ""):
    return SimpleNamespace(TextDetections=[SimpleNamespace(DetectedText=text)] if text else [])


class PdfPage:
    def __init__(self, text: str = "", *, render_error: Exception | None = None):
        self.get_text = Mock(return_value=text)
        self.get_pixmap = Mock(return_value=SimpleNamespace(tobytes=Mock(return_value=b"fake-png")))
        if render_error:
            self.get_pixmap.side_effect = render_error


class PdfDocument:
    def __init__(self, pages):
        self.pages = pages
        self.close = Mock()

    def __iter__(self):
        return iter(self.pages)

    def __len__(self):
        return len(self.pages)


class OCRProgressTests(unittest.TestCase):
    def setUp(self):
        self.ocr = TencentOCR()
        self.client = SimpleNamespace(
            GeneralAccurateOCR=Mock(return_value=response("識別文字")),
            GeneralBasicOCR=Mock(return_value=response("通用識別文字")),
        )
        self.ocr._get_client = Mock(return_value=self.client)
        self.image_patch = patch("services.ocr.tencent_ocr.image_bytes_to_base64", return_value="fake-base64")
        self.image_patch.start()
        self.addCleanup(self.image_patch.stop)

    def pdf_module(self, doc):
        return SimpleNamespace(open=Mock(return_value=doc), Matrix=Mock(return_value="fake-matrix"))

    def test_image_reports_start_and_done_without_a_percentage(self):
        events = []
        text = self.ocr.recognize_image(b"fixture", "image.png", progress_callback=events.append, retry=False)
        self.assertEqual(text, "識別文字")
        self.assertEqual(events, [{"type": "ocr_started", "total": None}, {"type": "ocr_done"}])
        self.client.GeneralAccurateOCR.assert_called_once()
        self.client.GeneralBasicOCR.assert_not_called()

    def test_image_failure_and_empty_result_stop_without_fallback_or_done(self):
        for outcome in (RuntimeError("mock-api-failed"), response(), response("   ")):
            with self.subTest(outcome=type(outcome).__name__):
                self.client.GeneralAccurateOCR.reset_mock()
                self.client.GeneralAccurateOCR.side_effect = outcome if isinstance(outcome, Exception) else None
                self.client.GeneralAccurateOCR.return_value = outcome
                events = []
                with self.assertRaises(OCRRecognitionError):
                    self.ocr.recognize_image(b"fixture", progress_callback=events.append, retry=False)
                self.assertEqual(events, [{"type": "ocr_started", "total": None}])
                self.client.GeneralAccurateOCR.assert_called_once()
                self.client.GeneralBasicOCR.assert_not_called()

    def test_default_image_call_preserves_fallback_and_failure_return(self):
        self.client.GeneralAccurateOCR.side_effect = RuntimeError("mock-api-failed")
        with patch("services.ocr.tencent_ocr.logging.warning"), patch("services.ocr.tencent_ocr.logging.error"):
            self.assertEqual(self.ocr.recognize_image(b"fixture", "legacy.png"), "通用識別文字")
            self.client.GeneralBasicOCR.side_effect = RuntimeError("mock-basic-failed")
            self.assertEqual(self.ocr.recognize_image(b"fixture", "legacy.png"), "")
        self.assertEqual(self.client.GeneralBasicOCR.call_count, 2)

    def test_not_configured_error_is_still_exposed(self):
        self.ocr._get_client.side_effect = TencentOCRNotConfigured("mock-not-configured")
        with self.assertRaises(TencentOCRNotConfigured):
            self.ocr.recognize_image(b"fixture", retry=False)
        self.client.GeneralBasicOCR.assert_not_called()

    def test_pdf_reports_actual_embedded_and_scanned_page_progress_only(self):
        doc = PdfDocument([PdfPage(" 第一頁 "), PdfPage(), PdfPage("第三頁")])
        events = []
        with patch.dict(sys.modules, {"fitz": self.pdf_module(doc)}):
            text = self.ocr.recognize_pdf(b"fixture", "lesson.pdf", progress_callback=events.append, retry=False)
        self.assertEqual(text, "--- 第1页 ---\n第一頁\n\n--- 第2页 ---\n識別文字\n\n--- 第3页 ---\n第三頁")
        self.assertEqual(events, [
            {"type": "ocr_started", "total": 3},
            {"type": "ocr_progress", "completed": 1, "total": 3},
            {"type": "ocr_progress", "completed": 2, "total": 3},
            {"type": "ocr_progress", "completed": 3, "total": 3},
        ])
        doc.close.assert_called_once()
        self.client.GeneralAccurateOCR.assert_called_once()
        self.client.GeneralBasicOCR.assert_not_called()

    def test_pdf_failure_stops_before_later_pages_and_never_reports_complete(self):
        for failure in ("api", "empty", "render", "embedded"):
            with self.subTest(failure=failure):
                self.client.GeneralAccurateOCR.reset_mock()
                self.client.GeneralAccurateOCR.side_effect = RuntimeError("mock-api-failed") if failure == "api" else None
                self.client.GeneralAccurateOCR.return_value = response() if failure == "empty" else response("文字")
                page = PdfPage(render_error=RuntimeError("mock-render-failed") if failure == "render" else None)
                if failure == "embedded":
                    page.get_text.side_effect = RuntimeError("mock-text-failed")
                later = PdfPage("第三頁")
                doc = PdfDocument([PdfPage("第一頁"), page, later])
                events = []
                with patch.dict(sys.modules, {"fitz": self.pdf_module(doc)}):
                    with self.assertRaises(OCRRecognitionError):
                        self.ocr.recognize_pdf(b"fixture", progress_callback=events.append, retry=False)
                self.assertEqual(events, [
                    {"type": "ocr_started", "total": 3},
                    {"type": "ocr_progress", "completed": 1, "total": 3},
                ])
                later.get_text.assert_not_called()
                doc.close.assert_called_once()
                self.client.GeneralBasicOCR.assert_not_called()

    def test_default_pdf_call_preserves_partial_text_after_a_failed_scan(self):
        doc = PdfDocument([PdfPage("第一頁"), PdfPage(), PdfPage("第三頁")])
        self.client.GeneralAccurateOCR.side_effect = RuntimeError("mock-api-failed")
        self.client.GeneralBasicOCR.side_effect = RuntimeError("mock-basic-failed")
        with patch.dict(sys.modules, {"fitz": self.pdf_module(doc)}), \
                patch("services.ocr.tencent_ocr.logging.warning"), patch("services.ocr.tencent_ocr.logging.error"):
            text = self.ocr.recognize_pdf(b"fixture", "legacy.pdf")
        self.assertEqual(text, "--- 第1页 ---\n第一頁\n\n--- 第3页 ---\n第三頁")
        doc.close.assert_called_once()
        self.client.GeneralBasicOCR.assert_called_once()

    def test_invalid_and_empty_pdf_fail_without_fabricated_progress(self):
        events = []
        invalid_module = SimpleNamespace(open=Mock(side_effect=RuntimeError("mock-invalid-pdf")))
        with patch.dict(sys.modules, {"fitz": invalid_module}):
            with self.assertRaises(OCRRecognitionError):
                self.ocr.recognize_pdf(b"fixture", progress_callback=events.append, retry=False)
        self.assertEqual(events, [])
        doc = PdfDocument([])
        with patch.dict(sys.modules, {"fitz": self.pdf_module(doc)}):
            with self.assertRaises(OCRRecognitionError):
                self.ocr.recognize_pdf(b"fixture", progress_callback=events.append, retry=False)
        self.assertEqual(events, [{"type": "ocr_started", "total": 0}])
        doc.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
