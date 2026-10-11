"""
Chroma 向量库封装 + 索引编排（knowledge_chunks 集合）

职责：
- 将「文件切块 → Embedding → 入库」串成后台任务；
- 提供语义检索所需的 query 接口；
- 按文档删除（重建/覆盖时清理旧块）；
- 自带统计接口，供前端展示 RAG 就绪状态。

集合命名：hk_knowledge_{backend}_{dim}，由嵌入器后端决定，避免维度冲突。

降级策略：若 chromadb 未安装或初始化失败，所有方法抛出 ChromaUnavailable，
由调用方（retriever / 上传流程）捕获并优雅降级，不影响主链路。
"""
import logging
import os
import shutil
import uuid
from pathlib import Path
from typing import Dict, List, Optional

from config.settings import CHROMA_DIR as CONFIGURED_CHROMA_DIR
from services.chunker import PageAwareChunker
from services.knowledge.embedder import get_embedder
from services.review_policy import is_published_record

logger = logging.getLogger("vector_store")

# The public copy uses its own configured store, never the private application's.
CHROMA_DIR = str(CONFIGURED_CHROMA_DIR)

# 图片扩展名（需包裹为单页）
_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff", ".tif", ".heic"}

# 允许写入 Chroma 元数据的字段（均为基本类型）
_META_FIELDS = (
    "document_id",
    "page",
    "filename",
    "char_start",
    "char_end",
    "subject",
    "audience",
    "owner",
    "grade",
    "review_status",
    "publisher_role",
    "audience_class",
    "audience_grade",
)


def _index_metadata(file_info: Dict[str, object]) -> Dict[str, object]:
    metadata = {
        "subject": file_info.get("subject", "") or "",
        "audience": file_info.get("audience", "school") or "school",
        "owner": file_info.get("owner", "") or "",
        "grade": file_info.get("grade", "") or "",
    }
    for field in ("review_status", "publisher_role", "audience_class", "audience_grade"):
        if file_info.get(field) is not None:
            metadata[field] = file_info[field]
    return metadata


class ChromaUnavailable(Exception):
    """Chroma 不可用（未安装 / 初始化失败）。"""


