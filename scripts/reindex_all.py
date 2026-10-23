"""Rebuild the active knowledge index from published records and uploads/.

The two sources are deliberately treated as a union: files_data.json contains
inline text which is not present in uploads/, while uploads/ contains stored
files which may not have a files_data entry. Current conversations_data.json
records override legacy metadata, including review state and audience scope.
Only uploads/ is scanned; the separate review_uploads/ directory is excluded.

Usage:
    python scripts/reindex_all.py
    python scripts/reindex_all.py --verify-only
    python scripts/reindex_all.py --no-cache
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import io
import json
import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.review_policy import is_published_record
from config.settings import DATA_DIR

FILES_DATA_PATH = Path(DATA_DIR) / 'files_data.json'
CONVERSATIONS_DATA_PATH = Path(DATA_DIR) / 'conversations_data.json'
UPLOADS_DIR = Path(DATA_DIR) / 'uploads'
OCR_CACHE_PATH = Path(DATA_DIR) / '.reindex_ocr_cache.json'
ORPHAN_DOCUMENT_IDS = ()
FAILED_EXTRACTION_STATUSES = {"empty", "failed"}
_STORED_UPLOAD_RE = re.compile(
    r"^(?P<id>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
    r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12})_(?P<name>.+)$"
)

SAMPLE_QUESTIONS = (
    ("化学·原电池", "原电池的工作原理和电极反应式的书写方法", "原電池"),
    ("数学", "高中数学数列、函数与空间向量的解题方法", "數學"),
    ("生物", "酶的特性以及温度对酶活性的影响", "生物"),
    ("英语", "Senior 2 English grammar gerund and infinitive", "English"),
    ("AI竞赛", "ENJOY AI 文明破晓比赛的任务和规则", "文明破曉"),
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("reindex_all")


@dataclass(frozen=True)
class SourceDocument:
    file_info: dict[str, object]
    content: str
    source: str
    extraction_status: str = "success"

    @property
    def document_id(self) -> str:
        return str(self.file_info["id"])


def stable_upload_id(filename: str) -> str:
    """Return an ID derived only from the stored filename.

    main.py stores uploads as ``<file UUID>_<safe original name>``. Reusing that
    UUID preserves the ID generated at upload time. Files not following that
    convention receive a deterministic filename-only hash.
    """
    match = _STORED_UPLOAD_RE.match(Path(filename).name)
    if match:
        return match.group("id").lower()
    digest = hashlib.sha256(Path(filename).name.encode("utf-8")).hexdigest()[:20]
    return f"upload_{digest}"


def display_upload_name(filename: str) -> str:
    match = _STORED_UPLOAD_RE.match(Path(filename).name)
    return match.group("name") if match else Path(filename).name


def _load_json(path: Path, default: Any) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
        logger.warning("无法读取 %s：%s", path, exc)
        return default


def _save_ocr_cache(cache: dict[str, object]) -> None:
    try:
        with OCR_CACHE_PATH.open("w", encoding="utf-8") as handle:
            json.dump(cache, handle, ensure_ascii=False)
    except OSError as exc:
        logger.warning("无法写入 OCR 缓存 %s：%s", OCR_CACHE_PATH, exc)


def _cache_signature(path: Path) -> dict[str, int]:
    stat = path.stat()
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def _legacy_cache_signature(path: Path) -> dict[str, float | int]:
    stat = path.stat()
    return {"size": stat.st_size, "mtime": stat.st_mtime}


def _legacy_upload_id(path: Path) -> str:
    # Compatibility with the previous reindex script, which hashed an absolute
    # path. This is used only to reuse its OCR cache, never as document_id.
    return "upload_" + hashlib.md5(str(path).encode("utf-8")).hexdigest()[:12]


def _cached_extraction(
    cache: dict[str, object], path: Path, document_id: str
) -> dict[str, object] | None:
    current = cache.get(document_id)
    if isinstance(current, dict) and current.get("sig") == _cache_signature(path):
        return current

    legacy = cache.get(_legacy_upload_id(path))
    if isinstance(legacy, dict) and legacy.get("sig") == _legacy_cache_signature(path):
        return legacy
    return None


def _catalog_records(data: dict[str, object]) -> Iterable[dict[str, object]]:
    files = data.get("files")
    if isinstance(files, list) and files:
        yield from (item for item in files if isinstance(item, dict))
        return
    # Match DataStore's backwards-compatible migration without mutating input.
    public_files = data.get("public_files", [])
    for item in public_files if isinstance(public_files, list) else []:
        if isinstance(item, dict):
            yield {**item, "audience": item.get("audience") or "school"}
    private_files = data.get("private_files", {})
    if isinstance(private_files, dict):
        for owner, items in private_files.items():
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict):
                        yield {**item, "owner": item.get("owner") or owner, "audience": "personal"}


def load_catalog_data(
    path: Path = FILES_DATA_PATH,
    conversations_path: Path | None = None,
) -> tuple[dict[str, dict[str, object]], dict[str, object]]:
    """Merge legacy and active records; active fields and contents take priority."""
    conversations_path = conversations_path or path.parent / CONVERSATIONS_DATA_PATH.name
    metadata: dict[str, dict[str, object]] = {}
    file_contents: dict[str, object] = {}
    deleted_ids: set[str] = set()
    for source in (path, conversations_path):
        data = _load_json(source, {})
        if not isinstance(data, dict):
            continue
        deleted_ids.update(str(item) for item in data.get("deleted_document_ids", []))
        for item in _catalog_records(data):
            if item.get("id"):
                document_id = str(item["id"])
                metadata[document_id] = {**metadata.get(document_id, {}), **item}
        contents = data.get("file_contents", {})
        if isinstance(contents, dict):
            file_contents.update({str(key): value for key, value in contents.items()})
    for document_id in deleted_ids:
        metadata[document_id] = {"id": document_id, "review_status": "deleted"}
        file_contents.pop(document_id, None)
    return metadata, file_contents


def _index_file_info(document_id: str, name: str, meta: dict[str, object]) -> dict[str, object]:
    file_info = {
        "id": document_id,
        "name": str(meta.get("name") or meta.get("original_name") or name),
        "owner": meta.get("owner") or "",
        "audience": meta.get("audience") or "school",
        "subject": meta.get("subject") or "",
    }
    for field in ("grade", "review_status", "publisher_role", "audience_class", "audience_grade"):
        if field in meta:
            file_info[field] = meta[field]
    return file_info


def load_inline_documents(
    path: Path = FILES_DATA_PATH,
    *,
    conversations_path: Path | None = None,
) -> tuple[list[SourceDocument], list[str]]:
    metadata, file_contents = load_catalog_data(path, conversations_path)

    documents: list[SourceDocument] = []
    skipped: list[str] = []
    for raw_id, raw_content in file_contents.items():
        document_id = str(raw_id)
        meta = metadata.get(document_id, {})
        status = str(meta.get("extraction_status") or "success").lower()
        content = str(raw_content or "")
        name = str(meta.get("name") or f"{document_id}.txt")
        if not is_published_record(meta):
            skipped.append(f"files_data:{name} [{meta.get('review_status')}]")
            continue
        if not content.strip() or status in FAILED_EXTRACTION_STATUSES:
            skipped.append(f"files_data:{name} [{status if content.strip() else 'empty'}]")
            logger.warning("跳过内联空文本/提取失败：%s (id=%s, status=%s)", name, document_id, status)
            continue

        file_info = _index_file_info(document_id, name, meta)
        documents.append(SourceDocument(file_info, content, "files_data", status))
    return documents, skipped


def _make_upload_file(filename: str, raw: bytes):
    from fastapi import UploadFile

    return UploadFile(filename=filename, file=io.BytesIO(raw))


def load_upload_documents(
    uploads_dir: Path = UPLOADS_DIR,
    *,
    use_cache: bool = True,
    extractor: Callable[[Any], dict[str, object]] | None = None,
    metadata: dict[str, dict[str, object]] | None = None,
) -> tuple[list[SourceDocument], list[str], int]:
    # Never scan the pending-original directory, even if explicitly supplied.
    uploads_root = uploads_dir.resolve()
    if any(part.lower() == "review_uploads" for part in uploads_root.parts):
        logger.warning("待审核目录不参与索引：%s", uploads_dir)
        return [], [], 0
    if not uploads_dir.is_dir():
        logger.warning("uploads 目录不存在：%s", uploads_dir)
        return [], [], 0

    if extractor is None:
        from services.parser_runtime import parse_upload_isolated
        extractor = lambda file: asyncio.run(parse_upload_isolated(file))

    if metadata is None:
        metadata, _ = load_catalog_data()

    cache = _load_json(OCR_CACHE_PATH, {}) if use_cache else {}
    if not isinstance(cache, dict):
        cache = {}

    documents: list[SourceDocument] = []
    skipped: list[str] = []
    cache_hits = 0
    paths = sorted(
        (path for path in uploads_dir.iterdir()
         if path.is_file() and path.resolve().is_relative_to(uploads_root)),
        key=lambda p: p.name,
    )
    logger.info("uploads/ 共发现 %d 个磁盘文件", len(paths))

    for path in paths:
        document_id = stable_upload_id(path.name)
        meta = metadata.get(document_id, {})
        if path.stat().st_size > 50 * 1024 * 1024:
            skipped.append(f"uploads:{path.name} [size-limit]")
            continue
        if not is_published_record(meta):
            skipped.append(f"uploads:{path.name} [{meta.get('review_status')}]")
            continue
        result = _cached_extraction(cache, path, document_id) if use_cache else None
        if result is not None:
            cache_hits += 1
        else:
            try:
                result = extractor(_make_upload_file(path.name, path.read_bytes()))
            except Exception as exc:  # noqa: BLE001 - one bad source must not abort the rebuild
                logger.exception("提取异常，跳过：%s: %s", path.name, exc)
                skipped.append(f"uploads:{path.name} [failed: {exc}]")
                continue
            if not isinstance(result, dict):
                result = {"content": "", "extraction_status": "failed"}
            cache[document_id] = {
                "sig": _cache_signature(path),
                "content": result.get("content") or "",
                "status": result.get("extraction_status") or "none",
            }
            if use_cache:
                _save_ocr_cache(cache)

        content = str(result.get("content") or "")
        status = str(result.get("extraction_status") or result.get("status") or "none").lower()
        if not content.strip() or status in FAILED_EXTRACTION_STATUSES:
            reason = status if content.strip() else f"{status}/empty-text"
            logger.warning("跳过空文本/提取失败：%s (status=%s)", path.name, reason)
            skipped.append(f"uploads:{path.name} [{reason}]")
            continue

        file_info = _index_file_info(document_id, display_upload_name(path.name), meta)
        documents.append(SourceDocument(file_info, content, "uploads", status))

    return documents, skipped, cache_hits


def _document_ids(store: Any) -> set[str]:
    result = store._collection.get(include=["metadatas"])
    return {
        str(meta["document_id"])
        for meta in (result.get("metadatas") or [])
        if isinstance(meta, dict) and meta.get("document_id")
    }


def _tfidf_stores(store: Any) -> Iterable[Any]:
    """Yield shallow store views bound to any TF-IDF collections.

    VectorStore.delete_by_document operates on ``self._collection``. A shallow
    view lets the same public maintenance method clean a non-active legacy
    TF-IDF collection without constructing a different embedder or assuming its
    vector dimension.
    """
    client = getattr(store, "_client", None)
    active_name = getattr(store, "_collection_name", "")
    if client is None:
        return
    for collection in client.list_collections():
        name = getattr(collection, "name", "")
        if "_tfidf_" not in name or name == active_name:
            continue
        view = copy.copy(store)
        view._collection = collection
        view._collection_name = name
        yield view


def delete_orphans(store: Any) -> dict[str, int]:
    removed = {document_id: store.delete_by_document(document_id) for document_id in ORPHAN_DOCUMENT_IDS}
    for legacy_store in _tfidf_stores(store):
        for document_id in ORPHAN_DOCUMENT_IDS:
            removed[document_id] += legacy_store.delete_by_document(document_id)
    return removed


def rebuild(store: Any, documents: list[SourceDocument]) -> dict[str, object]:
    documents = [document for document in documents if is_published_record(document.file_info)]
    desired_ids = {document.document_id for document in documents}
    if len(desired_ids) != len(documents):
        raise ValueError("源文件生成了重复 document_id，已中止以避免相互覆盖")

    before_ids = _document_ids(store)
    stale_ids = sorted(before_ids - desired_ids - set(ORPHAN_DOCUMENT_IDS))
    for document_id in stale_ids:
        store.delete_by_document(document_id)
        logger.info("清理不再属于源并集的旧 document_id：%s", document_id)

    orphan_removed = delete_orphans(store)
    indexed_chunks = 0
    failures: list[str] = []
    per_document: dict[str, int] = {}
    for number, document in enumerate(documents, start=1):
        name = document.file_info.get("name")
        logger.info("[%d/%d] 重建 %s (%s)", number, len(documents), name, document.document_id)
        try:
            # Required for idempotence when a document becomes shorter: upsert
            # alone cannot remove chunk IDs left over from the previous version.
            store.delete_by_document(document.document_id)
            count = int(store.index_document(document.file_info, document.content))
        except Exception as exc:  # noqa: BLE001 - report all failed documents
            logger.exception("索引失败：%s (%s): %s", name, document.document_id, exc)
            failures.append(f"{document.document_id}:{name}: {exc}")
            continue
        per_document[document.document_id] = count
        indexed_chunks += count

    return {
        "requested_documents": len(documents),
        "indexed_documents": len(per_document),
        "indexed_chunks": indexed_chunks,
        "per_document": per_document,
        "stale_ids_removed": stale_ids,
        "orphan_chunks_removed": orphan_removed,
        "failures": failures,
    }


def combine_source_documents(
    *sources: Iterable[SourceDocument],
) -> list[SourceDocument]:
    """Deduplicate by ID; any explicit unpublished source excludes that ID."""
    documents_by_id: dict[str, SourceDocument] = {}
    unpublished_ids: set[str] = set()
    for source in sources:
        for document in source:
            if is_published_record(document.file_info):
                documents_by_id[document.document_id] = document
            else:
                unpublished_ids.add(document.document_id)
    return [document for document_id, document in documents_by_id.items()
            if document_id not in unpublished_ids]


def verify(store: Any, expected_ids: set[str] | None = None) -> dict[str, object]:
    actual_ids = _document_ids(store)
    expected_ids = set(expected_ids or actual_ids)
    samples: list[dict[str, object]] = []
    for label, question, expected_name in SAMPLE_QUESTIONS:
        try:
            hits = store.query(question, top_k=5, filters=None)
        except Exception as exc:  # noqa: BLE001
            logger.warning("抽样检索失败 [%s]：%s", label, exc)
            hits = []
        top = hits[0] if hits else {}
        metadata = top.get("metadata") or {}
        similarity = 1.0 - float(top.get("distance", 1.0)) if top else 0.0
        correct_hit = any(expected_name.lower() in str((hit.get("metadata") or {}).get("filename", "")).lower() for hit in hits)
        sample = {
            "label": label,
            "query": question,
            "hit_count": len(hits),
            "top_filename": metadata.get("filename"),
            "top_document_id": metadata.get("document_id"),
            "top1_similarity": round(similarity, 4),
            "expected_file_in_top5": correct_hit,
        }
        samples.append(sample)
        logger.info(
            "检索 [%s] top1=%s sim=%.4f hits=%d expected_in_top5=%s",
            label,
            sample["top_filename"],
            similarity,
            len(hits),
            correct_hit,
        )

    orphan_ids = actual_ids.intersection(ORPHAN_DOCUMENT_IDS)
    return {
        "collection": getattr(store, "_collection_name", "unknown"),
        "backend": getattr(getattr(store, "_embedder", None), "name", "unknown"),
        "dimension": getattr(getattr(store, "_embedder", None), "dim", None),
        "total_chunks": int(store.count()),
        "distinct_documents": len(actual_ids),
        "document_ids": sorted(actual_ids),
        "expected_documents": len(expected_ids),
        "missing_document_ids": sorted(expected_ids - actual_ids),
        "unexpected_document_ids": sorted(actual_ids - expected_ids),
        "orphan_document_ids": sorted(orphan_ids),
        "samples": samples,
    }


def run(*, verify_only: bool = False, use_cache: bool = True) -> dict[str, object]:
    # Import only after ROOT is on sys.path. No backend or dimension is assumed:
    # this is the same singleton used by the running application.
    from services.knowledge.vector_store import get_vector_store

    store = get_vector_store()
    logger.info(
        "激活集合：%s (backend=%s, dim=%s)",
        getattr(store, "_collection_name", "unknown"),
        getattr(getattr(store, "_embedder", None), "name", "unknown"),
        getattr(getattr(store, "_embedder", None), "dim", "unknown"),
    )

    inline_documents, inline_skipped = load_inline_documents()
    metadata, _ = load_catalog_data()
    upload_documents, upload_skipped, cache_hits = load_upload_documents(
        use_cache=use_cache, metadata=metadata
    )
    # A persisted upload can also have cached inline text. Use one canonical
    # record per ID, with current stored text preferred over disk extraction.
    documents = [document for document in combine_source_documents(upload_documents, inline_documents)
                 if is_published_record(metadata.get(document.document_id, document.file_info))]
    expected_ids = {document.document_id for document in documents}

    result: dict[str, object] = {
        "sources": {
            "files_data_discovered": len(inline_documents) + len(inline_skipped),
            "files_data_indexable": len(inline_documents),
            "uploads_discovered": len(upload_documents) + len(upload_skipped),
            "uploads_indexable": len(upload_documents),
            "union_indexable": len(documents),
            "ocr_cache_hits": cache_hits,
            "skipped": inline_skipped + upload_skipped,
        }
    }
    if not verify_only:
        result["rebuild"] = rebuild(store, documents)
    try:
        result["verification"] = verify(store, expected_ids)
    finally:
        if not store.flush():
            raise RuntimeError("索引刷盘失败；请勿将此次重建视为成功")

    logger.info("重建/验证报告：\n%s", json.dumps(result, ensure_ascii=False, indent=2))
    verification = result["verification"]
    if verification["missing_document_ids"] or verification["orphan_document_ids"]:
        raise RuntimeError("索引覆盖验证失败；请查看 missing/orphan_document_ids")
    if not verify_only and result["rebuild"]["failures"]:
        raise RuntimeError("部分文档索引失败；请查看 rebuild.failures")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="重建当前文件记录与 uploads 中已发布资料的激活向量索引")
    parser.add_argument("--verify-only", action="store_true", help="只验证当前激活集合")
    parser.add_argument("--no-cache", action="store_true", help="忽略 OCR 缓存并重新提取 uploads")
    args = parser.parse_args()
    try:
        run(verify_only=args.verify_only, use_cache=not args.no_cache)
        return 0
    except KeyboardInterrupt:
        logger.warning("用户中断")
        return 130
    except Exception as exc:  # noqa: BLE001
        logger.error("重建失败：%s", exc, exc_info=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
