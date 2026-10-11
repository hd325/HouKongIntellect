"""Hybrid vector + BM25 retrieval for the RAG pipeline.

Chroma supplies semantic recall while jieba-tokenized BM25 supplies exact-term
recall. The two ranked lists are fetched concurrently and fused with reciprocal
rank fusion (RRF). Business and permission filters are applied identically to
both paths.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import jieba
from rank_bm25 import BM25Okapi


logger = logging.getLogger("retriever")
jieba.setLogLevel(logging.WARNING)

HIGH = 0.5
FLOOR = 0.2
STEP = 0.1
DEFAULT_MIN_K = 2
# Backward-compatible names for callers which imported the earlier constants.
DEFAULT_SCORE_THRESHOLD = HIGH
ADAPTIVE_FLOOR = FLOOR
NO_RELEVANT_MESSAGE = "未检索到相关资料，无法基于当前文件库作答。"
RRF_K = 60
_TERM_RE = re.compile(r"[a-z0-9]+(?:[-_.][a-z0-9]+)*", re.IGNORECASE)
_ONLY_PUNCTUATION_RE = re.compile(r"^\W+$", re.UNICODE)


def tokenize_for_bm25(text: str) -> List[str]:
    """Tokenize Chinese with jieba and preserve technical Latin terms.

    The supplemental regex keeps strings such as ``COVID-19`` as a complete
    token in addition to jieba's component tokens. This improves exact-term
    recall without compromising Chinese word segmentation.
    """
    normalized = str(text or "").strip().lower()
    if not normalized:
        return []
    tokens = [
        token.strip()
        for token in jieba.cut(normalized, cut_all=False)
        if token.strip() and not _ONLY_PUNCTUATION_RE.match(token.strip())
    ]
    for term in _TERM_RE.findall(normalized):
        if term not in tokens:
            tokens.append(term)
    return tokens


def _compare(actual: object, operator: str, expected: object) -> bool:
    if operator == "$eq":
        return actual == expected
    if operator == "$ne":
        return actual != expected
    if operator == "$in":
        return isinstance(expected, (list, tuple, set)) and actual in expected
    if operator == "$nin":
        return isinstance(expected, (list, tuple, set)) and actual not in expected
    try:
        if operator == "$gt":
            return actual > expected
        if operator == "$gte":
            return actual >= expected
        if operator == "$lt":
            return actual < expected
        if operator == "$lte":
            return actual <= expected
    except TypeError:
        return False
    logger.warning("BM25 遇到不支持的 Chroma filter 操作符：%s", operator)
    return False


def metadata_matches_filter(
    metadata: Dict[str, object], filters: Optional[Dict[str, object]]
) -> bool:
    """Evaluate the Chroma ``where`` subset used by the application."""
    if not filters:
        return True
    for field, condition in filters.items():
        if field == "$and":
            if (
                not isinstance(condition, list)
                or not condition
                or not all(isinstance(child, dict) for child in condition)
                or not all(metadata_matches_filter(metadata, child) for child in condition)
            ):
                return False
            continue
        if field == "$or":
            if (
                not isinstance(condition, list)
                or not condition
                or not all(isinstance(child, dict) for child in condition)
                or not any(metadata_matches_filter(metadata, child) for child in condition)
            ):
                return False
            continue

        actual = metadata.get(field)
        if isinstance(condition, dict):
            if not all(_compare(actual, operator, expected) for operator, expected in condition.items()):
                return False
        elif actual != condition:
            return False
    return True


@dataclass(frozen=True)
class _BM25Snapshot:
    key: tuple[object, ...]
    chunk_ids: List[str]
    documents: List[str]
    metadatas: List[Dict[str, object]]
    model: Optional[BM25Okapi]


class BM25Index:
    """Thread-safe, lazily refreshed BM25 view of the active Chroma collection."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snapshot = _BM25Snapshot((), [], [], [], None)

    @staticmethod
    def _state_key(store: Any) -> tuple[object, ...]:
        return (
            getattr(store, "_persist_dir", None),
            getattr(store, "_collection_name", None),
            int(store.count()),
            int(getattr(store, "_index_revision", 0)),
        )

    def _ensure_current(self, store: Any) -> _BM25Snapshot:
        key = self._state_key(store)
        if self._snapshot.key == key:
            return self._snapshot
        with self._lock:
            key = self._state_key(store)
            if self._snapshot.key == key:
                return self._snapshot
            raw = store._collection.get(include=["documents", "metadatas"])
            chunk_ids = [str(value) for value in (raw.get("ids") or [])]
            documents = [str(value or "") for value in (raw.get("documents") or [])]
            raw_metadatas = raw.get("metadatas") or []
            metadatas = [dict(value or {}) for value in raw_metadatas]

            size = min(len(chunk_ids), len(documents), len(metadatas))
            chunk_ids = chunk_ids[:size]
            documents = documents[:size]
            metadatas = metadatas[:size]
            corpus = [tokenize_for_bm25(document) for document in documents]
            model = BM25Okapi(corpus) if corpus and any(corpus) else None
            self._snapshot = _BM25Snapshot(
                key, chunk_ids, documents, metadatas, model
            )
            logger.info("BM25 内存索引已同步：%d chunks", size)
            return self._snapshot

    def search(
        self,
        store: Any,
        query: str,
        top_k: int,
        filters: Optional[Dict[str, object]] = None,
    ) -> List[Dict[str, object]]:
        snapshot = self._ensure_current(store)
        query_tokens = tokenize_for_bm25(query)
        if snapshot.model is None or not query_tokens or top_k <= 0:
            return []

        scores = snapshot.model.get_scores(query_tokens)
        ranked_indices = sorted(
            range(len(scores)), key=lambda index: float(scores[index]), reverse=True
        )
        results: List[Dict[str, object]] = []
        for index in ranked_indices:
            score = float(scores[index])
            if score <= 0:
                break
            metadata = snapshot.metadatas[index]
            if not metadata_matches_filter(metadata, filters):
                continue
            results.append(
                {
                    "chunk_id": snapshot.chunk_ids[index],
                    "content": snapshot.documents[index],
                    "metadata": metadata,
                    "bm25_score": score,
                }
            )
            if len(results) >= top_k:
                break
        return results


