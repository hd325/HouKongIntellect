"""Bounded document extraction used by the disposable parser worker."""
import csv
import io
import logging
import re
import zipfile
from html.parser import HTMLParser
from typing import Any, Dict
from fastapi import UploadFile
from docx import Document
from services.ocr.huawei_ocr import HuaweiOCR, HuaweiOCRNotConfigured
from services.resource_limits import ParserLimitError, validate_zip, bounded_member
_huawei_ocr = HuaweiOCR()
def extract_text_from_file(file: UploadFile, *, progress_callback=None, retry: bool = True) -> Dict[str, Any]:
    """
    從上傳的檔案中提取文本內容（華為雲 OCR 架構）

    支援格式:
    - 文檔: txt, docx, doc, pdf, md, rtf, wps, odt
    - 圖片: jpg, jpeg, png, gif, bmp, webp, tiff,tif, heic, svg (華為雲 OCR 識別)
    - 表格: xlsx, xls, csv, ods, et
    - 演示: pptx, ppt, odp, dps
    - 電子書: epub
    - 網頁: html, htm, xml
    - 壓縮包: zip, rar (嘗試提取文本檔案)

    PDF 與圖片統一由華為雲 OCR 識別；Office 文檔（docx/xlsx/pptx）使用原生庫提取，
    其餘格式使用各自解析器（非 OCR）。

    Args:
        file: 上傳的檔案對象

    Returns:
        {
            "content":          str,   # 提取的文本（OCR 失敗/無文字時為空）
            "source":           str,   # 提取來源標識
            "page_count":       int,   # OCR 頁數（非 OCR 格式為 0）
            "extraction_status":str,   # "success" | "partial" | "empty" | "failed" | "none"
            "error":            str,   # 不支援/解析失敗說明（非空時上傳應失敗）
        }
    """
    file_ext = file.filename.split(".")[-1].lower() if file.filename else ""

    try:
        file_content = file.file.read()
        if file_ext in {"zip", "docx", "xlsx", "pptx", "epub", "wps", "et", "sxs"}:
            validate_zip(file_content)

        # ========== 文本檔案 (增強多編碼支援) ==========
        if file_ext == "txt":
            # 嘗試多種編碼
            for encoding in ["utf-8", "gbk", "gb2312", "big5", "utf-16", "iso-8859-1"]:
                try:
                    return _ok_result(file_content.decode(encoding), "txt")
                except (UnicodeDecodeError, LookupError):
                    continue
            return _ok_result(file_content.decode("utf-8", errors="ignore"), "txt")

        # ========== Markdown 檔案 ==========
        elif file_ext in ["md", "markdown"]:
            for encoding in ["utf-8", "gbk", "gb2312"]:
                try:
                    return _ok_result(file_content.decode(encoding), "md")
                except UnicodeDecodeError:
                    continue
            return _ok_result(file_content.decode("utf-8", errors="ignore"), "md")

        # ========== RTF 富文本格式 ==========
        elif file_ext == "rtf":
            try:
                # 簡單RTF解析，提取純文本
                rtf_text = file_content.decode("utf-8", errors="ignore")
                # 移除RTF控制字
                # 移除分組、控制符
                rtf_text = re.sub(r'\\[a-z]+\d*\s?', '', rtf_text)
                rtf_text = re.sub(r'[{}]', '', rtf_text)
                rtf_text = re.sub(r'\\\'[0-9a-f]{2}', '', rtf_text)
                # 移除多餘空白但保留換行
                rtf_text = re.sub(r'\\line\s?', '\n', rtf_text)
                rtf_text = re.sub(r'\\par\s?', '\n', rtf_text)
                rtf_text = re.sub(r'\n{3,}', '\n\n', rtf_text)
                return _ok_result(rtf_text.strip(), "rtf") if rtf_text.strip() else _err_result("RTF 檔案解析失敗")
            except Exception as rtf_err:
                logging.error(f"RTF 解析失敗: {rtf_err}")
                return _err_result(f"RTF 解析失敗: {str(rtf_err)}")

        # ========== Word 文檔 (docx 原生提取 / doc 跳過) ==========
        elif file_ext == "docx":
            return _extract_office_text(file_content, file.filename)
        elif file_ext == "doc":
            return _skipped_result(file.filename)

        # ========== WPS 文檔 (et/sxs/wps) ==========
        elif file_ext in ["et", "sxs", "wps"]:
            # WPS格式嘗試作為ZIP解析
            return _wrap_extract(_extract_wps_like, file_content, file.filename, "wps")

        # ========== PDF 檔案（華為雲 OCR）==========
        elif file_ext == "pdf":
            try:
                text = _huawei_ocr.recognize_pdf(
                    file_content, file.filename, progress_callback=progress_callback, retry=retry,
                )
            except HuaweiOCRNotConfigured:
                return {
                    "content": "",
                    "source": "huawei_ocr",
                    "page_count": 0,
                    "extraction_status": "failed",
                    "error": "",
                }
            return _huawei_ocr_result(text)

        # ========== 圖片檔案（華為雲 OCR）==========
        elif file_ext in ["jpg", "jpeg", "png", "gif", "bmp", "webp", "tiff", "tif", "heic"]:
            try:
                text = _huawei_ocr.recognize_image(
                    file_content, file.filename, progress_callback=progress_callback, retry=retry,
                )
            except HuaweiOCRNotConfigured:
                return {
                    "content": "",
                    "source": "huawei_ocr",
                    "page_count": 0,
                    "extraction_status": "failed",
                    "error": "",
                }
            return _huawei_ocr_result(text)

        # ========== SVG 矢量圖 (嘗試提取文字) ==========
        elif file_ext == "svg":
            try:
                svg_text = file_content.decode("utf-8", errors="ignore")
                # 提取SVG中的文字
                text_elements = re.findall(r'<text[^>]*>([^<]+)</text>', svg_text, re.IGNORECASE)
                if text_elements:
                    return _ok_result(" | ".join(text_elements), "svg")
                # 嘗試提取CDATA
                cdata_text = re.findall(r'<!\[CDATA\[([^\]]+)\]\]>', svg_text)
                if cdata_text:
                    return _ok_result(" | ".join(cdata_text), "svg")
                return _err_result("SVG 檔案中未找到文本內容")
            except Exception as svg_err:
                return _err_result(f"SVG 解析失敗: {str(svg_err)}")

        # ========== Excel 表格 (xlsx 原生提取 / xls 跳過) ==========
        elif file_ext == "xlsx":
            return _extract_office_text(file_content, file.filename)
        elif file_ext == "xls":
            return _skipped_result(file.filename)

        # ========== ODS 表格 (ods 跳過，無本地轉換器) ==========
        elif file_ext == "ods":
            return _skipped_result(file.filename)

        # ========== CSV/TSV 表格 ==========
        elif file_ext in ["csv", "tsv"]:
            return _wrap_extract(_extract_csv, file_content, file.filename, "csv", file_ext=file_ext)

        # ========== PowerPoint 演示文稿 (pptx 原生提取 / ppt 跳過) ==========
        elif file_ext == "pptx":
            return _extract_office_text(file_content, file.filename)
        elif file_ext == "ppt":
            return _skipped_result(file.filename)

        # ========== ODP 演示文稿 (odp 跳過，無本地轉換器) ==========
        elif file_ext == "odp":
            return _skipped_result(file.filename)

        # ========== DPS 演示文稿 (金山格式) ==========
        elif file_ext == "dps":
            return _err_result("金山演示 .dps 格式暫不支援，建議另存為 .pptx 後上傳")

        # ========== HTML 網頁 (增強) ==========
        elif file_ext in ["html", "htm", "xml"]:
            return _wrap_extract(_extract_html, file_content, file.filename, "html")

        # ========== EPUB 電子書 ==========
        elif file_ext == "epub":
            return _wrap_extract(_extract_epub, file_content, file.filename, "epub")

        # ========== ODT 文檔 (odt 跳過，無本地轉換器) ==========
        elif file_ext == "odt":
            return _skipped_result(file.filename)

        # ========== 壓縮包 - 嘗試提取文本檔案 ==========
        elif file_ext in ["zip", "rar", "7z", "tar", "gz"]:
            return _wrap_extract(_extract_from_archive, file_content, file.filename, "archive", file_ext=file_ext)

        # ========== 不支援的格式：跳過 OCR，仍允許上傳 ==========
        else:
            # 不支援類型（如 .exe）不阻塞上傳：標記 skipped，返回空文本，文件仍可下載
            logging.info(f"⏭️ 不支援的格式 .{file_ext}，跳過文字提取: {file.filename}")
            return {
                "content": "",
                "source": "skip",
                "page_count": 0,
                "extraction_status": "skipped",
                "error": "",
            }

    except ParserLimitError:
        raise
    except Exception as e:
        logging.error(f"檔案解析失敗 {file.filename}: {str(e)}")
        return _err_result(f"檔案解析失敗: {str(e)}")

    finally:
        file.file.seek(0)  # Reset file pointer


