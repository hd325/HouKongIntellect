"""
页码感知文本切块器（PageAwareChunker）

职责：将「带页码标记」的 OCR/文本提取结果切分为适合向量检索的语义文本块。

输入约定：
- PDF 经华为云 OCR（recognize_pdf）后，每页以 ``--- 第N页 ---`` 分隔；
- 图片 OCR 结果由调用方统一包裹为 ``--- 第1页 ---``（单页）；
- 纯文本 / Office 文档无页码标记时，整体视为第 1 页。

切块规则：
1. 优先按「自然段落」（空行 / 换行）切分；
2. 段落过长（估算 token > 512）时，在句子边界（。！？；\n）再切分；
3. 绝不跨页切块（每页独立编号）；
4. 每个块产出结构化元数据，携带在「原始文本」中的字符起止位置，
   供前端定位与高亮。

输出块结构：
{
    "chunk_id":   "DOCID_P12_C03",
    "document_id":"DOC_20260721_001",
    "page":       12,
    "content":    "二次函数的图像是一条抛物线...",
    "filename":   "数学教材.pdf",
    "char_start": 1024,
    "char_end":   1400,
    # 以下字段由索引层按需补充（subject / audience / owner / grade 等）
}
"""
import logging
import re
import uuid
from typing import Dict, List, Optional
from services.resource_limits import ParserLimitError

MAX_DOCUMENT_CHUNKS = 512

logger = logging.getLogger("chunker")

# 页码标记：--- 第12页 --- （允许中英文空格/数字）
_PAGE_MARKER = re.compile(r"^---\s*第\s*(\d+)\s*页\s*---$", re.MULTILINE)

# 句子边界（中文 + 英文）
_SENT_SPLIT = re.compile(r"(?<=[。！？!?；;\n])")

# token 估算：中文约 1.5 字符/token；英文按词 *1.3 近似
_MAX_TOKENS = 512
_CHAR_PER_TOKEN = 1.5


def _est_tokens(text: str) -> int:
    cn = sum(1 for c in text if "\u4e00" <= c <= "\u9fa5")
    words = len(re.findall(r"[a-zA-Z0-9]+", text))
    return int(cn / _CHAR_PER_TOKEN + words * 1.3) + 1