_BM25_INDEX = BM25Index()


class RetrievalResults(list):
    """List-compatible retrieval result with source-grounding diagnostics."""

    def __init__(
        self,
        items: List[Dict[str, object]],
        *,
        status: str,
        message: str,
        threshold_used: Optional[float],
        threshold_path: List[float],
        metrics: Optional[Dict[str, object]] = None,
    ) -> None:
        super().__init__(items)
        self.status = status
        self.message = message
        self.threshold_used = threshold_used
        self.threshold_path = threshold_path
        self.metrics = {
            "hit_chunk_count": len(items),
            "hit_file_count": len({item["document_id"] for item in items}),
            "top1_score": max((item["vector_score"] for item in items if item.get("vector_score") is not None), default=None),
            "threshold_degraded": status == "degraded",
            "retrieval_sources": {
                source: sum(set(item.get("retrieval_sources", [])) == sources for item in items)
                for source, sources in (("vector", {"vector"}), ("bm25", {"bm25"}), ("both", {"vector", "bm25"}))
            },
            "retrieval_latency_ms": 0.0,
            "empty": status == "no_relevant_context",
        }
        self.metrics.update(metrics or {})

    def diagnostics(self) -> Dict[str, object]:
        return {
            "status": self.status,
            "message": self.message,
            "threshold_used": self.threshold_used,
            "threshold_path": self.threshold_path,
            "metrics": self.metrics,
        }


def threshold_schedule(
    high: float = HIGH, floor: float = FLOOR, step: float = STEP
) -> List[float]:
    """Return a stable inclusive high-to-floor threshold schedule."""
    if step <= 0:
        raise ValueError("threshold step must be positive")
    high = max(float(high), float(floor))
    values: List[float] = []
    current = high
    while current > floor:
        values.append(round(current, 4))
        current = round(current - step, 10)
    values.append(round(float(floor), 4))
    return list(dict.fromkeys(values))


def _citation(
    item: Dict[str, object], snippet_chars: int = 300
) -> Dict[str, object]:
    """Build a stable source anchor for the front-end [N] badge."""
    filename = item.get("filename") or "未知文件"
    page = item.get("page") or 1
    return {
        "chunk_id": item.get("chunk_id"),
        "document_id": item.get("document_id"),
        "filename": filename,
        "page": page,
        "char_start": item.get("char_start"),
        "char_end": item.get("char_end"),
        "snippet": str(item.get("content") or "")[:snippet_chars],
        "label": f"《{filename}》第{page}页",
    }


