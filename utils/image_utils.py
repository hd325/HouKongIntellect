"""
圖片工具函數。

提供位元組與 Base64 之間的轉換，供騰訊雲 OCR 圖片識別使用。
"""
import base64


def image_bytes_to_base64(image_bytes: bytes) -> str:
    """
    將圖片位元組編碼為 Base64 字符串（不含 data URI 前綴）。

    Args:
        image_bytes: 圖片原始位元組

    Returns:
        Base64 編碼字符串
    """
    return base64.b64encode(image_bytes).decode("utf-8")
