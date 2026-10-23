"""Budgets enforced before hostile documents are expanded or rasterized."""
from __future__ import annotations

import asyncio
import io
import zipfile

MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_ZIP_ENTRIES = 100
MAX_ZIP_MEMBER_BYTES = 10 * 1024 * 1024
MAX_ZIP_TOTAL_BYTES = 50 * 1024 * 1024
MAX_PDF_PAGES = 50
MAX_PAGE_PIXELS = 10_000_000
# The API and optional file agent share this process-wide admission gate.
PROCESSING_SLOT = asyncio.Semaphore(1)


async def await_thread_completion(operation):
    """Keep resource admission until a non-killable thread ends, even on recancel."""
    task = asyncio.get_running_loop().run_in_executor(None, operation)
    canceled = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            canceled = True
    if canceled:
        raise asyncio.CancelledError
    return result


class ParserLimitError(ValueError):
    """A safe rejection; never include document contents or host paths."""


def validate_zip(raw) -> None:
    with zipfile.ZipFile(io.BytesIO(raw) if isinstance(raw, bytes) else raw) as archive:
        entries = archive.infolist()
        if len(entries) > MAX_ZIP_ENTRIES:
            raise ParserLimitError("壓縮文件最多允許 100 個項目。")
        total = 0
        for entry in entries:
            if entry.is_dir():
                continue
            total += entry.file_size
            if entry.flag_bits & 1:
                raise ParserLimitError("不支援加密壓縮文件。")
            if entry.file_size > MAX_ZIP_MEMBER_BYTES or total > MAX_ZIP_TOTAL_BYTES:
                raise ParserLimitError("壓縮文件解壓大小超限（單項 10 MB，總量 50 MB）。")
            if entry.file_size > max(entry.compress_size, 1) * 100:
                raise ParserLimitError("壓縮文件的膨脹比例超過安全限制。")


def bounded_member(archive: zipfile.ZipFile, entry: zipfile.ZipInfo) -> bytes:
    with archive.open(entry) as stream:
        value = stream.read(MAX_ZIP_MEMBER_BYTES + 1)
    if len(value) > MAX_ZIP_MEMBER_BYTES:
        raise ParserLimitError("壓縮項目解壓大小超限。")
    return value


def validate_pdf(doc) -> None:
    if len(doc) > MAX_PDF_PAGES:
        raise ParserLimitError("PDF 最多允許 50 頁，請分拆文件。")
    for page in doc:
        rect = page.rect
        width, height = float(rect.width), float(rect.height)
        if not (0 < width <= 100_000 and 0 < height <= 100_000):
            raise ParserLimitError("PDF 頁面尺寸不合法。")
        if width * height * 4 > MAX_PAGE_PIXELS:
            raise ParserLimitError("PDF 每頁渲染最多允許 1000 萬像素。")
