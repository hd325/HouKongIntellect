"""
騰訊雲 OCR 文字提取模塊。

封裝 TencentOCR 類別，提供：
- recognize_image(image_bytes, filename) -> str  通用印刷體識別（高精度優先，通用兜底）
- recognize_pdf(pdf_bytes, filename) -> str       PDF 識別（PyMuPDF 提取內嵌文字 + 掃描頁轉圖 OCR）

既有呼叫預設保留高精度失敗後通用兜底、失敗回傳空字串的行為。
分析草稿可使用 retry=False：單次識別失敗或無文字即拋出 OCRRecognitionError，
配合 progress_callback 回報實際完成的頁數，不回報估計百分比。
"""
import logging
from typing import Callable

from tencentcloud.common import credential
from tencentcloud.common.profile.client_profile import ClientProfile
from tencentcloud.common.profile.http_profile import HttpProfile
from tencentcloud.ocr.v20181119 import ocr_client, models

from config.settings import (
    TENCENT_SECRET_ID,
    TENCENT_SECRET_KEY,
    TENCENT_OCR_REGION,
    is_tencent_ocr_configured,
)
from utils.image_utils import image_bytes_to_base64


class TencentOCRNotConfigured(Exception):
    """騰訊雲 OCR 未配置（缺少 SECRET_ID / SECRET_KEY）。"""


class OCRRecognitionError(RuntimeError):
    """單次文字識別失敗或未取得文字，應由呼叫方停止目前分析。"""


# 模組級別憑證（啟動時若缺金鑰則為 None，首次呼叫時拋出 TencentOCRNotConfigured）
_credential = (
    credential.Credential(TENCENT_SECRET_ID, TENCENT_SECRET_KEY)
    if is_tencent_ocr_configured()
    else None
)


