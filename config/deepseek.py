"""Shared public DeepSeek model settings; credentials stay in settings.py.

Official mapping checked on 2026-10-17:
https://api-docs.deepseek.com/zh-cn/quick_start/pricing/
The API alias follows provider upgrades; the version below is the last
documented mapping, not a claim that an API response exposes a revision ID.
"""

from config.settings import DEEPSEEK_MODEL, DEEPSEEK_REASONING_MODEL
DEEPSEEK_MODEL_VERSION = "DeepSeek-V4.1-Flash-1017"
DEEPSEEK_MODEL_VERIFIED_ON = "2026-10-17"
DEEPSEEK_MODEL_DOCS_URL = "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/"

# Public Fetch endpoint only; discovery stays on Serper/Google.
DEEPSEEK_FETCH_API_URL = "https://api.deepseek.com/v1/tools/fetch"
DEEPSEEK_FETCH_DOCS_URL = "https://api-docs.deepseek.com/zh-cn/guides/search/"


def deepseek_request_options(*, think: bool = False) -> dict:
    """Select Flash and explicitly control the provider's default-on thinking."""
    options = {
        "model": DEEPSEEK_REASONING_MODEL if think else DEEPSEEK_MODEL,
        "thinking": {"type": "enabled" if think else "disabled"},
    }
    if think:
        options["reasoning_effort"] = "high"
    return options