def _ok_result(content: str, source: str) -> Dict[str, Any]:
    """構造成功的提取結果。"""
    return {
        "content": content,
        "source": source,
        "page_count": 0,
        "extraction_status": "none",
        "error": "",
    }


def _err_result(error: str) -> Dict[str, Any]:
    """構造失敗的提取結果（不支援格式 / 解析異常）。"""
    return {
        "content": "",
        "source": "error",
        "page_count": 0,
        "extraction_status": "failed",
        "error": error,
    }


def _wrap_extract(fn, file_content: bytes, filename: str, source: str, file_ext: str = "") -> Dict[str, Any]:
    """
    包裝非 OCR 格式提取器：成功返回内容，失敗返回帶錯誤說明的結果。
    fn 簽名：fn(file_content, filename) 或 fn(file_content, filename, ext)
    """
    try:
        if "file_ext" in fn.__code__.co_varnames:
            text = fn(file_content, filename, file_ext) if file_ext else fn(file_content, filename)
        else:
            text = fn(file_content, filename)
    except ParserLimitError:
        raise
    except Exception as e:
        logging.error(f"❌ {source} 解析異常: {e}")
        return _err_result(f"{source} 解析失敗: {str(e)}")

    if text and text.strip() and "失敗" not in text and "不支援" not in text:
        return _ok_result(text, source)
    # 解析器返回了錯誤說明字符串
    return _err_result(text if text else f"{source} 解析失敗")


