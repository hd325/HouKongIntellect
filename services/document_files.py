"""Upload identity helpers, following scripts/reindex_all.py's existing rules.

Keep these runtime helpers independent of the maintenance script and its CLI
imports. UUID-prefixed files retain their upload ID; all other names use the
same SHA-256 filename digest as the active index.
"""
import hashlib
import re
from pathlib import Path

_STORED_UPLOAD_RE = re.compile(
    r"^(?P<id>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
    r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12})_(?P<name>.+)$"
)


def stable_upload_id(filename: str) -> str:
    name = Path(filename).name
    match = _STORED_UPLOAD_RE.match(name)
    if match:
        return match.group("id").lower()
    return "upload_" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:20]


def display_upload_name(filename: str) -> str:
    name = Path(filename).name
    match = _STORED_UPLOAD_RE.match(name)
    return match.group("name") if match else name
