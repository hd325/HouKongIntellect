"""Small, deterministic decisions about when school-file evidence is needed.

This module does not query storage, score providers, or grant permissions. The
caller supplies the current owner's history and intersects any document scope
with its existing permission filter before retrieval.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any, Iterable, Mapping

import zhconv


_FILE_TAG = re.compile(r"\[([^\[\]\n]+\.(?:pdf|docx?|txt|png|jpe?g|gif|xlsx?|pptx?|md|csv|zip|rar|7z))\]", re.I)
_PAGE = re.compile(r"第\s*(\d+)\s*[页頁]|\bp(?:age)?\.?\s*(\d+)\b", re.I)
_DATE = re.compile(r"(?<!\d)(\d{1,2})\s*(?:月|[/.-])\s*(\d{1,2})\s*[日号號]?(?!\d)")
_SCHOOL = re.compile(r"校历|校内|本校|我校|学校|校园|濠江|校长|校训|校区|行政简历|班级|班会|放假|开学|校运会")
_EXAM = re.compile(r"考试(?:安排|范围|时间|日期|通知|提纲)|(?:统测|测验|小测)(?:安排|范围|时间|日期|通知)?|(?:第一|第二|第三)段(?:考|测)")
_FILE_TASK = re.compile(r"(?:这|那|该|上述|上面|之前|上传|附件|引用|提供).{0,5}(?:文档|文件|档案|资料|讲义|课件|试卷)|(?:文件|档案|文档|讲义|课件|校历|试卷|练习册)(?:里|中|内容|讲|写|提|第)|根据.{0,20}(?:文件|档案|资料|讲义|课件)|总结.{0,5}(?:文件|档案|文档)")
_FOLLOWUP = re.compile(r"^(?:那|那么|还有|接着|继续|这|该|上面|刚才|之前|再)|(?:第\s*\d+\s*页|这份文件|这份档案|那份文件|这道题|下一页)")
_SHORT_FOLLOWUP = re.compile(r"^(?:再|继续|接着)?(?:解释|说明|讲|说).{0,8}(?:详细|清楚|一点|一下|下去)[。？！?!]*$")
_SCHEDULE_DOC = re.compile(r"校历|行政简历|行事历|校内|学校|校园|学年.{0,8}安排|开学|放假|校运会|班会|活动安排")
_EXAM_DOC = re.compile(r"考试|统测|测验|小测|考纲|考核|考试提纲|复习范围")
_SUBJECTS = ("数学", "化学", "物理", "生物", "历史", "地理", "英文", "英语", "语文", "公民", "电脑")
_NO_DOCUMENT = "__no_authorized_document__"


def _normal(text: Any) -> str:
    return zhconv.convert(unicodedata.normalize("NFKC", str(text or "")), "zh-cn").casefold().strip()


def _filename(text: Any) -> str:
    return _normal(text).replace("\\", "/").rsplit("/", 1)[-1].strip("[] \t\r\n\"'")


def parse_file_references(question: str, file_references: str = "") -> list[str]:
    """Extract explicit file names without permissive substring matching."""
    values = [part.strip().strip("[]") for part in re.split(r"[\r\n]+", file_references or "") if part.strip()]
    values.extend(match.group(1).strip() for match in _FILE_TAG.finditer(question or ""))
    return list(dict.fromkeys(values))


def answer_file_citation_numbers(text: str) -> list[int]:
    """Only answer prose can establish a used file; thoughts/code are examples."""
    answer = re.sub(r"<think>[\s\S]*?(?:</think>|$)", "", str(text or ""), flags=re.I)
    answer = re.sub(r"(```|~~~)[\s\S]*?\1|`[^`\n]*`", "", answer)
    return [int(value) for value in re.findall(r"\[(\d+)\]", answer)]


def _previous_file_scope(messages: Iterable[Mapping[str, Any]]) -> tuple[list[str], list[str]]:
    """Only the immediately preceding exchange can supply an implicit scope.

    An assistant's candidate citation_map alone is insufficient: its answer
    must actually contain the corresponding numeric marker. Stopping at the
    preceding user turn prevents a later general topic reviving an old file.
    """
    for message in reversed(list(messages)):
        if message.get("is_user"):
            names = parse_file_references(str(message.get("content", "")), str(message.get("file_references", "")))
            return names, []
        citations = message.get("citation_map") or []
        used = {str(number) for number in answer_file_citation_numbers(message.get("content", ""))}
        ids = list(dict.fromkeys(str(item.get("document_id")) for index, item in enumerate(citations, 1)
                                 if str(item.get("index") or index) in used and item.get("document_id")))
        if ids:
            return [], ids
    return [], []


def route_question(question: str, file_references: str = "", recent_messages=None, *,
                   conversation_owner=None, user_id=None, skill_id="none") -> dict[str, Any]:
    """Route generic knowledge separately from file/school-specific questions."""
    q = _normal(question)
    names = parse_file_references(question, file_references)
    route = {"kind": "general", "retrieve_local": False, "reason": "general_knowledge",
             "file_names": names, "document_ids": [], "inherited": False,
             "simple_fact": bool(len(q) <= 80 and re.search(r"什么|哪天|几号|哪年|谁|多少|what|when|who", q)
                                 and not re.search(r"解释|分析|推导|证明|比较|详细|计算|为什么|如何|怎么", q)
                                 and skill_id in (None, "", "none"))}
    if names:
        route.update(kind="file", retrieve_local=True, reason="explicit_file")
        return route
    follows_file = bool(_SHORT_FOLLOWUP.fullmatch(q) or
                        (_FOLLOWUP.search(q) and (_PAGE.search(q) or _FILE_TASK.search(q) or re.search(r"这道题|下一页", q))))
    if follows_file and conversation_owner is not None and conversation_owner == user_id:
        inherited_names, inherited_ids = _previous_file_scope(recent_messages or [])
        if inherited_names or inherited_ids:
            route.update(kind="file", retrieve_local=True, reason="file_followup", inherited=True,
                         file_names=inherited_names, document_ids=inherited_ids)
            return route
    if (_PAGE.search(q) or _FILE_TASK.search(q)) and not (_SCHOOL.search(q) or _EXAM.search(q)):
        route.update(kind="file", retrieve_local=True, reason="missing_file_scope", simple_fact=False)
        return route
    if _SCHOOL.search(q) or _EXAM.search(q):
        route.update(kind="local", retrieve_local=True, reason="school_or_file_question", simple_fact=False)
    return route


def restrict_document_filter(permission_filter: Mapping[str, Any], route: Mapping[str, Any],
                             file_records: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Narrow an existing allowed-ID filter; never turn a name into permission."""
    result = dict(permission_filter)
    if route.get("kind") != "file":
        return result
    allowed = set((permission_filter.get("document_id") or {}).get("$in") or [])
    wanted = {_filename(value) for value in route.get("file_names", [])}
    selected = set(str(value) for value in route.get("document_ids", []) if str(value) in allowed)
    if wanted:
        selected.update(str(record.get("id")) for record in file_records
                        if record.get("id") in allowed and _filename(record.get("name") or record.get("filename")) in wanted)
    result["document_id"] = {"$in": sorted(selected & allowed) or [_NO_DOCUMENT]}
    return result