class VectorStore:
    def __init__(self, persist_dir: str = CHROMA_DIR):
        try:
            import chromadb
        except ImportError as e:  # pragma: no cover
            raise ChromaUnavailable(
                "chromadb 未安装，无法启用向量检索。请执行 pip install chromadb"
            ) from e

        os.makedirs(persist_dir, exist_ok=True)
        self._persist_dir = persist_dir
        from chromadb.config import Settings

        self._client = chromadb.PersistentClient(
            path=persist_dir, settings=Settings(anonymized_telemetry=False)
        )
        self._embedder = get_embedder()
        # In-process generation used by the hybrid retriever to refresh its
        # cached BM25 corpus whenever this store is mutated.
        self._index_revision = 0
        self._collection_name = f"hk_knowledge_{self._embedder.name}_{self._embedder.dim}"
        self._collection = self._client.get_or_create_collection(
            name=self._collection_name,
            metadata={"hnsw:space": "cosine"},
        )
        logger.info(f"🗄️ Chroma 集合就绪: {self._collection_name} (persist={persist_dir})")

    # ------------------------------------------------------------------
    # 索引编排
    # ------------------------------------------------------------------
    def index_document(self, file_info: Dict[str, object], content: str) -> int:
        """
        切块 → 向量化 → 入库。返回新增/更新的块数量。

        调用方应在后台任务中执行，异常由调用方捕获。
        """
        if not is_published_record(file_info):
            logger.info("⏭️ 文档尚未发布，跳过索引: %s", file_info.get("name"))
            return 0
        if not content or not content.strip():
            logger.info(f"⏭️ 文档无文本，跳过索引: {file_info.get('name')}")
            return 0

        document_id = file_info.get("id")
        filename = file_info.get("name") or file_info.get("original_name") or ""
        ext = os.path.splitext(filename)[1].lower()

        # 图片：统一包裹为「第1页」；其他无页码标记文档也按第1页处理
        if ext in _IMAGE_EXT and "--- 第" not in content:
            indexed_text = f"--- 第1页 ---\n{content}"
        else:
            indexed_text = content

        # Upload/review service supplies scope and publication metadata.
        extra_meta = _index_metadata(file_info)

        chunker = PageAwareChunker()
        chunks = chunker.chunk(
            indexed_text,
            document_id=document_id,
            filename=filename,
            extra_meta=extra_meta,
        )
        if not chunks:
            return 0

        # ⚠️ 注意：不再调用 delete_by_document 先清旧块。
        # 原因：chromadb 1.5.x 的 delete 会产生 HNSW tombstone，
        # 集合在「新进程重开」时极易触发 "Error loading hnsw index" 损坏，
        # 导致整个集合不可用（含整站 /api/chat 重启失败）。
        # 替代方案：chunk_id 由 document_id + 页码 + 段序确定性生成
        # （见 services/chunker.py），重传同一文件会产生完全相同的 chunk_id，
        # 直接 upsert 即覆盖旧块，天然幂等且零 tombstone。

        ids = [c["chunk_id"] for c in chunks]
        docs = [c["content"] for c in chunks]
        metas = []
        for c in chunks:
            metas.append({k: c.get(k) for k in _META_FIELDS if c.get(k) is not None})

        embeddings = self._embedder.embed(docs)
        self._collection.upsert(
            ids=ids,
            documents=docs,
            metadatas=metas,
            embeddings=embeddings,
        )
        self._index_revision += 1
        logger.info(
            f"✅ 入库完成: {filename} → {len(chunks)} 块 (backend={self._embedder.name})"
        )
        return len(chunks)

    # ------------------------------------------------------------------
    # 批量重建（无 delete，规避 HNSW tombstone 重开损坏）
    # ------------------------------------------------------------------
    def bulk_index_documents(self, items: List) -> int:
        """
        批量切块→向量化→入库，用于全量重建。

        ⚠️ 与 index_document 不同：本方法**不调用 delete_by_document**。
        原因：chromadb 1.5.x 在 HNSW 集合上执行 delete（产生 tombstone）后，
        下次重开集合时极易触发 "Error loading hnsw index" 损坏，导致整个集合不可用。
        全量重建场景下，通过「先清空 chroma_db 或 upsert 同 id 覆盖」即可保证幂等，
        无需 delete。

        items: List of (file_info: dict, content: str)
               file_info 需含 id/name/owner/audience/subject（与 index_document 同义）
        返回：新增/更新的块总数。
        """
        chunker = PageAwareChunker()
        all_ids: List[str] = []
        all_docs: List[str] = []
        all_metas: List[Dict[str, object]] = []

        for file_info, content in items:
            if not is_published_record(file_info):
                continue
            if not content or not content.strip():
                continue
            document_id = file_info.get("id")
            filename = file_info.get("name") or file_info.get("original_name") or ""
            ext = os.path.splitext(filename)[1].lower()
            indexed_text = (
                f"--- 第1页 ---\n{content}"
                if ext in _IMAGE_EXT and "--- 第" not in content
                else content
            )
            extra_meta = _index_metadata(file_info)
            chunks = chunker.chunk(
                indexed_text,
                document_id=document_id,
                filename=filename,
                extra_meta=extra_meta,
            )
            for c in chunks:
                all_ids.append(c["chunk_id"])
                all_docs.append(c["content"])
                all_metas.append({k: c.get(k) for k in _META_FIELDS if c.get(k) is not None})

        if not all_ids:
            return 0

        # 分批 upsert（避免单次超大数据包），相同 chunk_id 自然覆盖，保证幂等
        BATCH = 1000
        for i in range(0, len(all_ids), BATCH):
            batch_ids = all_ids[i : i + BATCH]
            batch_docs = all_docs[i : i + BATCH]
            batch_metas = all_metas[i : i + BATCH]
            batch_emb = self._embedder.embed(batch_docs)
            self._collection.upsert(
                ids=batch_ids,
                documents=batch_docs,
                metadatas=batch_metas,
                embeddings=batch_emb,
            )
        self._index_revision += 1
        logger.info(
            f"✅ 批量入库完成：{len(all_ids)} 块 (backend={self._embedder.name})"
        )
        return len(all_ids)

    # ------------------------------------------------------------------
    # 检索
    # ------------------------------------------------------------------
    def query(
        self,
        query_text: str,
        top_k: int = 6,
        filters: Optional[Dict[str, object]] = None,
    ) -> List[Dict[str, object]]:
        """返回 Chroma 原始结果，按相似度升序（最近在前）。"""
        if self._collection.count() == 0:
            return []
        q_emb = self._embedder.embed_query(query_text)
        kwargs = {"query_embeddings": [q_emb], "n_results": top_k}
        if filters:
            kwargs["where"] = filters
        res = self._collection.query(**kwargs)

        out: List[Dict[str, object]] = []
        ids = (res.get("ids") or [[]])[0]
        docs = (res.get("documents") or [[]])[0]
        metas = (res.get("metadatas") or [[]])[0]
        dists = (res.get("distances") or [[]])[0]
        for i, cid in enumerate(ids):
            out.append(
                {
                    "chunk_id": cid,
                    "content": docs[i] if i < len(docs) else "",
                    "metadata": metas[i] if i < len(metas) else {},
                    "distance": dists[i] if i < len(dists) else 1.0,
                }
            )
        return out

    # ------------------------------------------------------------------
    # 维护
    # ------------------------------------------------------------------
    def reset_collection(self) -> None:
        """Recreate this instance's dedicated Chroma directory for a full reindex.

        Reject unrelated files before deleting anything. Closing the client first
        releases Windows file handles; deletion failures stop the rebuild.
        """
        import chromadb
        from chromadb.config import Settings

        persist_dir = Path(self._persist_dir).resolve()
        if persist_dir == Path(persist_dir.anchor) or not persist_dir.is_dir():
            raise ChromaUnavailable("Cannot reset a missing or unsafe Chroma directory")
        database = persist_dir / "chroma.sqlite3"
        if not database.is_file() or database.is_symlink():
            raise ChromaUnavailable("Cannot reset a directory without a Chroma database")
        allowed_files = {"chroma.sqlite3", "chroma.sqlite3-shm", "chroma.sqlite3-wal",
                         "chroma.sqlite3-journal"}
        for entry in persist_dir.iterdir():
            if entry.is_symlink():
                raise ChromaUnavailable("Cannot reset a Chroma directory containing symlinks")
            if entry.is_file() and entry.name in allowed_files:
                continue
            try:
                segment_id = str(uuid.UUID(entry.name))
            except ValueError:
                segment_id = ""
            if entry.is_dir() and entry.name.lower() == segment_id:
                if any(child.is_symlink() for child in entry.rglob("*")):
                    raise ChromaUnavailable("Cannot reset a Chroma directory containing symlinks")
                continue
            raise ChromaUnavailable("Cannot reset a Chroma directory containing unrelated files")

        self._client.close()
        shutil.rmtree(persist_dir)
        self._client = chromadb.PersistentClient(
            path=str(persist_dir), settings=Settings(anonymized_telemetry=False)
        )
        self._collection = self._client.get_or_create_collection(
            name=self._collection_name,
            metadata={"hnsw:space": "cosine"},
        )
        self._index_revision += 1
        logger.info(f"🆕 已重建空集合: {self._collection_name}")

    def delete_by_document(self, document_id: str) -> int:
        try:
            before = self._collection.count()
            self._collection.delete(where={"document_id": document_id})
            after = self._collection.count()
            removed = before - after
            if removed > 0:
                self._index_revision += 1
                logger.info(f"🧹 清理旧块 document={document_id}: {removed} 块")
            return removed
        except Exception as e:  # noqa: BLE001
            logger.warning(f"⚠️ 清理文档块失败 {document_id}: {e}")
            return 0

    def get_chunk(self, chunk_id: str) -> Optional[Dict[str, object]]:
        """按 chunk_id 取回单个块（用于原文定位）。"""
        try:
            res = self._collection.get(ids=[chunk_id])
        except Exception:  # noqa: BLE001
            return None
        if not res or not res.get("ids"):
            return None
        return {
            "chunk_id": res["ids"][0],
            "content": (res.get("documents") or [""])[0],
            "metadata": (res.get("metadatas") or [{}])[0],
        }

    def get_chunk_context(
        self, chunk_id: str, before: int = 3, after: int = 3
    ) -> Optional[Dict[str, object]]:
        """
        取目标块及其前后上下文（按 char_start 全文档顺序，前后各 before/after 段）。

        用于「段落溯源」两级交互的 Modal 完整视图：目标段落高亮 + 上下文段落。
        返回 {target, context_before:[...], context_after:[...]}；块不存在返回 None。
        """
        target = self.get_chunk(chunk_id)
        if not target:
            return None

        meta = target.get("metadata") or {}
        document_id = meta.get("document_id") or target.get("document_id")
        if not document_id:
            return {"target": target, "context_before": [], "context_after": []}

        # 拉取该文档全部块，按 char_start 排序后切片取上下文
        try:
            res = self._collection.get(where={"document_id": document_id})
        except Exception as e:  # noqa: BLE001
            logger.warning(f"⚠️ 取上下文失敗（document={document_id}）: {e}")
            return {"target": target, "context_before": [], "context_after": []}

        all_chunks: List[Dict[str, object]] = []
        ids = (res.get("ids") or [])
        docs = (res.get("documents") or [])
        metas = (res.get("metadatas") or [])
        for i, cid in enumerate(ids):
            m = metas[i] if i < len(metas) else {}
            cs = m.get("char_start")
            all_chunks.append(
                {
                    "chunk_id": cid,
                    "content": docs[i] if i < len(docs) else "",
                    "metadata": m,
                    "char_start": cs if isinstance(cs, (int, float)) else 0,
                }
            )

        # char_start 在全文档内递增（页码块内 offset 累加），以此定序
        all_chunks.sort(key=lambda c: c["char_start"] if c["char_start"] is not None else 0)

        idx = next(
            (i for i, c in enumerate(all_chunks) if c["chunk_id"] == chunk_id), None
        )
        if idx is None:
            return {"target": target, "context_before": [], "context_after": []}

        lo = max(0, idx - before)
        hi = min(len(all_chunks), idx + after + 1)
        return {
            "target": target,
            "context_before": all_chunks[lo:idx],
            "context_after": all_chunks[idx + 1 : hi],
        }

    def count(self) -> int:
        return self._collection.count()

    def flush(self) -> bool:
        """
        强制把 HNSW 向量索引同步刷盘，并返回是否成功。

        为什么需要：chromadb 1.5.x（rust 后端）依赖内部 System 在进程退出 / close 时
        异步刷盘。若进程被外部强杀（例如后台任务包装器在刷盘完成前回收进程），HNSW 的
        .bin 索引文件不会落盘，下一次重开集合即抛 "Error loading hnsw index"，
        导致整站 /api/chat 重启失败。

        本方法显式 close client，强制 rust 后端同步持久化索引；随后重连并做轻量自检
        （count），确认索引确实可重新打开（即已落盘）。失败则降级为依赖默认 atexit 刷盘，
        不影响内存态与后续操作。

        适用范围：全量重建脚本结束前、或任何「希望确保索引已落盘」的受控时点。
        注意：不要在每次单文档上传后调用——会关闭全局单例 client，影响并发查询；
        线上进程靠 uvicorn 优雅退出时的 atexit 刷盘即可。
        """
        try:
            # 关闭前先确认内存态可统计（基本健全性检查）
            _ = self._collection.count()
        except Exception:  # noqa: BLE001
            logger.warning("⚠️ flush: 关闭前 count 自检异常（仍尝试刷盘）")
        try:
            self._client.close()
            logger.info("💾 HNSW 索引已强制刷盘（client 已关闭，索引文件落盘，释放目录锁）")
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning(f"⚠️ flush: client.close 异常（降级 atexit 刷盘）: {e}")
            return False

    def stats(self) -> Dict[str, object]:
        try:
            return {
                "backend": self._embedder.name,
                "dim": self._embedder.dim,
                "collection": self._collection_name,
                "total_chunks": self._collection.count(),
            }
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)}


# ----------------------------------------------------------------------
# 单例 + 工具
# ----------------------------------------------------------------------
_STORE: "VectorStore | None" = None


def get_vector_store() -> VectorStore:
    global _STORE
    if _STORE is None:
        _STORE = VectorStore()
    return _STORE


def reset_vector_store() -> None:
    global _STORE
    _STORE = None