def _skipped_result(filename: str) -> Dict[str, Any]:
    """構造跳過文字提取的結果（不支援格式 / 無本地轉換器）。上傳不阻塞。"""
    logging.info(f"⏭️ 跳過文字提取（無本地轉換器）: {filename}")
    return {
        "content": "",
        "source": "skip",
        "page_count": 0,
        "extraction_status": "skipped",
        "error": "",
    }


def _huawei_ocr_result(text: str) -> Dict[str, Any]:
    """將華為雲 OCR 返回的文本構造成標準提取結果。"""
    if text and text.strip():
        return {
            "content": text,
            "source": "huawei_ocr",
            "page_count": 0,
            "extraction_status": "success",
            "error": "",
        }
    return {
        "content": "",
        "source": "huawei_ocr",
        "page_count": 0,
        "extraction_status": "empty",
        "error": "",
    }


def _extract_office_text(file_content: bytes, filename: str) -> Dict[str, Any]:
    """
    使用原生庫提取 Office 文檔文本（docx / xlsx / pptx）。
    不再依賴 LibreOffice / MinerU。doc / xls / ppt / odt / ods / odp 不在支援範圍
    （由呼叫方走 _skipped_result）。

    提取失敗一律回傳空內容 + failed（error 留空），由 /api/upload 標記後仍返回 200，
    不阻塞上傳。
    """
    ext = filename.split(".")[-1].lower() if filename else ""
    from io import BytesIO
    try:
        if ext == "docx":
            doc = Document(BytesIO(file_content))
            paras = [p.text for p in doc.paragraphs if p.text and p.text.strip()]
            for table in doc.tables:
                for row in table.rows:
                    cells = [c.text.strip() for c in row.cells if c.text and c.text.strip()]
                    if cells:
                        paras.append(" | ".join(cells))
            text = "\n".join(paras)
        elif ext == "xlsx":
            import openpyxl
            wb = openpyxl.load_workbook(BytesIO(file_content), data_only=True, read_only=True)
            rows = []
            for ws in wb.worksheets:
                for r in ws.iter_rows(values_only=True):
                    cells = [str(c).strip() for c in r if c is not None and str(c).strip()]
                    if cells:
                        rows.append(" | ".join(cells))
            text = "\n".join(rows)
        elif ext == "pptx":
            from pptx import Presentation
            prs = Presentation(BytesIO(file_content))
            paras = []
            for slide in prs.slides:
                for shape in slide.shapes:
                    if shape.has_text_frame:
                        t = shape.text_frame.text.strip()
                        if t:
                            paras.append(t)
            text = "\n".join(paras)
        else:
            return {"content": "", "source": "office", "page_count": 0, "extraction_status": "failed", "error": ""}

        if text and text.strip():
            return {"content": text, "source": ext, "page_count": 0, "extraction_status": "none", "error": ""}
        return {"content": "", "source": ext, "page_count": 0, "extraction_status": "failed", "error": ""}
    except ParserLimitError:
        raise
    except Exception as e:
        logging.error(f"❌ Office 文本提取失敗 {filename}: {e}")
        return {"content": "", "source": ext, "page_count": 0, "extraction_status": "failed", "error": ""}