def _to_item(
    raw: Dict[str, object],
    *,
    vector_score: Optional[float] = None,
    bm25_score: Optional[float] = None,
) -> Dict[str, object]:
    metadata = dict(raw.get("metadata") or {})
    similarity = round(vector_score, 4) if vector_score is not None else None
    item = {
        "chunk_id": raw.get("chunk_id"),
        "document_id": metadata.get("document_id"),
        "page": metadata.get("page"),
        "filename": metadata.get("filename"),
        "content": raw.get("content") or "",
        "char_start": metadata.get("char_start"),
        "char_end": metadata.get("char_end"),
        "subject": metadata.get("subject"),
        "grade": metadata.get("grade"),
        "audience": metadata.get("audience"),
        "owner": metadata.get("owner"),
        # Backward-compatible vector similarity field used by existing callers.
        "score": similarity if similarity is not None else 0.0,
        "vector_score": similarity,
        "bm25_score": round(bm25_score, 4) if bm25_score is not None else None,
        "metadata": metadata,
    }
    item["citation"] = _citation(item)
    return item


def _vector_search(
    store: Any,
    query: str,
    candidate_k: int,
    filters: Optional[Dict[str, object]],
    score_threshold: float,
    min_k: int = DEFAULT_MIN_K,
) -> List[Dict[str, object]]:
    raw_results = store.query(query, top_k=candidate_k, filters=filters)
    scored = [
        (raw, 1.0 - float(raw.get("distance", 1.0))) for raw in raw_results
    ]
    schedule = threshold_schedule(score_threshold, FLOOR, STEP)
    required = max(1, min(int(min_k), candidate_k))
    selected: List[tuple[Dict[str, object], float]] = []
    selected_threshold: Optional[float] = None
    attempted: List[float] = []
    for threshold in schedule:
        attempted.append(threshold)
        candidates = [pair for pair in scored if pair[1] >= threshold]
        if len(candidates) >= required:
            selected = candidates
            selected_threshold = threshold
            break
        if threshold == FLOOR and candidates:
            # The floor is terminal: return partial coverage rather than discard
            # the only grounded result merely because min_k was not reached.
            selected = candidates
            selected_threshold = threshold

    if selected_threshold is None:
        logger.info(
            "向量阈值路径 %s 无相关结果（floor=%s）", attempted, FLOOR
        )
        return []

    degraded = selected_threshold < score_threshold
    results: List[Dict[str, object]] = []
    for raw, similarity in selected[:candidate_k]:
        item = _to_item(raw, vector_score=similarity)
        item["threshold_used"] = selected_threshold
        item["threshold_path"] = attempted
        item["threshold_degraded"] = degraded
        results.append(item)
    if degraded:
        logger.info(
            "向量召回逐级降阈值：path=%s selected=%s hits=%d min_k=%d",
            attempted,
            selected_threshold,
            len(results),
            required,
        )
    return results


def _bm25_search(
    store: Any,
    query: str,
    candidate_k: int,
    filters: Optional[Dict[str, object]],
) -> List[Dict[str, object]]:
    return [
        _to_item(raw, bm25_score=float(raw["bm25_score"]))
        for raw in _BM25_INDEX.search(store, query, candidate_k, filters)
    ]


def rrf_fuse(
    vector_results: List[Dict[str, object]],
    bm25_results: List[Dict[str, object]],
    top_k: int,
    rrf_k: int = RRF_K,
) -> List[Dict[str, object]]:
    """Fuse two ranked lists with ``sum(1 / (rrf_k + rank))``."""
    fused: Dict[str, Dict[str, object]] = {}
    for source, ranked in (("vector", vector_results), ("bm25", bm25_results)):
        for rank, item in enumerate(ranked, start=1):
            chunk_id = str(item.get("chunk_id") or "")
            if not chunk_id:
                continue
            if chunk_id not in fused:
                fused[chunk_id] = {
                    **item,
                    "rrf_score": 0.0,
                    "retrieval_sources": [],
                }
            merged = fused[chunk_id]
            merged["rrf_score"] = float(merged["rrf_score"]) + 1.0 / (rrf_k + rank)
            merged["retrieval_sources"].append(source)
            if source == "vector":
                merged["score"] = item.get("score", 0.0)
                merged["vector_score"] = item.get("vector_score")
            else:
                merged["bm25_score"] = item.get("bm25_score")

    ranked_fused = sorted(
        fused.values(),
        key=lambda item: (
            float(item.get("rrf_score") or 0.0),
            float(item.get("vector_score") or 0.0),
            float(item.get("bm25_score") or 0.0),
        ),
        reverse=True,
    )
    for item in ranked_fused:
        item["rrf_score"] = round(float(item["rrf_score"]), 6)
    return ranked_fused[:top_k]


