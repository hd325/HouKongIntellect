"""Offline Huawei general-text contracts: mock SDK, image preparation and sleep."""
from __future__ import annotations

import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from huaweicloudsdkcore.exceptions import exceptions

import tests  # Establish synthetic-only storage before application imports.
from services.ocr.huawei_ocr import HuaweiOCR, HuaweiOCRNotConfigured, OCRRecognitionError

MODULE = "services.ocr.huawei_ocr"


def response(text: str = ""):
    return SimpleNamespace(result=SimpleNamespace(
        words_block_list=[SimpleNamespace(words=text)] if text else [],
    ))


def service_error(status: int, code: str = "OCR.fixture", message: str = "fixture"):
    details = SimpleNamespace(
        error_msg=message, error_code=code, request_id="fixture", encoded_auth_msg=None,
    )
    kind = exceptions.ServerResponseException if status >= 500 else exceptions.ClientRequestException
    return kind(status, details)


class PdfPage:
    rect = type("Rect", (), {"width": 595, "height": 842})()
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
        self.real_get_client = HuaweiOCR._get_client
        self.ocr = HuaweiOCR()
        self.client = SimpleNamespace(recognize_general_text=Mock(return_value=response("識別文字")))
        self.get_client = self.start_patch(patch.object(HuaweiOCR, "_get_client", return_value=self.client))
        self.prepare = self.start_patch(patch(MODULE + ".prepare_image_bytes", return_value=b"prepared-image"))
        self.encode = self.start_patch(patch(MODULE + ".image_bytes_to_base64", return_value="fixture-base64"))
        self.sleep = self.start_patch(patch(MODULE + ".time.sleep"))

    def start_patch(self, patcher):
        mocked = patcher.start()
        self.addCleanup(patcher.stop)
        return mocked

    @staticmethod
    def pdf_module(doc):
        return SimpleNamespace(open=Mock(return_value=doc), Matrix=Mock(return_value="fake-matrix"))

    def test_image_reports_start_and_done_without_a_percentage(self):
        events = []
        text = self.ocr.recognize_image(b"fixture", "image.png", progress_callback=events.append, retry=False)
        self.assertEqual(text, "識別文字")
        self.assertEqual(events, [{"type": "ocr_started", "total": None}, {"type": "ocr_done"}])
        self.client.recognize_general_text.assert_called_once()
        self.prepare.assert_called_once_with(b"fixture")
        self.encode.assert_called_once_with(b"prepared-image")
        self.sleep.assert_not_called()

    def test_image_failure_and_empty_result_stop_without_retry_or_done(self):
        for outcome in (RuntimeError("mock-api-failed"), response(), response("   ")):
            with self.subTest(outcome=type(outcome).__name__):
                self.client.recognize_general_text.reset_mock()
                self.client.recognize_general_text.side_effect = outcome if isinstance(outcome, Exception) else None
                self.client.recognize_general_text.return_value = outcome
                events = []
                with self.assertRaises(OCRRecognitionError):
                    self.ocr.recognize_image(b"fixture", progress_callback=events.append, retry=False)
                self.assertEqual(events, [{"type": "ocr_started", "total": None}])
                self.client.recognize_general_text.assert_called_once()
                self.sleep.assert_not_called()

    def test_transient_network_5xx_and_429_retry_once_then_succeed(self):
        errors = [ConnectionError("fixture-network"), TimeoutError("fixture-timeout"),
                  exceptions.ConnectionException("fixture-sdk-network"),
                  service_error(500), service_error(503), service_error(429)]
        for error in errors:
            with self.subTest(error=type(error).__name__, status=getattr(error, "status_code", None)):
                self.client.recognize_general_text.reset_mock()
                self.sleep.reset_mock()
                self.client.recognize_general_text.side_effect = [error, response("重試成功")]
                events = []
                self.assertEqual(self.ocr.recognize_image(b"fixture", progress_callback=events.append), "重試成功")
                self.assertEqual(self.client.recognize_general_text.call_count, 2)
                self.sleep.assert_called_once_with(1)
                self.assertEqual(events, [{"type": "ocr_started", "total": None}, {"type": "ocr_done"}])

    def test_two_transient_failures_return_empty_without_done(self):
        self.client.recognize_general_text.side_effect = [ConnectionError("first"), service_error(503)]
        events = []
        self.assertEqual(self.ocr.recognize_image(b"fixture", progress_callback=events.append), "")
        self.assertEqual(self.client.recognize_general_text.call_count, 2)
        self.sleep.assert_called_once_with(1)
        self.assertEqual(events, [{"type": "ocr_started", "total": None}])

    def test_retry_false_never_retries_a_transient_failure(self):
        self.client.recognize_general_text.side_effect = service_error(429)
        with self.assertRaises(OCRRecognitionError):
            self.ocr.recognize_image(b"fixture", retry=False)
        self.client.recognize_general_text.assert_called_once()
        self.sleep.assert_not_called()

    def test_ordinary_4xx_never_retry(self):
        for status in (400, 404, 408, 422):
            with self.subTest(status=status):
                self.client.recognize_general_text.reset_mock()
                self.client.recognize_general_text.side_effect = service_error(status)
                self.assertEqual(self.ocr.recognize_image(b"fixture"), "")
                self.client.recognize_general_text.assert_called_once()
                self.sleep.assert_not_called()

    def test_unknown_local_programming_error_never_retries(self):
        self.client.recognize_general_text.side_effect = TypeError("fixture-local-bug")
        self.assertEqual(self.ocr.recognize_image(b"fixture"), "")
        self.client.recognize_general_text.assert_called_once()
        self.sleep.assert_not_called()

    def test_fatal_401_403_and_modelarts_stop_immediately(self):
        for status, code in ((401, "OCR.fixture"), (403, "OCR.fixture"), (400, "ModelArts.4204")):
            with self.subTest(status=status, code=code):
                self.client.recognize_general_text.reset_mock()
                self.client.recognize_general_text.side_effect = service_error(status, code)
                events = []
                with self.assertRaises(OCRRecognitionError) as caught:
                    self.ocr.recognize_image(b"fixture", progress_callback=events.append)
                self.assertEqual(caught.exception.status_code, status)
                self.assertEqual(caught.exception.error_code, code)
                self.client.recognize_general_text.assert_called_once()
                self.sleep.assert_not_called()
                self.assertEqual(events, [{"type": "ocr_started", "total": None}])

    def test_not_configured_error_is_still_exposed(self):
        self.get_client.side_effect = HuaweiOCRNotConfigured("mock-not-configured")
        with self.assertRaises(HuaweiOCRNotConfigured):
            self.ocr.recognize_image(b"fixture", retry=False)
        self.client.recognize_general_text.assert_not_called()
        self.sleep.assert_not_called()

    def test_sdk_request_detects_direction_and_preserves_block_order(self):
        self.client.recognize_general_text.return_value = SimpleNamespace(result=SimpleNamespace(
            words_block_list=[SimpleNamespace(words="第一行"), SimpleNamespace(words=""),
                              SimpleNamespace(), SimpleNamespace(words="第二行")],
        ))
        self.assertEqual(self.ocr.recognize_image(b"fixture", retry=False), "第一行\n第二行")
        request = self.client.recognize_general_text.call_args.args[0]
        self.assertEqual(request.body.image, "fixture-base64")
        self.assertIs(request.body.detect_direction, True)

    def test_pdf_reports_actual_embedded_and_scanned_page_progress_only(self):
        doc = PdfDocument([PdfPage(" 第一頁 "), PdfPage(), PdfPage("第三頁")])
        events = []
        with patch.dict(sys.modules, {"pymupdf": self.pdf_module(doc)}):
            text = self.ocr.recognize_pdf(b"fixture", "lesson.pdf", progress_callback=events.append, retry=False)
        self.assertEqual(text, "--- 第1页 ---\n第一頁\n\n--- 第2页 ---\n識別文字\n\n--- 第3页 ---\n第三頁")
        self.assertEqual(events, [
            {"type": "ocr_started", "total": 3},
            {"type": "ocr_progress", "completed": 1, "total": 3},
            {"type": "ocr_progress", "completed": 2, "total": 3},
            {"type": "ocr_progress", "completed": 3, "total": 3},
        ])
        doc.close.assert_called_once()
        self.client.recognize_general_text.assert_called_once()

    def test_text_only_pdf_never_renders_or_calls_ocr(self):
        pages = [PdfPage("第一頁"), PdfPage("第二頁")]
        doc = PdfDocument(pages)
        with patch.dict(sys.modules, {"pymupdf": self.pdf_module(doc)}):
            self.assertEqual(self.ocr.recognize_pdf(b"fixture"),
                             "--- 第1页 ---\n第一頁\n\n--- 第2页 ---\n第二頁")
        for page in pages:
            page.get_pixmap.assert_not_called()
        self.get_client.assert_not_called()
        self.client.recognize_general_text.assert_not_called()
        self.prepare.assert_not_called()
        self.encode.assert_not_called()
        doc.close.assert_called_once()

    def test_pdf_failure_stops_before_later_pages_and_never_reports_complete(self):
        for failure in ("api", "empty", "render", "embedded"):
            with self.subTest(failure=failure):
                self.client.recognize_general_text.reset_mock()
                self.client.recognize_general_text.side_effect = RuntimeError("mock-api-failed") if failure == "api" else None
                self.client.recognize_general_text.return_value = response() if failure == "empty" else response("文字")
                page = PdfPage(render_error=RuntimeError("mock-render-failed") if failure == "render" else None)
                if failure == "embedded":
                    page.get_text.side_effect = RuntimeError("mock-text-failed")
                later = PdfPage("第三頁")
                doc = PdfDocument([PdfPage("第一頁"), page, later])
                events = []
                with patch.dict(sys.modules, {"pymupdf": self.pdf_module(doc)}):
                    with self.assertRaises(OCRRecognitionError):
                        self.ocr.recognize_pdf(b"fixture", progress_callback=events.append, retry=False)
                self.assertEqual(events, [{"type": "ocr_started", "total": 3},
                                          {"type": "ocr_progress", "completed": 1, "total": 3}])
                later.get_text.assert_not_called()
                doc.close.assert_called_once()
                self.sleep.assert_not_called()

    def test_default_pdf_preserves_partial_text_and_counts_successes_after_failed_scan(self):
        doc = PdfDocument([PdfPage("第一頁"), PdfPage(), PdfPage("第三頁")])
        self.client.recognize_general_text.side_effect = [ConnectionError("first"), ConnectionError("second")]
        events = []
        with patch.dict(sys.modules, {"pymupdf": self.pdf_module(doc)}):
            text = self.ocr.recognize_pdf(b"fixture", "lesson.pdf", progress_callback=events.append)
        self.assertEqual(text, "--- 第1页 ---\n第一頁\n\n--- 第3页 ---\n第三頁")
        self.assertEqual(events, [{"type": "ocr_started", "total": 3},
                                  {"type": "ocr_progress", "completed": 1, "total": 3},
                                  {"type": "ocr_progress", "completed": 2, "total": 3}])
        doc.close.assert_called_once()
        self.assertEqual(self.client.recognize_general_text.call_count, 2)
        self.sleep.assert_called_once_with(1)

    def test_default_pdf_skips_failed_local_page_without_network_retry(self):
        broken = PdfPage()
        broken.get_text.side_effect = TypeError("fixture-text-layer-failed")
        doc = PdfDocument([broken, PdfPage("第二頁")])
        events = []
        with patch.dict(sys.modules, {"pymupdf": self.pdf_module(doc)}):
            self.assertEqual(self.ocr.recognize_pdf(b"fixture", progress_callback=events.append),
                             "--- 第2页 ---\n第二頁")
        self.assertEqual(events, [{"type": "ocr_started", "total": 2},
                                  {"type": "ocr_progress", "completed": 1, "total": 2}])
        self.get_client.assert_not_called()
        self.sleep.assert_not_called()
        doc.close.assert_called_once()

    def test_pdf_fatal_errors_do_not_continue_to_later_pages(self):
        for status, code in ((401, "OCR.fixture"), (403, "OCR.fixture"), (400, "ModelArts.4204")):
            with self.subTest(status=status, code=code):
                self.client.recognize_general_text.reset_mock()
                self.client.recognize_general_text.side_effect = service_error(status, code)
                later = PdfPage("第三頁")
                doc = PdfDocument([PdfPage("第一頁"), PdfPage(), later])
                events = []
                with patch.dict(sys.modules, {"pymupdf": self.pdf_module(doc)}):
                    with self.assertRaises(OCRRecognitionError):
                        self.ocr.recognize_pdf(b"fixture", progress_callback=events.append)
                later.get_text.assert_not_called()
                self.client.recognize_general_text.assert_called_once()
                self.sleep.assert_not_called()
                self.assertEqual(events, [{"type": "ocr_started", "total": 3},
                                          {"type": "ocr_progress", "completed": 1, "total": 3}])
                doc.close.assert_called_once()

    def test_invalid_and_empty_pdf_fail_without_fabricated_progress(self):
        events = []
        invalid_module = SimpleNamespace(open=Mock(side_effect=RuntimeError("mock-invalid-pdf")))
        with patch.dict(sys.modules, {"pymupdf": invalid_module}):
            with self.assertRaises(OCRRecognitionError):
                self.ocr.recognize_pdf(b"fixture", progress_callback=events.append, retry=False)
        self.assertEqual(events, [])
        doc = PdfDocument([])
        with patch.dict(sys.modules, {"pymupdf": self.pdf_module(doc)}):
            with self.assertRaises(OCRRecognitionError):
                self.ocr.recognize_pdf(b"fixture", progress_callback=events.append, retry=False)
        self.assertEqual(events, [{"type": "ocr_started", "total": 0}])
        doc.close.assert_called_once()
        self.get_client.assert_not_called()

    def test_image_preparation_failure_never_calls_sdk(self):
        self.prepare.side_effect = ValueError("fixture-invalid-image")
        self.assertEqual(self.ocr.recognize_image(b"fixture"), "")
        with self.assertRaises(OCRRecognitionError):
            self.ocr.recognize_image(b"fixture", retry=False)
        self.encode.assert_not_called()
        self.get_client.assert_not_called()
        self.sleep.assert_not_called()

    def test_logs_omit_exception_original_text_and_request_base64(self):
        marker = "DO_NOT_LOG_NETWORK_EXCEPTION_TEXT"
        self.client.recognize_general_text.side_effect = RuntimeError(marker + " fixture-base64")
        with self.assertLogs(level="WARNING") as captured:
            self.assertEqual(self.ocr.recognize_image(b"fixture", "safe-fixture.png"), "")
        logged = "\n".join(captured.output)
        self.assertNotIn(marker, logged)
        self.assertNotIn("fixture-base64", logged)
        for field in ("status_code=", "error_code=", "error_msg=", "filename=safe-fixture.png"):
            self.assertIn(field, logged)

    def test_sdk_error_message_redacts_mock_credentials_and_base64(self):
        fake_ak, fake_sk, fake_b64 = "MOCK_AK_MARKER", "MOCK_SK_MARKER", "MOCK_BASE64_MARKER"
        self.encode.return_value = fake_b64
        self.client.recognize_general_text.side_effect = service_error(
            400, message=f"fixture {fake_ak} {fake_sk} {fake_b64}",
        )
        with patch(MODULE + ".HUAWEI_AK", fake_ak), patch(MODULE + ".HUAWEI_SK", fake_sk), \
                self.assertLogs(level="WARNING") as captured:
            self.assertEqual(self.ocr.recognize_image(b"fixture", "safe-fixture.png"), "")
        logged = "\n".join(captured.output)
        for marker in (fake_ak, fake_sk, fake_b64):
            self.assertNotIn(marker, logged)
        self.assertIn("status_code=400", logged)
        self.assertIn("error_code=OCR.fixture", logged)

    def test_first_request_project_id_error_stops_image_and_pdf(self):
        self.client.recognize_general_text.side_effect = ValueError("project_id is required")
        for retry in (True, False):
            with self.subTest(retry=retry):
                self.client.recognize_general_text.reset_mock()
                events = []
                with self.assertRaises(HuaweiOCRNotConfigured):
                    self.ocr.recognize_image(b"fixture", retry=retry, progress_callback=events.append)
                self.assertEqual(events, [{"type": "ocr_started", "total": None}])
                self.client.recognize_general_text.assert_called_once()
                self.sleep.assert_not_called()
        later = PdfPage("第二頁")
        doc = PdfDocument([PdfPage(), later])
        with patch.dict(sys.modules, {"pymupdf": self.pdf_module(doc)}):
            with self.assertRaises(HuaweiOCRNotConfigured):
                self.ocr.recognize_pdf(b"fixture")
        later.get_text.assert_not_called()
        doc.close.assert_called_once()

    def test_missing_required_project_id_stops_image_and_pdf_with_configuration_error(self):
        builder = Mock()
        builder.with_credentials.return_value = builder
        builder.with_region.return_value = builder
        builder.build.side_effect = ValueError("project_id required")
        later = PdfPage("第二頁")
        doc = PdfDocument([PdfPage(), later])
        with patch(MODULE + ".is_huawei_ocr_configured", return_value=True), \
                patch(MODULE + ".HUAWEI_AK", "fixture-ak"), \
                patch(MODULE + ".HUAWEI_SK", "fixture-sk"), \
                patch(MODULE + ".HUAWEI_PROJECT_ID", ""), \
                patch(MODULE + ".BasicCredentials") as credentials, \
                patch(MODULE + ".OcrClient.new_builder", return_value=builder), \
                patch.object(self.ocr, "_get_client", side_effect=lambda: self.real_get_client(self.ocr)):
            with self.assertRaises(HuaweiOCRNotConfigured):
                self.ocr.recognize_image(b"fixture")
            credentials.assert_called_once_with("fixture-ak", "fixture-sk")
            with patch.dict(sys.modules, {"pymupdf": self.pdf_module(doc)}):
                with self.assertRaises(HuaweiOCRNotConfigured):
                    self.ocr.recognize_pdf(b"fixture")
        self.sleep.assert_not_called()
        self.client.recognize_general_text.assert_not_called()
        later.get_text.assert_not_called()
        doc.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