class PageAwareChunker:
    """页码感知切块器（无状态，可复用）。"""

    def __init__(self, max_tokens: int = _MAX_TOKENS):
        self.max_tokens = max_tokens

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------
    def chunk(
        self,
        raw_text: str,
        document_id: Optional[str] = None,
        filename: str = "",
        extra_meta: Optional[Dict[str, object]] = None,
    ) -> List[Dict[str, object]]:
        """
        将原始文本切块。

        Args:
            raw_text:   带页码标记的文本（可为空）
            document_id: 文档唯一 ID（缺省自动生成）
            filename:   文件名（写入每个块的 filename 字段）
            extra_meta: 附加元数据（subject/audience/owner/grade 等），并入每个块

        Returns:
            切块列表（可能为空）
        """
        if not raw_text or not raw_text.strip():
            return []
        if len(raw_text) > 1_000_000:
            raise ParserLimitError("索引文字最多 100 萬字元，請分拆資料。")

        if document_id is None:
            document_id = f"DOC_{uuid.uuid4().hex[:16].upper()}"

        # 1) 解析页码区间
        pages = self._parse_pages(raw_text)
        extra = dict(extra_meta or {})

        chunks: List[Dict[str, object]] = []
        for page_no, p_start, p_end in pages:
            page_text = raw_text[p_start:p_end]
            # 2) 段内切块（含长段再切），每个返回 (content, offset_in_page)
            for content, off in self._split_page(page_text):
                content = content.strip()
                if not content:
                    continue
                if len(chunks) >= MAX_DOCUMENT_CHUNKS:
                    raise ParserLimitError("文件索引最多 512 個段落塊，請分拆資料。")
                chunk_id = f"{document_id}_P{page_no}_C{len(chunks) + 1:03d}"
                chunks.append(
                    {
                        "chunk_id": chunk_id,
                        "document_id": document_id,
                        "page": page_no,
                        "content": content,
                        "filename": filename,
                        "char_start": p_start + off,
                        "char_end": p_start + off + len(content),
                        **extra,
                    }
                )
        logger.info(
            f"🧩 切块完成: document={document_id}, 页数={len(pages)}, 块数={len(chunks)}"
        )
        return chunks

    # ------------------------------------------------------------------
    # 内部：页码解析
    # ------------------------------------------------------------------
    def _parse_pages(self, raw_text: str) -> List[tuple]:
        """返回 [(page_no, start, end), ...]，start/end 为 raw_text 中的字符区间。"""
        matches = list(_PAGE_MARKER.finditer(raw_text))
        if not matches:
            # 无页码标记：整篇视为第 1 页
            return [(1, 0, len(raw_text))]

        pages = []
        for i, m in enumerate(matches):
            page_no = int(m.group(1))
            start = m.end()  # 标记之后的正文起点
            end = matches[i + 1].start() if i + 1 < len(matches) else len(raw_text)
            pages.append((page_no, start, end))
        return pages

    # ------------------------------------------------------------------
    # 内部：单页切块
    # ------------------------------------------------------------------
    def _split_page(self, page_text: str) -> List[tuple]:
        """将单页文本切分为 (content, offset_in_page) 列表。"""
        # 先按空行拆段；无空行则按换行
        paragraphs = self._split_paragraphs(page_text)
        result: List[tuple] = []
        for para, off in paragraphs:
            para = para.strip()
            if not para:
                continue
            if _est_tokens(para) <= self.max_tokens:
                result.append((para, off))
            else:
                # 长段在句子边界再切
                result.extend(self._split_long(para, off))
        return result

    @staticmethod
    def _split_paragraphs(page_text: str) -> List[tuple]:
        """按空行拆段，保留每段在 page_text 内的偏移；短段合并入前段。"""
        # 用正则切分，同时记录偏移
        parts = []
        pos = 0
        for seg in re.split(r"\n\s*\n", page_text):
            seg_off = page_text.find(seg, pos)
            if seg_off == -1:
                seg_off = pos
            parts.append((seg, seg_off))
            pos = seg_off + len(seg)

        # 若整页没有空行，回退到按换行拆
        if len(parts) <= 1 and "\n" in page_text:
            parts = []
            pos = 0
            for seg in page_text.split("\n"):
                seg_off = page_text.find(seg, pos)
                if seg_off == -1:
                    seg_off = pos
                parts.append((seg, seg_off))
                pos = seg_off + len(seg) + 1  # +1 跳过换行

        merged: List[tuple] = []
        for seg, off in parts:
            seg = seg.strip()
            if not seg:
                continue
            if merged and len(seg) < 15:
                # 超短段并入前一段（避免碎片块），偏移保持前段起点
                prev_text, prev_off = merged[-1]
                merged[-1] = (prev_text + "\n" + seg, prev_off)
            else:
                merged.append((seg, off))
        return merged

    def _split_long(self, para: str, base_off: int) -> List[tuple]:
        """将超长段落在句子边界切分为 <= max_tokens 的若干子块。"""
        sentences = [s for s in _SENT_SPLIT.split(para) if s.strip()]
        if len(sentences) <= 1:
            # 没有可切分的句子边界，按字符硬切
            return self._hard_split(para, base_off)

        chunks: List[tuple] = []
        cur = ""
        cur_off = base_off
        for s in sentences:
            if cur and _est_tokens(cur + s) > self.max_tokens:
                chunks.append((cur.strip(), cur_off))
                cur = s
                cur_off = base_off + para.find(s, (cur_off - base_off))
            else:
                if not cur:
                    cur_off = base_off + para.find(s, (cur_off - base_off))
                cur += s
        if cur.strip():
            chunks.append((cur.strip(), cur_off))
        return chunks

    def _hard_split(self, para: str, base_off: int) -> List[tuple]:
        """无句子边界时的硬切（按字符预算）。"""
        budget = int(self.max_tokens * _CHAR_PER_TOKEN)
        out = []
        step = max(1, budget)
        for i in range(0, len(para), step):
            seg = para[i : i + step]
            out.append((seg, base_off + i))
        return out