def retrieve(
    query: str,
    top_k: int = 8,
    filters: Optional[Dict[str, object]] = None,
    score_threshold: float = DEFAULT_SCORE_THRESHOLD,
    min_k: int = DEFAULT_MIN_K,
) -> RetrievalResults:
    """Run vector and BM25 recall concurrently, then return RRF-ranked chunks."""
    t0 = time.perf_counter()
    if not query or not query.strip() or top_k <= 0:
        return RetrievalResults(
            [],
            status="no_relevant_context",
            message=NO_RELEVANT_MESSAGE,
            threshold_used=None,
            threshold_path=[],
            metrics={"retrieval_latency_ms": (time.perf_counter() - t0) * 1000},
        )

    from services.knowledge.vector_store import ChromaUnavailable, get_vector_store

    try:
        store = get_vector_store()
    except ChromaUnavailable as exc:
        logger.warning("向量库不可用，检索降级为空：%s", exc)
        return RetrievalResults(
            [],
            status="retrieval_unavailable",
            message=NO_RELEVANT_MESSAGE,
            threshold_used=None,
            threshold_path=[],
            metrics={"retrieval_latency_ms": (time.perf_counter() - t0) * 1000},
        )

    candidate_k = top_k * 2
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="hybrid-retrieval") as pool:
        vector_future = pool.submit(
            _vector_search,
            store,
            query,
            candidate_k,
            filters,
            score_threshold,
            min_k,
        )
        bm25_future = pool.submit(_bm25_search, store, query, candidate_k, filters)
        try:
            vector_results = vector_future.result()
        except Exception as exc:  # noqa: BLE001 - retain BM25 if Chroma query fails
            logger.warning("向量召回失败，保留 BM25 结果：%s", exc)
            vector_results = []
        try:
            bm25_results = bm25_future.result()
        except Exception as exc:  # noqa: BLE001 - retain vector retrieval if BM25 fails
            logger.warning("BM25 召回失败，保留向量结果：%s", exc)
            bm25_results = []

    fused = rrf_fuse(vector_results, bm25_results, top_k)
    vector_anchor = next(
        (item for item in vector_results if item.get("threshold_used") is not None),
        None,
    )
    threshold_used = (
        float(vector_anchor["threshold_used"]) if vector_anchor is not None else None
    )
    threshold_path = (
        list(vector_anchor.get("threshold_path") or []) if vector_anchor else []
    )
    degraded = bool(vector_anchor and vector_anchor.get("threshold_degraded"))
    if fused:
        status = "degraded" if degraded else "ok"
        message = (
            f"高阈值结果不足，已降至 {threshold_used:.1f} 保持资料覆盖。"
            if degraded and threshold_used is not None
            else "已检索到相关资料。"
        )
    else:
        status = "no_relevant_context"
        message = NO_RELEVANT_MESSAGE
    results = RetrievalResults(
        fused,
        status=status,
        message=message,
        threshold_used=threshold_used,
        threshold_path=threshold_path,
    )
    results.metrics["retrieval_latency_ms"] = (time.perf_counter() - t0) * 1000
    logger.info(
        "混合检索：query='%s' vector=%d bm25=%d fused=%d",
        query[:40],
        len(vector_results),
        len(bm25_results),
        len(results),
    )
    return results


def format_reference_block(chunks: List[Dict[str, object]]) -> str:
    """Format retrieved chunks as numbered prompt references."""
    if not chunks:
        return ""
    lines = []
    for index, chunk in enumerate(chunks, 1):
        page = chunk.get("page") or 1
        filename = chunk.get("filename") or "资料"
        lines.append(
            f"[{index}] 来源：《{filename}》第{page}页\n{chunk.get('content', '')}"
        )
    return "\n---\n".join(lines)


def build_citation_payload(
    chunks: List[Dict[str, object]],
) -> List[Dict[str, object]]:
    """Build the citation payload consumed by the front end."""
    payload = []
    for index, chunk in enumerate(chunks, 1):
        citation = dict(chunk.get("citation") or _citation(chunk))
        payload.append(
            {
                "index": index,
                "chunk_id": chunk.get("chunk_id"),
                "document_id": chunk.get("document_id"),
                "page": chunk.get("page"),
                "filename": chunk.get("filename"),
                "snippet": (chunk.get("content") or "")[:200],
                "char_start": chunk.get("char_start"),
                "char_end": chunk.get("char_end"),
                "score": chunk.get("score"),
                "bm25_score": chunk.get("bm25_score"),
                "rrf_score": chunk.get("rrf_score"),
                "retrieval_sources": chunk.get("retrieval_sources"),
                "threshold_used": chunk.get("threshold_used"),
                "threshold_degraded": chunk.get("threshold_degraded", False),
                "citation": citation,
            }
        )
    return payload
