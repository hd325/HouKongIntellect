"""Production review UI with synthetic accounts, files and temporary storage.

Run from the checkout: python tests/review_browser_harness.py
Open http://127.0.0.1:8001/fixture/student or /fixture/teacher.
The production lifespan never runs; model/OCR/vector writes are disabled.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from unittest.mock import AsyncMock, patch
import tests  # Establish synthetic-only storage before application imports.
import main
from tests.account_fixtures import fixture_accounts


CHECKOUT = Path(__file__).resolve().parents[1]


async def no_index(record, content):
    return None


def no_vector_store():
    """Empty read-only catalog; never initialize the real Chroma directory."""
    return SimpleNamespace(_collection=SimpleNamespace(get=lambda **kwargs: {"ids": [], "documents": [], "metadatas": []}))


async def fixture_classify(filename, text, api_config):
    return {"subject": "chemistry", "study_period": "first", "material_type": "review_paper",
            "material_custom": "", "notes": ""}


def fixture_extract(file, *, progress_callback=None, retry=True):
    if progress_callback:
        progress_callback({"type": "ocr_started", "total": 1})
        progress_callback({"type": "ocr_progress", "completed": 1, "total": 1})
    raw = file.file.read()
    return {"content": raw.decode("utf-8", errors="replace"), "page_count": 1,
            "extraction_status": "success", "error": ""}


async def fixture_parse(file, *, progress_callback=None, retry=True):
    return await asyncio.to_thread(fixture_extract, file,
                                   progress_callback=progress_callback, retry=retry)


def fixture_page(role):
    session_id = "fixture-teacher" if role == "teacher" else "fixture-student"
    account = "fixture-teacher-0" if role == "teacher" else "review-fixture-student"
    user_info = main.public_user_info(account)
    source = (CHECKOUT / "index.html").read_text(encoding="utf-8")
    bootstrap = '<script>localStorage.setItem("sessionId",' + json.dumps(session_id) + ');' + \
                'localStorage.setItem("userInfo",JSON.stringify(' + json.dumps(user_info, ensure_ascii=False) + '));</script>'
    return source.replace("<head>", "<head>" + bootstrap, 1)


def seed_store(root):
    store = main.DataStore()
    for role, account in (("student", "review-fixture-student"), ("teacher", "fixture-teacher-0"), ("other", "review-fixture-other")):
        store._sessions["fixture-" + role] = {"account": account, "user_info": main.public_user_info(account),
                                             "created_at": datetime.now().isoformat(), "current_conversation_id": None}
    specs = [
        ("學生待審化學複習.txt", "review-fixture-student", "school", "pending", ""),
        ("學生班級練習.txt", "review-fixture-student", "class", "pending", ""),
        ("需要修改的資料.txt", "review-fixture-student", "school", "rejected", "請移除個人資料後重新提交"),
        ("學生私人筆記.txt", "review-fixture-student", "personal", "published", ""),
        ("老師化學教學資料.txt", "fixture-teacher-0", "school", "published", ""),
        ("學生已通過複習資料.txt", "review-fixture-student", "school", "published", ""),
        ("另一位學生待審資料.txt", "review-fixture-other", "school", "pending", ""),
    ]
    for name, owner, audience, status, reason in specs:
        file_id = str(uuid.uuid4())
        content = "本文件是隔離測試資料。\n原電池將化學能轉化為電能。\n老師可以查看原件並核准或退回，個人資料直接發布。"
        raw = content.encode("utf-8")
        storage_name = file_id + "_fixture.txt"
        directory = root / ("uploads" if status == "published" else "review_uploads")
        (directory / storage_name).write_bytes(raw)
        store._files.append({
            "id": file_id, "name": name, "original_name": name, "original_filename": name,
            "size": len(raw), "uploaded_at": datetime.now().isoformat(), "owner": owner,
            "audience": audience, "subject": "chemistry", "material_type": "review_paper",
            "material_custom": "", "study_period": "first", "notes": "", "storage_path": storage_name,
            "is_truncated": False, "page_count": 1, "extraction_status": "success",
            "original_sha256": hashlib.sha256(raw).hexdigest(), "text_sha256": hashlib.sha256(raw).hexdigest(),
            "review_status": status, "publisher_role": "teacher" if owner == "fixture-teacher-0" else "student",
            "review_reason": reason, "reviewed_by": "fixture-teacher-0" if status == "rejected" else "",
            "reviewed_at": datetime.now().isoformat() if status == "rejected" else "",
            "review_history": [],
        })
        store._file_contents[file_id] = content
    asyncio.run(store.save_data(strict=True))
    asyncio.run(store._save_sessions())
    return store


def run():
    with tempfile.TemporaryDirectory(prefix="hk_review_browser_") as folder:
        root = Path(folder)
        for name in ("uploads", "review_uploads"):
            (root / name).mkdir()
        main.DATA_DIR = str(root)
        main.CONVERSATIONS_FILE = str(root / "conversations.json")
        main.SESSIONS_FILE = str(root / "sessions.json")
        main.UPLOAD_DIR = str(root / "uploads")
        main.REVIEW_UPLOAD_DIR = str(root / "review_uploads")
        accounts = fixture_accounts()
        accounts["review-fixture-student"] = accounts["review-student"].copy()
        accounts["review-fixture-other"] = accounts["review-other"].copy()
        main.VALID_ACCOUNTS.clear()
        main.VALID_ACCOUNTS.update(accounts)
        main.data_store = seed_store(root)
        main.parse_upload_isolated = fixture_parse
        main.classify_upload = fixture_classify
        main._index_file_background = no_index
        from services.knowledge import vector_store
        vector_store.get_vector_store = no_vector_store
        fixture_app = FastAPI()
        allowed_paths = {"/api/login", "/api/logout", "/api/session", "/api/conversations", "/api/files",
                         "/api/files/{file_id}/preview", "/api/files/{file_id}/download", "/api/files/{file_id}",
                         "/api/reviews", "/api/reviews/{file_id}/decision", "/api/reviews/{file_id}/resubmit",
                         "/api/upload", "/api/upload/analyze", "/static"}
        fixture_app.routes.extend(route for route in main.app.routes if getattr(route, "path", None) in allowed_paths)

        @fixture_app.get("/fixture/student", response_class=HTMLResponse)
        async def student_page():
            return fixture_page("student")

        @fixture_app.get("/fixture/teacher", response_class=HTMLResponse)
        async def teacher_page():
            return fixture_page("teacher")

        @fixture_app.get("/", response_class=HTMLResponse)
        async def landing():
            return '<h1>審核功能隔離檢查</h1><p>所有資料儲存在臨時目錄，不寫入真實檔案庫。</p><a href="/fixture/student">學生端</a> <a href="/fixture/teacher">老師端</a>'

        @fixture_app.get("/fixture/results")
        async def results():
            return {"files": [{key: item.get(key) for key in ("id", "name", "owner", "audience", "review_status", "publisher_role", "review_reason")}
                              for item in main.data_store._files],
                    "published_originals": len(list((root / "uploads").iterdir())),
                    "pending_originals": len(list((root / "review_uploads").iterdir()))}

        # This app intentionally has no production lifecycle hook and no chat/model routes.
        uvicorn.run(fixture_app, host="127.0.0.1", port=8001, log_level="warning")


if __name__ == "__main__":
    run()
