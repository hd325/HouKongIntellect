"""Public configuration. Credentials belong in environment variables or local .env."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
# Read this checkout's file only; an existing process environment takes precedence.
load_dotenv(PROJECT_ROOT / ".env", override=False)


def _string(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _path(name: str, default: Path) -> Path:
    configured = _string(name)
    value = Path(configured).expanduser() if configured else default
    if not value.is_absolute():
        value = PROJECT_ROOT / value
    return value.resolve()


def _boolean(name: str, default: bool = False) -> bool:
    value = _string(name, str(default)).lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off", ""}:
        return False
    raise ValueError(f"{name} must be true or false")


def _port() -> int:
    try:
        value = int(_string("HOUKONG_PORT", "8000"))
    except ValueError as error:
        raise ValueError("HOUKONG_PORT must be an integer between 1 and 65535") from error
    if not 1 <= value <= 65535:
        raise ValueError("HOUKONG_PORT must be an integer between 1 and 65535")
    return value


def _validate_storage_directory(name: str, path: Path) -> None:
    if path in {PROJECT_ROOT, Path(path.anchor), Path.home().resolve()}:
        raise ValueError(f"{name} must point to a dedicated data directory")


DATA_DIR = _path("HOUKONG_DATA_DIR", PROJECT_ROOT / "data")
_validate_storage_directory("HOUKONG_DATA_DIR", DATA_DIR)
HOST = _string("HOUKONG_HOST", "127.0.0.1") or "127.0.0.1"
PORT = _port()
RELOAD = _boolean("HOUKONG_RELOAD")

DEEPSEEK_API_KEY = _string("DEEPSEEK_API_KEY")
DEEPSEEK_API_URL = _string(
    "DEEPSEEK_API_URL", "https://api.deepseek.com/chat/completions"
)
DEEPSEEK_MODEL = _string("DEEPSEEK_MODEL", "deepseek-flash")
DEEPSEEK_REASONING_MODEL = _string("DEEPSEEK_REASONING_MODEL", "deepseek-flash")
TENCENT_SECRET_ID = _string("TENCENT_SECRET_ID")
TENCENT_SECRET_KEY = _string("TENCENT_SECRET_KEY")
TENCENT_OCR_REGION = _string("TENCENT_OCR_REGION", "ap-guangzhou")
SERPER_API_KEY = _string("SERPER_API_KEY")
HUAWEI_AK = _string("HUAWEI_AK")
HUAWEI_SK = _string("HUAWEI_SK")
HUAWEI_PROJECT_ID = _string("HUAWEI_PROJECT_ID")
HUAWEI_OCR_REGION = _string("HUAWEI_OCR_REGION", "cn-north-4")
ENABLE_FILE_AGENT = _boolean("ENABLE_FILE_AGENT", False)
PARSER_TIMEOUT = 120
MAX_FILE_TASKS = 1


def is_huawei_ocr_configured() -> bool:
    return bool(HUAWEI_AK and HUAWEI_SK)

# The default needs no model download, torch installation, or external API.
EMBEDDING_BACKEND = _string("EMBEDDING_BACKEND", "tfidf").lower()
LOCAL_EMBEDDING_MODEL = _string(
    "LOCAL_EMBEDDING_MODEL", "shibing624/text2vec-base-chinese"
)


def is_tencent_ocr_configured() -> bool:
    return bool(TENCENT_SECRET_ID and TENCENT_SECRET_KEY)


def _default_chroma_dir() -> Path:
    default = DATA_DIR / "chroma_db"
    # Chroma's Windows persistence backend can fail when the path contains
    # non-ASCII characters. Use a separate public-copy store per data directory.
    local_app_data = _string("LOCALAPPDATA")
    if os.name == "nt" and not str(default).isascii() and local_app_data:
        identity = hashlib.sha256(str(DATA_DIR).encode("utf-8")).hexdigest()[:16]
        return Path(local_app_data) / "HouKongIntellectPublic" / identity / "chroma_db"
    return default


CHROMA_DIR = _path("HOUKONG_CHROMA_DIR", _default_chroma_dir())
_validate_storage_directory("HOUKONG_CHROMA_DIR", CHROMA_DIR)
if CHROMA_DIR == DATA_DIR:
    raise ValueError("HOUKONG_CHROMA_DIR must be separate from HOUKONG_DATA_DIR")
