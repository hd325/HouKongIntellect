"""Read-only summaries of actual retrieval events in ``rag_metrics.jsonl``.

The reducer is independent of the RAG pipeline. Answer events are excluded and
request IDs are deduplicated before aggregation. Naive historical timestamps
are interpreted in Macau/Hong Kong time (UTC+08:00).
"""

from __future__ import annotations

import copy
import json
import math
import re
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Iterable, Mapping
from uuid import uuid4


from config.settings import DATA_DIR

DEFAULT_LOG_PATH = DATA_DIR / "work" / "rag_metrics.jsonl"
LOCAL_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Macau")
TIMEZONE_NAME = "Asia/Macau"
MINIMUM_RECORDS = 5
_LATENCY_BUCKETS = (
    ("<100 ms", 0.0, 100.0),
    ("100–250 ms", 100.0, 250.0),
    ("250–500 ms", 250.0, 500.0),
    ("500–1000 ms", 500.0, 1000.0),
    ("1000–3000 ms", 1000.0, 3000.0),
    ("≥3000 ms", 3000.0, None),
)
_SUBJECT_PATTERNS = (
    ("数学", re.compile(r"数学|數學", re.I)),
    ("化学", re.compile(r"化学|化學", re.I)),
    ("生物", re.compile(r"生物", re.I)),
    ("物理", re.compile(r"物理", re.I)),
    ("英文", re.compile(r"英文|英語|英语|\benglish\b", re.I)),
    ("AI 竞赛", re.compile(r"\benjoy\s*ai\b|文明破曉|文明破晓|智創校園|智创校园", re.I)),
    ("校历/通知", re.compile(r"校历|校曆|通知|行政簡曆|行政简历|服務費|服务费|獲獎名單|获奖名单", re.I)),
    ("历史", re.compile(r"歷史|历史|\bhistory\b", re.I)),
)


def build_retrieval_record(
    chunks: Iterable[Mapping[str, Any]] | None,
    latency_ms: float | None,
    *,
    timestamp: datetime | str | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Build metadata-only telemetry without changing retrieval or its results.

    Existing diagnostics metrics remain authoritative, including a missing
    vector score for a BM25-only result. For plain lists, the first chunk's
    vector score (or legacy score if absent) is used. The measured caller
    latency fills an absent/invalid diagnostic latency; source filenames are
    always read from the returned chunks. Content, questions, snippets and
    unrecognized diagnostic fields are never copied into the log.
    """
    items = list(chunks) if chunks is not None else []
    diagnostic_getter = getattr(chunks, "diagnostics", None)
    diagnostics: Mapping[str, Any] = {}
    if callable(diagnostic_getter):
        try:
            value = diagnostic_getter()
            if isinstance(value, Mapping):
                diagnostics = value
        except Exception:
            # A telemetry-only helper must not turn a successful response into
            # a failure merely because optional diagnostics cannot be read.
            pass
    metric_value = diagnostics.get("metrics")
    metrics = metric_value if isinstance(metric_value, Mapping) else {}
    records = [item for item in items if isinstance(item, Mapping)]
    filenames: set[str] = set()
    document_ids: set[str] = set()
    sources = {"vector": 0, "bm25": 0, "both": 0}
    for item in records:
        metadata = item.get("metadata")
        metadata = metadata if isinstance(metadata, Mapping) else {}
        document_id = item.get("document_id") or metadata.get("document_id")
        if document_id not in (None, ""):
            document_ids.add(str(document_id))
        filename = item.get("filename") or metadata.get("filename")
        if isinstance(filename, str) and filename.strip():
            filenames.add(filename.strip())
        raw_sources = item.get("retrieval_sources")
        if isinstance(raw_sources, (list, tuple, set)):
            source_set = {value for value in raw_sources if isinstance(value, str)}
            if source_set == {"vector"}:
                sources["vector"] += 1
            elif source_set == {"bm25"}:
                sources["bm25"] += 1
            elif source_set == {"vector", "bm25"}:
                sources["both"] += 1
    first = records[0] if records else {}
    first_score = first.get("vector_score") if "vector_score" in first else first.get("score")
    status = diagnostics.get("status")
    if not isinstance(status, str) or not status:
        status = "ok" if records else "no_relevant_context"
    measured_latency = _number(latency_ms, nonnegative=True)
    fallback = {
        "hit_chunk_count": len(items),
        "hit_file_count": len(document_ids),
        "top1_score": _number(first_score),
        "threshold_degraded": status == "degraded",
        "retrieval_sources": sources,
        "retrieval_latency_ms": measured_latency,
        "empty": not items,
    }
    current = _local_datetime(timestamp) if timestamp is not None else datetime.now(LOCAL_TIMEZONE)
    if current is None:
        raise ValueError("timestamp must be an ISO timestamp or datetime")
    result = {
        "request_id": str(request_id) if request_id is not None else str(uuid4()),
        "timestamp": current.isoformat(),
        "event": "retrieval",
        "status": status,
        **{
            key: copy.deepcopy(metrics[key]) if key in metrics else value
            for key, value in fallback.items()
        },
        "filenames": sorted(filenames),
    }
    if _number(result["retrieval_latency_ms"], nonnegative=True) is None:
        result["retrieval_latency_ms"] = measured_latency
    for key in ("threshold_used", "threshold_path"):
        if key in diagnostics:
            result[key] = copy.deepcopy(diagnostics[key])
    return result


def _local_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=LOCAL_TIMEZONE)
    return parsed.astimezone(LOCAL_TIMEZONE)


def _number(value: object, *, nonnegative: bool = False) -> float | None:
    """Missing/invalid values remain missing, rather than becoming zero."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or (nonnegative and number < 0):
        return None
    return number


def _percentile(values: list[float], fraction: float) -> float | None:
    """Linear interpolation, matching NumPy's default percentile method."""
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower), 3)


