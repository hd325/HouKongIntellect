"""Disposable parser process. The parent owns its files, deadline and admission."""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path


def run(manifest_path: str) -> None:
    from services.process_limits import limit_worker
    _job = limit_worker()
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    if manifest.get("operation") == "office_preview":
        from services.file_agent.runtime import office_preview
        try:
            result = office_preview(Path(manifest["input"]), Path(manifest["output"]))
            print(json.dumps({"result": {"path": str(result)}}), flush=True)
        except Exception:
            print(json.dumps({"parser_error": "無法產生 Office 預覽，請核對 LibreOffice 安裝或下載原檔。"}), flush=True)
        return
    from fastapi import UploadFile
    from services.document_parser import extract_text_from_file
    from services.resource_limits import MAX_FILE_BYTES, ParserLimitError
    path = Path(manifest["input"])
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ParserLimitError("檔案超過 50 MB。")
    def emit(value):
        print(json.dumps(value, ensure_ascii=False), flush=True)
    def progress(value):
        emit({"progress": value})
    with path.open("rb") as handle:
        source = UploadFile(filename=manifest["filename"], file=handle)
        try:
            result = extract_text_from_file(source, progress_callback=progress,
                                            retry=manifest.get("retry", False))
            content = str(result.get("content", ""))
            result["is_truncated"] = len(content) > 1_000_000 or bool(result.get("is_truncated"))
            result["content"] = content[:1_000_000]
            # Reject fragment explosions before upload persistence/indexing.
            from services.chunker import PageAwareChunker
            PageAwareChunker().chunk(result["content"], document_id="preflight")
            emit({"result": result})
        except ParserLimitError as error:
            emit({"limit_error": str(error)})
        except Exception:
            emit({"parser_error": "文件解析失敗，請核對文件格式。"})


if __name__ == "__main__":
    run(sys.argv[1])
