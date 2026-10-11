"""Read-only file-list views for authorized documents already in the active index.

No IDs are invented and neither the collection nor upload records are modified.
Indexed metadata is the authority for permission checks; old file records can
only supplement display information and existing text.
"""
from __future__ import annotations

import math
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from services.document_files import display_upload_name, stable_upload_id


MAX_TEXT_CHARS = 200_000
VALID_AUDIENCES = {"school", "personal", "class", "grade"}
_PAGE_PREVIEW_TYPES = {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp",
                       ".svg", ".heic", ".tif", ".tiff"}


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _integer(value: object, default: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    if value < 0 or (isinstance(value, float) and not math.isfinite(value)):
        return default
    return int(value)


def _upload_paths(uploads_dir: str | Path) -> dict[str, list[Path]]:
    """Match IDs using direct upload filenames; reject symlinks outside uploads."""
    root = Path(uploads_dir).resolve()
    paths: dict[str, list[Path]] = defaultdict(list)
    try:
        entries = sorted(root.iterdir(), key=lambda path: path.name)
    except OSError:
        return paths
    for entry in entries:
        try:
            resolved = entry.resolve()
            if not resolved.is_relative_to(root) or not resolved.is_file():
                continue
        except OSError:
            continue
        paths[stable_upload_id(entry.name)].append(entry)
    return paths


def _allowed_metadata(metadata: Mapping[str, Any], can_access: Callable[[Mapping[str, Any]], bool]) -> bool:
    audience = metadata.get("audience")
    if not isinstance(audience, str) or audience not in VALID_AUDIENCES:
        return False
    if audience != "school" and not _text(metadata.get("owner")):
        return False
    try:
        return can_access(dict(metadata)) is True
    except Exception:
        return False


def _joined_text(chunks: list[dict[str, Any]]) -> str:
    pieces: list[str] = []
    remaining = MAX_TEXT_CHARS
    for chunk in sorted(chunks, key=lambda item: (
        _integer(item["metadata"].get("page"), 1),
        _integer(item["metadata"].get("char_start")),
        item["chunk_id"],
    )):
        content = chunk["content"]
        if not isinstance(content, str) or not content.strip():
            continue
        separator = "\n\n" if pieces else ""
        if len(separator) >= remaining:
            break
        piece = (separator + content)[:remaining]
        pieces.append(piece)
        remaining -= len(piece)
        if remaining <= 0:
            break
    return "".join(pieces)


def build_indexed_file_entries(
    store: Any,
    uploads_dir: str | Path,
    known_ids: Iterable[str],
    *,
    can_access: Callable[[Mapping[str, Any]], bool],
    inline_records: Iterable[Mapping[str, Any]] | None = None,
    inline_contents: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Supplement an existing file list with same-ID authorized index views.

    ``can_access`` must be a synchronous predicate, typically the existing
    application's permission checker bound to the current user. Every chunk
    belonging to a supplementary document must carry an explicit valid
    audience and pass that check. PDF/image views with an on-disk original
    omit extracted text; views without an original retain a text fallback.
    Text prefers cached inline content, then ordered indexed chunks capped
    at 200,000 characters, including separators.
    """
    existing = set(known_ids)
    originals = {
        record["id"]: dict(record)
        for record in (inline_records or [])
        if isinstance(record, Mapping) and isinstance(record.get("id"), str)
    }
    inline_texts = inline_contents or {}
    raw = store._collection.get(include=["metadatas", "documents"])
    metadata_rows = raw.get("metadatas") or []
    documents = raw.get("documents") or []
    chunk_ids = raw.get("ids") or []
    by_document: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for index, metadata in enumerate(metadata_rows):
        if not isinstance(metadata, Mapping):
            continue
        document_id = metadata.get("document_id")
        if not isinstance(document_id, str) or not document_id.strip() or document_id in existing:
            continue
        by_document[document_id].append({
            "metadata": dict(metadata),
            "content": documents[index] if index < len(documents) else "",
            "chunk_id": str(chunk_ids[index]) if index < len(chunk_ids) else "",
        })
    disk_files = _upload_paths(uploads_dir)
    result: list[dict[str, Any]] = []
    for document_id, chunks in by_document.items():
        if not all(_allowed_metadata(chunk["metadata"], can_access) for chunk in chunks):
            continue
        metadata = chunks[0]["metadata"]
        original = originals.get(document_id, {})
        preferred_name = next((value for value in (
            _text(original.get("original_name")), _text(original.get("original_filename")),
            _text(metadata.get("original_name")), _text(metadata.get("original_filename")),
            _text(original.get("name")), _text(metadata.get("filename")),
        ) if value), "")
        paths = disk_files.get(document_id, [])
        path = next((candidate for candidate in paths if display_upload_name(candidate.name) == preferred_name),
                    paths[0] if paths else None)
        name = display_upload_name(preferred_name) if preferred_name else (
            display_upload_name(path.name) if path else "未命名文件"
        )
        extension = (path.suffix if path else Path(name).suffix).lower()
        has_original = path is not None
        size = _integer(original.get("size"), _integer(metadata.get("size")))
        if path is not None:
            try:
                size = path.stat().st_size
            except OSError:
                has_original = False
        extracted_text = ""
        if not (has_original and extension in _PAGE_PREVIEW_TYPES):
            cached = inline_texts.get(document_id)
            extracted_text = cached[:MAX_TEXT_CHARS] if isinstance(cached, str) and cached.strip() else _joined_text(chunks)
        pages = max((_integer(chunk["metadata"].get("page")) for chunk in chunks), default=0)
        has_indexed_text = any(isinstance(chunk["content"], str) and chunk["content"].strip() for chunk in chunks)
        result.append({
            "id": document_id,
            "name": name,
            "original_filename": name,
            "audience": metadata["audience"],
            "size": size,
            "uploaded_at": _text(original.get("uploaded_at")) or _text(metadata.get("uploaded_at")),
            "has_original": has_original,
            "subject": _text(metadata.get("subject")) or _text(original.get("subject")),
            "material_type": _text(original.get("material_type")) or _text(metadata.get("material_type")),
            "study_period": _text(original.get("study_period")) or _text(metadata.get("study_period")),
            "notes": _text(original.get("notes")) or _text(metadata.get("notes")),
            "page_count": max(pages, _integer(original.get("page_count"))),
            "extraction_status": "success" if has_indexed_text or extracted_text.strip() else "none",
            "extracted_text": extracted_text,
            "indexed_only": True,
        })
    return sorted(result, key=lambda entry: (entry["uploaded_at"], entry["name"], entry["id"]), reverse=True)