def _mean(values: list[float], digits: int = 4) -> float | None:
    return round(mean(values), digits) if values else None


def _is_empty(record: Mapping[str, Any]) -> bool:
    return (
        record.get("status") == "no_relevant_context"
        or record.get("empty") is True
        or _number(record.get("hit_chunk_count"), nonnegative=True) == 0
    )


def _aggregate(records: list[dict[str, Any]]) -> dict[str, Any]:
    chunk_counts = [
        value for row in records
        if (value := _number(row.get("hit_chunk_count"), nonnegative=True)) is not None
    ]
    file_counts = [
        value for row in records
        if (value := _number(row.get("hit_file_count"), nonnegative=True)) is not None
    ]
    scores = [
        value for row in records if (value := _number(row.get("top1_score"))) is not None
    ]
    latencies = [
        value for row in records
        if (value := _number(row.get("retrieval_latency_ms"), nonnegative=True)) is not None
    ]
    return {
        "questions": len(records),
        "avg_hit_chunk_count": _mean(chunk_counts),
        "avg_hit_file_count": _mean(file_counts),
        "avg_top1_score": _mean(scores),
        "empty_rate": round(sum(_is_empty(row) for row in records) / len(records), 4) if records else None,
        "p50_latency_ms": _percentile(latencies, 0.50),
        "p95_latency_ms": _percentile(latencies, 0.95),
        "top1_score_sample_count": len(scores),
        "latency_sample_count": len(latencies),
        "status_counts": dict(sorted(Counter(str(row.get("status") or "unknown") for row in records).items())),
    }


def _subjects_for_filename(filename: str) -> set[str]:
    matches = {subject for subject, pattern in _SUBJECT_PATTERNS if pattern.search(filename)}
    return matches or {"未分类"}


