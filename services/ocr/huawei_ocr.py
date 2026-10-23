"""華為雲 general-text OCR；PDF 優先在本地提取文字層。"""
import logging
import time
from typing import Callable

from huaweicloudsdkcore.auth.credentials import BasicCredentials
from huaweicloudsdkcore.exceptions import exceptions
from huaweicloudsdkocr.v1 import OcrClient, RecognizeGeneralTextRequest, GeneralTextRequestBody
from huaweicloudsdkocr.v1.region.ocr_region import OcrRegion
from requests.exceptions import ConnectionError as RequestsConnectionError, Timeout

from config.settings import (
    HUAWEI_AK, HUAWEI_SK, HUAWEI_PROJECT_ID, HUAWEI_OCR_REGION,
    is_huawei_ocr_configured,
)
from services.resource_limits import validate_pdf, ParserLimitError
from utils.image_utils import image_bytes_to_base64, prepare_image_bytes


class HuaweiOCRNotConfigured(Exception):
    """華為雲 OCR 未配置。"""


class OCRRecognitionError(RuntimeError):
    """文字識別失敗；只向呼叫方暴露安全消息與服務錯誤碼。"""

    def __init__(self, message: str, *, status_code=None, error_code=None):
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code


_NETWORK_ERRORS = (
    ConnectionError, TimeoutError, RequestsConnectionError, Timeout,
    exceptions.ConnectionException, exceptions.RequestTimeoutException,
    exceptions.HostUnreachableException, exceptions.SslHandShakeException,
    exceptions.CallTimeoutException, exceptions.RetryOutageException,
)