def _dates(text: str) -> set[tuple[int, int]]:
    return {(int(month), int(day)) for month, day in _DATE.findall(text)
            if 1 <= int(month) <= 12 and 1 <= int(day) <= 31}


def filter_local_evidence(question: str, candidates: Iterable[Mapping[str, Any]], route: Mapping[str, Any]):
    """Reject explainably mismatched dates, pages, document types and entities.

    Hybrid RRF/BM25 scores are ranks rather than probabilities; they are never
    used as a confidence cutoff here. Rejected candidates are not backfilled.
    """
    q = _normal(question)
    wanted_dates = _dates(q)
    wanted_years = set(re.findall(r"(?<!\d)(20\d{2})(?:年|[-/])", q))
    pages = {int(a or b) for a, b in _PAGE.findall(q)}
    subjects = {subject for subject in _SUBJECTS if subject in q}
    entities = {_normal(value) for value in re.findall(r"[「『“\"]([^」』”\"]{2,40})[」』”\"]", question)}
    entities.update(re.findall(r"(?<![a-z0-9])[a-z][a-z0-9_-]{2,}(?![a-z0-9])", q))
    entities -= {"pdf", "docx", "txt", "pptx", "xlsx", "page", "the", "what", "when", "who"}
    accepted, rejected = [], []
    for candidate in candidates:
        item = dict(candidate)
        name = _normal(item.get("filename", ""))
        content = _normal(item.get("content", ""))
        evidence = name + "\n" + content
        reason = None
        if pages and str(item.get("page")) not in {str(page) for page in pages}:
            reason = "page_mismatch"
        elif wanted_dates and not wanted_dates.issubset(_dates(content)):
            reason = "date_not_in_fragment"
        elif wanted_years and not all(year in evidence for year in wanted_years):
            reason = "year_mismatch"
        elif subjects and not any(subject in evidence for subject in subjects):
            reason = "subject_mismatch"
        elif entities and not all(entity in evidence for entity in entities):
            reason = "entity_mismatch"
        elif route.get("kind") != "file":
            if _EXAM.search(q) and not _EXAM_DOC.search(evidence):
                reason = "exam_document_mismatch"
            elif _SCHOOL.search(q) and not _SCHEDULE_DOC.search(evidence) and not any(word in evidence and word in q for word in ("校长", "校训", "校区", "濠江", "班级")):
                reason = "school_document_mismatch"
            elif _FILE_TASK.search(q) and not (_SCHOOL.search(q) or _EXAM.search(q)):
                # A deictic file request with no owner-scoped antecedent must
                # not accidentally summarize an arbitrary accessible file.
                reason = "missing_file_scope"
        if reason:
            rejected.append({"chunk_id": item.get("chunk_id"), "reason": reason})
        else:
            accepted.append(item)
    return accepted, rejected