class TencentOCR:
    """騰訊雲 OCR 封裝。"""

    def __init__(self, region: str = TENCENT_OCR_REGION):
        self.region = region
        self._client = None

    def _get_client(self) -> "ocr_client.OcrClient":
        if _credential is None:
            raise TencentOCRNotConfigured(
                "騰訊雲 OCR 未配置：請在本機 .env 或環境變數設定 TENCENT_SECRET_ID 與 TENCENT_SECRET_KEY"
            )
        if self._client is None:
            http_profile = HttpProfile()
            http_profile.endpoint = "ocr.tencentcloudapi.com"
            client_profile = ClientProfile()
            client_profile.httpProfile = http_profile
            self._client = ocr_client.OcrClient(_credential, self.region, client_profile)
        return self._client

    # ------------------------------------------------------------------
    # 圖片識別
    # ------------------------------------------------------------------
    def recognize_image(
        self, image_bytes: bytes, filename: str = "", *,
        progress_callback: Callable[[dict], None] | None = None,
        retry: bool = True,
    ) -> str:
        """
        識別圖片中的文字。

        優先使用高精度印刷體識別（GeneralAccurateOCR），失敗或無結果時
        回退通用識別（GeneralBasicOCR）。返回每行一條 DetectedText 的拼接文本。

        Args:
            image_bytes: 圖片原始位元組
            filename: 檔名（僅用於日誌）
            progress_callback: 接收實際開始/完成事件；圖片識別沒有可量測的百分比
            retry: 是否在高精度失敗或無文字時回退通用識別

        Returns:
            識別出的文本；預設失敗回傳空字串，retry=False 時拋出 OCRRecognitionError
        """
        if progress_callback:
            progress_callback({"type": "ocr_started", "total": None})

        try:
            b64 = image_bytes_to_base64(image_bytes)
        except Exception as e:
            if not retry:
                raise OCRRecognitionError("圖片文字識別失敗") from e
            raise

        # 1) 高精度印刷體識別
        text = ""
        try:
            client = self._get_client()
            req = models.GeneralAccurateOCRRequest()
            req.ImageBase64 = b64
            resp = client.GeneralAccurateOCR(req)
            text = self._join_lines(resp)
        except TencentOCRNotConfigured:
            raise
        except Exception as e:
            if not retry:
                raise OCRRecognitionError("圖片文字識別失敗") from e
            logging.warning(f"⚠️ 騰訊雲高精度圖片 OCR 失敗 {filename}，回退通用識別: {e}")

        if text and (retry or text.strip()):
            if progress_callback:
                progress_callback({"type": "ocr_done"})
            return text
        if not retry:
            raise OCRRecognitionError("圖片未識別到文字")

        # 2) 通用識別（兜底）
        try:
            client = self._get_client()
            req = models.GeneralBasicOCRRequest()
            req.ImageBase64 = b64
            resp = client.GeneralBasicOCR(req)
            text = self._join_lines(resp)
        except TencentOCRNotConfigured:
            raise
        except Exception as e:
            logging.error(f"❌ 騰訊雲圖片 OCR 失敗 {filename}: {e}")
            return ""
        if text and progress_callback:
            progress_callback({"type": "ocr_done"})
        return text

    # ------------------------------------------------------------------
    # PDF 識別
    # ------------------------------------------------------------------
    def recognize_pdf(
        self, pdf_bytes: bytes, filename: str = "", *,
        progress_callback: Callable[[dict], None] | None = None,
        retry: bool = True,
    ) -> str:
        """
        識別 PDF 中的文字。

        流程：
        1. 用 PyMuPDF 逐頁提取內嵌文字（數位化 PDF 毫秒級命中）。
        2. 對無內嵌文字的頁（掃描件）渲染為 PNG 後調用 recognize_image OCR。
        3. 各頁以「--- 第N页 ---」分隔拼接。

        Args:
            pdf_bytes: PDF 原始位元組
            filename: 檔名（僅用於日誌）
            progress_callback: PDF 打開後回報總頁數，每頁成功後回報完成頁數
            retry: False 時任一頁識別失敗即停止，不回退通用識別

        Returns:
            識別出的文本；預設容許部分頁面失敗，retry=False 時失敗即拋出 OCRRecognitionError
        """
        try:
            import fitz  # PyMuPDF
        except ImportError as e:
            if not retry:
                raise OCRRecognitionError("無法解析 PDF") from e
            logging.error("❌ PyMuPDF 未安裝，無法解析 PDF。請執行 pip install PyMuPDF")
            return ""

        try:
            doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        except Exception as e:
            if not retry:
                raise OCRRecognitionError("無法打開 PDF") from e
            logging.error(f"❌ PyMuPDF 打開 PDF 失敗 {filename}: {e}")
            return ""

        pages_text: list[str] = []
        try:
            total = len(doc)
            if progress_callback:
                progress_callback({"type": "ocr_started", "total": total})
            if not retry and not total:
                raise OCRRecognitionError("PDF 沒有可識別的頁面")
            for idx, page in enumerate(doc, start=1):
                try:
                    embedded = page.get_text().strip()
                except Exception as e:
                    if not retry:
                        raise OCRRecognitionError("PDF 頁面文字提取失敗") from e
                    raise
                if embedded:
                    pages_text.append(f"--- 第{idx}页 ---\n{embedded}")
                    if progress_callback:
                        progress_callback({"type": "ocr_progress", "completed": idx, "total": total})
                    continue
                # 掃描頁：轉圖後 OCR
                try:
                    pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
                    img_bytes = pix.tobytes("png")
                    # 頁面的圖片識別不另發開始/完成事件，以免覆蓋 PDF 總頁數。
                    if retry:
                        ocr_text = self.recognize_image(img_bytes, f"{filename}_p{idx}")
                    else:
                        ocr_text = self.recognize_image(img_bytes, f"{filename}_p{idx}", retry=False)
                    if ocr_text and ocr_text.strip():
                        pages_text.append(f"--- 第{idx}页 ---\n{ocr_text}")
                    elif not retry:
                        raise OCRRecognitionError("PDF 頁面未識別到文字")
                except Exception as e:
                    if not retry:
                        if isinstance(e, (OCRRecognitionError, TencentOCRNotConfigured)):
                            raise
                        raise OCRRecognitionError("PDF 頁面文字識別失敗") from e
                    logging.warning(f"⚠️ PDF 第{idx}頁 OCR 失敗 {filename}: {e}")
                    continue
                if ocr_text and ocr_text.strip() and progress_callback:
                    progress_callback({"type": "ocr_progress", "completed": idx, "total": total})
        finally:
            doc.close()

        return "\n\n".join(pages_text)

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------
    @staticmethod
    def _join_lines(resp) -> str:
        if not resp or not getattr(resp, "TextDetections", None):
            return ""
        return "\n".join(
            d.DetectedText for d in resp.TextDetections if getattr(d, "DetectedText", "")
        )
