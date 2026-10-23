"""
圖片工具函數。

提供圖片預處理與 Base64 轉換，供華為雲 OCR 圖片識別使用。
"""
import base64
import io

from PIL import Image, ImageOps, UnidentifiedImageError


def prepare_image_bytes(image_bytes: bytes) -> bytes:
    """準備符合 general-text 限制的圖片，合規原圖保持不變。

    Base64 最多 10 MiB，圖片兩邊均須介於 15 和 30000 像素。
    總像素最多 160,000,000；修復超限圖片時使用第一幀轉 PNG/JPEG，
    縮圖保持比例，過短的邊補白。
    """
    max_base64_bytes = 10 * 1024 * 1024

    def fits_size(data: bytes) -> bool:
        return 4 * ((len(data) + 2) // 3) <= max_base64_bytes

    try:
        with Image.open(io.BytesIO(image_bytes)) as original:
            width, height = original.size
            if (original.format in {"PNG", "JPEG", "BMP", "GIF", "TIFF", "WEBP", "PCX", "ICO", "PSD"}
                    and 15 <= width <= 30000 and 15 <= height <= 30000
                    and width * height <= 160_000_000
                    and fits_size(image_bytes)):
                original.verify()
                return image_bytes
            image = ImageOps.exif_transpose(original).copy()
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, Image.DecompressionBombError) as exc:
        raise ValueError("無法解碼 OCR 圖片") from exc

    # JPEG 不支援透明度；白底也能避免透明文字背景轉成黑色。
    if image.mode in {"RGBA", "LA"} or "transparency" in image.info:
        rgba = image.convert("RGBA")
        image = Image.new("RGB", rgba.size, "white")
        image.paste(rgba, mask=rgba.getchannel("A"))
    else:
        image = image.convert("RGB")

    scale = min(1.0, 30000 / max(image.size),
                (160_000_000 / (image.width * image.height)) ** 0.5)
    if scale < 1:
        image = image.resize(
            (max(1, int(image.width * scale)), max(1, int(image.height * scale))),
            Image.Resampling.LANCZOS,
        )

    # 有界壓縮／縮圖，避免對無法滿足限制的圖片無限嘗試。
    for _ in range(12):
        if min(image.size) < 15:
            padded = Image.new("RGB", (max(15, image.width), max(15, image.height)), "white")
            padded.paste(image, ((padded.width - image.width) // 2,
                                 (padded.height - image.height) // 2))
            image = padded

        output = io.BytesIO()
        image.save(output, format="PNG")
        if fits_size(output.getvalue()):
            return output.getvalue()

        smallest_size = len(output.getvalue())
        for quality in (95, 85, 70, 55):
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=quality)
            data = output.getvalue()
            if fits_size(data):
                return data
            smallest_size = min(smallest_size, len(data))

        scale = min(0.9, ((max_base64_bytes * 3 / 4) / smallest_size) ** 0.5 * 0.95)
        next_size = (max(15, int(image.width * scale)), max(15, int(image.height * scale)))
        if next_size == image.size:
            break
        image = image.resize(next_size, Image.Resampling.LANCZOS)

    raise ValueError("圖片無法壓縮至華為雲 OCR 的 10 MiB Base64 限制內")


def image_bytes_to_base64(image_bytes: bytes) -> str:
    """
    將圖片位元組編碼為 Base64 字符串（不含 data URI 前綴）。

    Args:
        image_bytes: 圖片原始位元組

    Returns:
        Base64 編碼字符串
    """
    return base64.b64encode(image_bytes).decode("utf-8")