class HuaweiOCR:
    """固定使用北京四的通用文字識別，暫時性錯誤最多重試一次。"""

    def __init__(self, region: str = HUAWEI_OCR_REGION):
        if region != "cn-north-4":
            raise ValueError("華為雲 OCR 區域必須為 cn-north-4")
        self.region = region
        self._client = None

    def _get_client(self) -> OcrClient:
        if not is_huawei_ocr_configured():
            raise HuaweiOCRNotConfigured(
                "華為雲 OCR 未配置：請在本機 .env 填入 HUAWEI_AK / HUAWEI_SK"
            )
        if self._client is None:
            cred = (BasicCredentials(HUAWEI_AK, HUAWEI_SK, HUAWEI_PROJECT_ID)
                    if HUAWEI_PROJECT_ID else BasicCredentials(HUAWEI_AK, HUAWEI_SK))
            try:
                self._client = (OcrClient.new_builder()
                                .with_credentials(cred)
                                .with_region(OcrRegion.value_of(self.region))
                                .build())
            except Exception as error:
                # SDK 若不能按區域自動解析 project_id，停止而非吞錯或猜值。
                self._check_project_id_error(error)
                raise
        return self._client

    @staticmethod
    def _status(error):
        try:
            return int(getattr(error, "status_code", 0) or 0)
        except (TypeError, ValueError):
            return 0

    @classmethod
    def _is_fatal(cls, error) -> bool:
        return (cls._status(error) in (401, 403)
                or getattr(error, "error_code", None) == "ModelArts.4204")

    @classmethod
    def _is_transient(cls, error) -> bool:
        status = cls._status(error)
        if status:
            return status == 429 or 500 <= status < 600
        return isinstance(error, _NETWORK_ERRORS)

    @classmethod
    def _check_project_id_error(cls, error):
        # 憑證解析可能延至首次請求；檢查錯誤但不記錄原始異常。
        message = str(error).lower().replace("_", "").replace(" ", "")
        if (not HUAWEI_PROJECT_ID and "projectid" in message
                and not cls._is_fatal(error) and not cls._is_transient(error)):
            raise HuaweiOCRNotConfigured(
                "SDK 無法自動解析 project_id，請提供「我的憑證 → API 憑證 → 華北-北京四」的 project_id"
            ) from None

    @classmethod
    def _recognition_error(cls, error) -> OCRRecognitionError:
        status = cls._status(error)
        code = getattr(error, "error_code", None)
        if code == "ModelArts.4204":
            message = "華為雲北京四通用文字識別未開通或已關停，請檢查服務狀態"
        elif status == 401:
            message = "華為雲 OCR AK/SK 無效"
        elif status == 403:
            message = "華為雲 OCR IAM 權限不足或欠費"
        else:
            message = "圖片文字識別失敗"
        return OCRRecognitionError(message, status_code=status or None, error_code=code)

    @classmethod
    def _log_error(cls, error, filename: str, b64: str = ""):
        # 不使用 str(error)：網路異常可能包含請求內容或認證資訊。
        message = (getattr(error, "error_msg", "")
                   if isinstance(error, exceptions.ServiceResponseException) else "文字識別失敗")
        message = str(message or "文字識別失敗")
        for secret in (HUAWEI_AK, HUAWEI_SK, b64):
            if secret:
                message = message.replace(secret, "[redacted]")
        logging.warning("status_code=%s error_code=%s error_msg=%s filename=%s",
                        cls._status(error) or None, getattr(error, "error_code", None),
                        message[:256], filename)

    def recognize_image(
        self, image_bytes: bytes, filename: str = "", *,
        progress_callback: Callable[[dict], None] | None = None,
        retry: bool = True,
    ) -> str:
        """識別單張圖片；retry=False 首次失敗或空白結果立即拋錯。"""
        if progress_callback:
            progress_callback({"type": "ocr_started", "total": None})
        try:
            b64 = image_bytes_to_base64(prepare_image_bytes(image_bytes))
        except Exception:
            if not retry:
                raise OCRRecognitionError("圖片預處理失敗") from None
            self._log_error(ValueError(), filename)
            return ""

        for attempt in range(2 if retry else 1):
            try:
                client = self._get_client()
                request = RecognizeGeneralTextRequest()
                request.body = GeneralTextRequestBody(image=b64, detect_direction=True)
                text = self._join_lines(client.recognize_general_text(request))
            except HuaweiOCRNotConfigured:
                raise
            except Exception as error:
                self._check_project_id_error(error)
                self._log_error(error, filename, b64)
                if self._is_fatal(error) or not retry:
                    raise self._recognition_error(error) from None
                if attempt == 0 and self._is_transient(error):
                    time.sleep(1)
                    continue
                return ""
            if not text.strip():
                if not retry:
                    raise OCRRecognitionError("圖片未識別到文字")
                return ""
            if progress_callback:
                progress_callback({"type": "ocr_done"})
            return text
        return ""

    def recognize_pdf(
        self, pdf_bytes: bytes, filename: str = "", *,
        progress_callback: Callable[[dict], None] | None = None,
        retry: bool = True,
    ) -> str:
        """文字層零費用直取；僅掃描頁轉 PNG，保留原有頁碼分隔符。"""
        try:
            import pymupdf as fitz
            doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        except Exception:
            if not retry:
                raise OCRRecognitionError("無法打開 PDF") from None
            self._log_error(ValueError(), filename)
            return ""

        pages_text: list[str] = []
        completed = 0
        try:
            validate_pdf(doc)
            total = len(doc)
            if progress_callback:
                progress_callback({"type": "ocr_started", "total": total})
            if not total and not retry:
                raise OCRRecognitionError("PDF 沒有可識別的頁面")
            for idx, page in enumerate(doc, start=1):
                page_filename = f"{filename}_p{idx}"
                try:
                    text = page.get_text().strip()
                    if not text:
                        pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
                        # 頁內不發圖片事件，避免覆蓋 PDF 總頁數。
                        text = self.recognize_image(pix.tobytes("png"), page_filename, retry=retry)
                    if not text or not text.strip():
                        raise OCRRecognitionError("PDF 頁面未識別到文字")
                except HuaweiOCRNotConfigured:
                    raise
                except ParserLimitError:
                    raise
                except Exception as error:
                    if self._is_fatal(error):
                        raise
                    if not retry:
                        if isinstance(error, OCRRecognitionError):
                            raise
                        raise OCRRecognitionError("PDF 頁面文字識別失敗") from None
                    self._log_error(error, page_filename)
                    continue
                pages_text.append(f"--- 第{idx}页 ---\n{text}")
                completed += 1
                if progress_callback:
                    progress_callback({"type": "ocr_progress", "completed": completed, "total": total})
        finally:
            doc.close()
        return "\n\n".join(pages_text)

    @staticmethod
    def _join_lines(response) -> str:
        result = getattr(response, "result", None)
        blocks = getattr(result, "words_block_list", None) or []
        return "\n".join(block.words for block in blocks if getattr(block, "words", ""))