def _extract_from_archive(file_content: bytes, filename: str, file_ext: str) -> str:
    """從壓縮包中提取文本檔案"""
    try:
        if file_ext == 'zip':
            validate_zip(file_content)
            with zipfile.ZipFile(io.BytesIO(file_content), 'r') as zf:
                text_parts = []

                for name in zf.namelist():
                    if any(name.lower().endswith(ext) for ext in ['.txt', '.md', '.csv', '.json', '.xml', '.html', '.htm']):
                        try:
                            content = bounded_member(zf, zf.getinfo(name)).decode('utf-8', errors='ignore')
                            if len(content) > 10:
                                text_parts.append(f"[來自: {name}]\n{content[:5000]}")
                        except ParserLimitError:
                            raise
                        except Exception:
                            continue

                if text_parts:
                    result = "\n\n".join(text_parts)
                    logging.info(f"✅ 壓縮包文本提取成功: {filename}")
                    return result

        return "壓縮包中未找到可解析的文本檔案"

    except zipfile.BadZipFile:
        return f".{file_ext} 壓縮包格式無效或已損壞"
    except Exception as archive_err:
        logging.error(f"壓縮包解析失敗: {archive_err}")
        return f"壓縮包解析失敗: {str(archive_err)}"



def _extract_csv(raw, filename, ext="csv"):
    text = raw.decode("utf-8-sig", errors="replace")
    rows = csv.reader(io.StringIO(text), delimiter="\t" if ext == "tsv" else ",")
    output = []
    size = 0
    for number, row in enumerate(rows):
        if number > 100_000 or len(row) > 16_384:
            raise ParserLimitError("表格項目數量超過安全限制。")
        value = " | ".join(row)
        output.append(value); size += len(value)
        if size > 1_000_000: break
    return "\n".join(output)[:1_000_000]

class _TextOnlyHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True); self.parts=[]; self.hidden=0
    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}: self.hidden += 1
    def handle_endtag(self, tag):
        if tag in {"script", "style"}: self.hidden = max(0, self.hidden-1)
    def handle_data(self, value):
        if not self.hidden: self.parts.append(value)

def _extract_html(raw, filename=""):
    parser = _TextOnlyHTML(); parser.feed(raw.decode("utf-8", errors="replace"))
    return "\n".join(parser.parts)[:1_000_000]

def _extract_epub(raw, filename=""):
    validate_zip(raw); texts=[]
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        for entry in archive.infolist():
            if entry.filename.lower().endswith((".html", ".htm", ".xhtml")):
                texts.append(_extract_html(bounded_member(archive, entry)))
    return "\n".join(texts)[:1_000_000]

def _extract_wps_like(raw, filename=""):
    return _extract_from_archive(raw, filename, "zip")