def summarize_metric_records(
    records: Iterable[Mapping[str, Any]],
    *,
    days: int = 30,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Aggregate records without modifying them or writing any files.

    The window includes the current Macau calendar date and the preceding
    ``days - 1`` dates, up to ``now``. Missing-event historical records are
    accepted only when they carry retrieval metrics. For duplicate request
    IDs the newest retrieval record wins; without an ID only identical rows
    are deduplicated. Subject counts are the number of questions hitting a
    subject explicitly indicated by a logged filename, so one question may
    contribute to more than one subject.
    """
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 366:
        raise ValueError("days must be an integer between 1 and 366")
    current = _local_datetime(now) if now is not None else datetime.now(LOCAL_TIMEZONE)
    if current is None:
        raise ValueError("now must be a datetime")
    start_date = current.date() - timedelta(days=days - 1)
    window_start = datetime.combine(start_date, time.min, tzinfo=LOCAL_TIMEZONE)
    diagnostics = {
        "records_read": 0,
        "answer_events_excluded": 0,
        "other_events_excluded": 0,
        "invalid_records": 0,
        "invalid_timestamps": 0,
        "outside_window_records": 0,
        "future_records": 0,
        "duplicate_records": 0,
        "legacy_records_without_event": 0,
    }
    unique: dict[str, tuple[datetime, int, dict[str, Any]]] = {}
    for index, record in enumerate(records):
        diagnostics["records_read"] += 1
        if not isinstance(record, Mapping):
            diagnostics["invalid_records"] += 1
            continue
        event = record.get("event")
        if event == "answer":
            diagnostics["answer_events_excluded"] += 1
            continue
        if event not in (None, "", "retrieval"):
            diagnostics["other_events_excluded"] += 1
            continue
        if not any(key in record for key in ("status", "hit_chunk_count", "hit_file_count", "top1_score", "retrieval_latency_ms")):
            diagnostics["invalid_records"] += 1
            continue
        timestamp = _local_datetime(record.get("timestamp"))
        if timestamp is None:
            diagnostics["invalid_timestamps"] += 1
            continue
        if timestamp < window_start:
            diagnostics["outside_window_records"] += 1
            continue
        if timestamp > current:
            diagnostics["future_records"] += 1
            continue
        if event in (None, ""):
            diagnostics["legacy_records_without_event"] += 1
        request_id = record.get("request_id")
        key = (
            "request:" + str(request_id)
            if request_id not in (None, "")
            else "row:" + json.dumps(dict(record), sort_keys=True, ensure_ascii=False, default=str)
        )
        candidate = (timestamp, index, dict(record))
        if key in unique:
            diagnostics["duplicate_records"] += 1
            if candidate[:2] < unique[key][:2]:
                continue
        unique[key] = candidate

    selected = sorted(unique.values(), key=lambda item: (item[0], item[1]))
    rows = [row for _, _, row in selected]
    by_date: dict[date, list[dict[str, Any]]] = defaultdict(list)
    subject_counts: Counter[str] = Counter()
    filename_record_count = 0
    latency_values: list[float] = []
    for timestamp, _, row in selected:
        by_date[timestamp.date()].append(row)
        latency = _number(row.get("retrieval_latency_ms"), nonnegative=True)
        if latency is not None:
            latency_values.append(latency)
        filenames = row.get("filenames")
        if isinstance(filenames, list) and all(isinstance(value, str) for value in filenames):
            filename_record_count += 1
            subjects: set[str] = set()
            for filename in {value.strip() for value in filenames if value.strip()}:
                subjects.update(_subjects_for_filename(filename))
            subject_counts.update(subjects)

    daily = []
    for offset in range(days):
        day = start_date + timedelta(days=offset)
        daily.append({"date": day.isoformat(), **_aggregate(by_date.get(day, []))})
    latency_histogram = [{
        "label": label,
        "count": sum(low <= value and (high is None or value < high) for value in latency_values),
        "min_ms": low,
        "max_ms": high,
    } for label, low, high in _LATENCY_BUCKETS]
    if filename_record_count == 0:
        subject_note = "历史检索日志未记录来源文件名，无法还原科目命中分布。"
    elif filename_record_count < len(rows):
        subject_note = f"仅依据有来源文件名的{filename_record_count}/{len(rows)}次提问统计；旧日志缺失字段未推测补齐。"
    else:
        subject_note = "按实际来源文件名中的明确科目名称统计；同一次提问可命中多科，未知名称归入未分类。"
    return {
        "generated_at": current.isoformat(),
        "timezone": TIMEZONE_NAME,
        "days": days,
        "start_date": start_date.isoformat(),
        "end_date": current.date().isoformat(),
        "record_count": len(rows),
        "minimum_records": MINIMUM_RECORDS,
        "data_insufficient": len(rows) < MINIMUM_RECORDS,
        "summary": _aggregate(rows),
        "daily": daily,
        "latency_histogram": latency_histogram,
        "filename_record_count": filename_record_count,
        "subject_data_insufficient": filename_record_count < MINIMUM_RECORDS,
        "subject_distribution_available": filename_record_count > 0,
        "subject_distribution_note": subject_note,
        "subject_count_unit": "questions_with_subject",
        "subjects": [{"subject": subject, "count": count} for subject, count in sorted(subject_counts.items(), key=lambda item: (-item[1], item[0]))],
        "percentile_method": "linear interpolation at (n-1)*p",
        "diagnostics": diagnostics,
    }


def build_metrics_summary(
    log_path: str | Path = DEFAULT_LOG_PATH,
    *,
    days: int = 30,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Read a JSONL log and return an API-ready summary; never mutate the log."""
    rows: list[Mapping[str, Any]] = []
    lines_read = 0
    malformed_lines = 0
    blank_lines = 0
    log_status = "ok"
    try:
        with Path(log_path).open("r", encoding="utf-8-sig", errors="replace") as handle:
            for line in handle:
                lines_read += 1
                if not line.strip():
                    blank_lines += 1
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    malformed_lines += 1
    except FileNotFoundError:
        log_status = "missing"
    except OSError:
        log_status = "unreadable"
    result = summarize_metric_records(rows, days=days, now=now)
    result["diagnostics"].update({
        "log_status": log_status,
        "lines_read": lines_read,
        "malformed_lines": malformed_lines,
        "blank_lines": blank_lines,
    })
    return result
