"""Build an account-scoped classroom corpus from explicitly reviewed evidence.

The sync command only creates candidates. This command never promotes keyword
matches or automatic speaker labels into verified chemistry or teacher style.
Private curation.json contains sources, concepts, teachers, style_rules and
optional catchphrases; all source quotes must match the saved original
paragraph and content version.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def build(folder: Path, owner_account: str) -> dict:
    if not owner_account.strip():
        raise ValueError("A site owner account is required; recordings are private by default")
    curation = read_json(folder / "curation.json")
    if curation.get("schema_version") != 1:
        raise ValueError("Unsupported curation schema")
    coverage = read_json(folder / "coverage.json") if (folder / "coverage.json").exists() else {}
    inventory = read_json(folder / "inventory.json")
    records = {str(record.get("recordingId", record.get("id", ""))): record
               for record in inventory.get("recordings", [])}
    if any(not recording_id or not all(char.isalnum() or char in "_-" for char in recording_id)
           for recording_id in records):
        raise ValueError("Unsafe recording ID in inventory")
    teachers = {teacher["id"]: teacher for teacher in curation.get("teachers", [])}
    chunks = []
    seen = set()
    for source in curation.get("sources", []):
        source_id = source["id"]
        if source_id in seen:
            raise ValueError(f"Duplicate source: {source_id}")
        seen.add(source_id)
        recording_id = str(source["recording_id"])
        if recording_id not in records:
            raise ValueError(f"Source {source_id} is outside this inventory")
        transcript = read_json(folder / "transcripts" / f"{recording_id}.json")
        if transcript.get("status") != "complete":
            raise ValueError(f"Source {source_id} transcript has not been completely read")
        if not source.get("content_version") or source["content_version"] != transcript.get("content_version"):
            raise ValueError(f"Source {source_id} needs review against the current transcript version")
        paragraphs = {paragraph["paragraph"]: paragraph for paragraph in transcript.get("paragraphs", [])}
        numbers = source.get("paragraphs")
        if not isinstance(numbers, list) or not numbers or any(type(number) is not int for number in numbers):
            raise ValueError(f"Source {source_id} requires original paragraph numbers")
        selected = [paragraphs[number] for number in numbers]
        text = "\n".join(paragraph["text"] for paragraph in selected)
        quote = source.get("quote", "")
        if not quote or quote not in text:
            raise ValueError(f"Source {source_id} quote is not present verbatim in its original paragraphs")
        # Reviewing chemical correctness and identifying the classroom speaker
        # are separate checks. A raw diarization label is not a teacher identity.
        if source.get("chemistry_verified") is not True:
            continue
        teacher_id = source.get("teacher_id")
        teacher = teachers.get(teacher_id, {})
        speaker = "teacher" if source.get("speaker_role_verified") is True else "unknown"
        chunk = {"id": source_id, "recording_id": recording_id,
                 "title": transcript.get("title") or records[recording_id].get("title") or "化学课堂",
                 "text": quote, "keywords": source.get("keywords", []),
                 "paragraph": selected[0]["paragraph"], "paragraph_end": selected[-1]["paragraph"],
                 "speaker": speaker, "confidence": "verified", "content_version": source["content_version"],
                 "teacher_identity_verified": teacher.get("identity_verified") is True,
                 "verification_scope": "transcript_text_and_chemistry",
                 "purpose": source.get("purpose", "knowledge_evidence"),
                 "review_note": source.get("review_note", "")}
        if teacher_id:
            chunk["teacher_id"] = teacher_id
        if source.get("date"):
            chunk["date"] = source["date"]
        # Timestamp strings are supplied only after their units/meaning have
        # been reviewed. Never guess seconds versus milliseconds from a number.
        for field in ("timestamp_start", "timestamp_end"):
            if isinstance(source.get(field), str):
                chunk[field] = source[field]
        chunks.append(chunk)
    by_id = {chunk["id"]: chunk for chunk in chunks}
    concepts = []
    for concept in curation.get("concepts", []):
        evidence = concept.get("source_ids", [])
        if not evidence or any(source_id not in by_id for source_id in evidence):
            raise ValueError(f"Concept {concept.get('id')} lacks valid reviewed evidence")
        concepts.append({**concept, "knowledge_status": "reviewed",
                         "derivation": concept.get("derivation", "system_inference_from_classroom")})
        search_words = [concept.get("name", ""), *concept.get("aliases", [])]
        for source_id in evidence:
            by_id[source_id]["keywords"] = sorted(set(by_id[source_id]["keywords"] +
                                                      [word for word in search_words if isinstance(word, str) and word]))
            by_id[source_id].setdefault("knowledge_notes", []).append({
                key: concept.get(key, [] if key in ("conditions", "pitfalls", "source_ids") else "")
                for key in ("name", "explanation", "conditions", "pitfalls", "source_ids", "derivation")
            })
    accepted_rules, pending_rules = [], []
    for rule in curation.get("style_rules", []):
        evidence = [by_id[source_id] for source_id in rule.get("source_ids", []) if source_id in by_id]
        teacher = teachers.get(rule.get("teacher_id"), {})
        valid = (rule.get("reviewed") is True and isinstance(rule.get("rule"), str) and bool(rule["rule"].strip())
                 and teacher.get("identity_verified") is True
                 and len({source["recording_id"] for source in evidence}) >= 2
                 and len(evidence) == len(rule.get("source_ids", []))
                 and all(source.get("teacher_id") == rule.get("teacher_id")
                         and source["speaker"] == "teacher" for source in evidence))
        (accepted_rules if valid else pending_rules).append(rule)
    examples = []
    for example in curation.get("teaching_examples", []):
        if example.get("reviewed") is not True:
            continue
        teacher = teachers.get(example.get("teacher_id"), {})
        ids = example.get("source_ids", [])
        if (teacher.get("identity_verified") is not True or not ids or any(sid not in by_id for sid in ids)
                or any(by_id[sid].get("teacher_id") != example.get("teacher_id") or by_id[sid]["speaker"] != "teacher" for sid in ids)):
            raise ValueError(f"Teaching example {example.get('id')} lacks verified same-teacher evidence")
        if not isinstance(example.get("question"), str) or not isinstance(example.get("response"), str):
            raise ValueError(f"Teaching example {example.get('id')} lacks a worked demonstration")
        examples.append({**example, "derivation": "system_adaptation_of_reviewed_teaching"})
    raw_catchphrases = curation.get("catchphrases", [])
    if not isinstance(raw_catchphrases, list):
        raise ValueError("Catchphrases must be a list")
    catchphrases, catchphrase_ids = [], set()
    for catchphrase in raw_catchphrases:
        if not isinstance(catchphrase, dict):
            raise ValueError("Each catchphrase must be an object")
        if catchphrase.get("reviewed") is not True:
            continue
        phrase_id = catchphrase.get("id")
        teacher_id = catchphrase.get("teacher_id")
        phrase = catchphrase.get("phrase")
        ids = catchphrase.get("source_ids")
        routes = catchphrase.get("routes")
        if (not isinstance(phrase_id, str) or not phrase_id.strip()
                or phrase_id in catchphrase_ids
                or not isinstance(teacher_id, str) or not teacher_id.strip()
                or not isinstance(phrase, str) or not phrase.strip() or len(phrase) > 30
                or any(character in phrase for character in "\r\n\t")
                or not isinstance(catchphrase.get("use_when"), str)
                or not isinstance(ids, list) or not ids
                or any(not isinstance(source_id, str) or not source_id.strip() for source_id in ids)
                or not isinstance(routes, list)
                or any(not isinstance(route, str) or not route.strip() for route in routes)):
            raise ValueError(f"Catchphrase {phrase_id!r} has invalid fields")
        teacher = teachers.get(teacher_id, {})
        if (teacher.get("identity_verified") is not True
                or any(source_id not in by_id for source_id in ids)):
            raise ValueError(f"Catchphrase {phrase_id} lacks verified same-teacher evidence")
        evidence = [by_id[source_id] for source_id in ids]
        if (len({source["recording_id"] for source in evidence}) < 2
                or any(source.get("teacher_id") != teacher_id or source["speaker"] != "teacher"
                       or source.get("teacher_identity_verified") is not True
                       or source["confidence"] != "verified" for source in evidence)):
            raise ValueError(f"Catchphrase {phrase_id} requires the same identified teacher across recordings")
        if any(phrase not in source["text"] for source in evidence):
            raise ValueError(f"Catchphrase {phrase_id} is not present verbatim in every reviewed source")
        catchphrase_ids.add(phrase_id)
        catchphrases.append(dict(catchphrase))
    profile = {"schema_version": 1, "teachers": list(teachers.values()),
               "verified_rules": accepted_rules, "pending_rules": pending_rules,
               "verified_examples": examples, "verified_catchphrases": catchphrases,
               "teaching_version": curation.get("teaching_version", "1.0"),
               "note": "Identity confirmation is independent of transcript speaker labels."}
    payload = {"schema_version": 1, "owner_account": owner_account,
               "status": "ready" if inventory.get("inventory_complete") and coverage.get("sync_finished")
               and coverage.get("count_matches_inventory") and not coverage.get("transcript_partial", 0)
               and not coverage.get("transcript_error", 0) else "partial",
               "style_verified": bool(accepted_rules), "coverage": coverage, "chunks": chunks}
    if curation.get("active_teacher_id") in teachers:
        payload["active_teacher_id"] = curation["active_teacher_id"]
    version_data = {"chunks": chunks, "profile": profile, "concepts": concepts}
    payload["version"] = "1-" + hashlib.sha256(json.dumps(version_data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]
    profile["version"] = payload["version"]
    payload["built_at"] = datetime.now(timezone.utc).isoformat()
    write_json(folder / "corpus.json", payload)
    write_json(folder / "teacher-profile.json", profile)
    write_json(folder / "chemistry-keywords.json", {"schema_version": 1, "version": payload["version"], "entries": concepts})
    write_json(folder / "source-index.json", {"schema_version": 1, "version": payload["version"],
               "sources": [{key: chunk[key] for key in ("id", "recording_id", "title", "content_version", "paragraph", "paragraph_end")} for chunk in chunks]})
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    from config.settings import DATA_DIR
    parser.add_argument("--data-dir", type=Path, default=Path(DATA_DIR) / "private" / "chemistry-classroom")
    parser.add_argument("--owner-account", required=True, help="Existing site account authorized to use these recordings")
    args = parser.parse_args()
    corpus = build(args.data_dir.resolve(), args.owner_account)
    print(json.dumps({"version": corpus["version"], "sources": len(corpus["chunks"]),
                      "style_verified": corpus["style_verified"], "status": corpus["status"]}))


if __name__ == "__main__":
    main()
