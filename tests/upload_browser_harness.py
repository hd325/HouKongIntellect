"""Isolated real-browser fixture using the production upload UI and API routes.

Run from the checkout: python tests/upload_browser_harness.py
Files are synthetic; storage and records live only in a TemporaryDirectory.
No model, cloud OCR, real session or knowledge-store write is performed.
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

import main
from services.accounts import create_account_record
from services.upload_analysis import UploadAnalysisError, blank_classification


class FixtureStore:
    def __init__(self):
        self._files = []
        self.contents = {}
        self.sessions = {"browser-fixture": {"account": "fixture-user", "created_at": datetime.now().isoformat()}}

    async def get_session(self, session_id):
        return self.sessions.get(session_id)

    async def get_all_files(self, user_id):
        return list(self._files)

    async def get_file_content(self, file_id):
        return self.contents.get(file_id)

    async def add_file(self, record, content):
        self._files.append(record)
        self.contents[record["id"]] = content

    async def set_session(self, session_id, session):
        self.sessions[session_id] = session


async def fixture_classify(filename, text, api_config):
    await asyncio.sleep(3)
    if filename.startswith("unknown"):
        return blank_classification()
    if filename.startswith("failed"):
        raise UploadAnalysisError("fixture failure")
    return {"subject": "chemistry", "study_period": "third", "material_type": "review_paper",
            "material_custom": "", "notes": "有機化學"}


def fixture_extract(file, *, progress_callback=None, retry=True):
    if progress_callback:
        progress_callback({"type": "ocr_started", "total": 3})
        for completed in range(1, 4):
            time.sleep(1)
            if file.filename.startswith("ocr-failed") and completed == 2:
                raise RuntimeError("fixture OCR failure")
            progress_callback({"type": "ocr_progress", "completed": completed, "total": 3})
    return {"content": file.file.read().decode("utf-8"), "page_count": 3,
            "extraction_status": "success", "error": ""}


async def no_index(record, content):
    return None


def page():
    source = (Path(__file__).resolve().parents[1] / "index.html").read_text(encoding="utf-8")
    style = re.search(r"<style>(.*?)</style>", source, re.S).group(1)
    begin = source.index("        // ===================== 檔案分類彈窗")
    end = source.index("        panelFileInput.addEventListener", begin)
    ui = source[begin:end]
    begin = source.index('    <div class="categorize-dialog-overlay"')
    end = source.index("    <!-- ==================== 檔案預覽彈窗", begin)
    modal = source[begin:end]
    script = """
        let sessionId = 'browser-fixture', logoutInProgress = false;
        const API_BASE = location.origin + '/api';
        function formatFileSize(size) { return size + ' B'; }
        async function loadFiles() {
            const result = await (await fetch('/fixture/results')).json();
            document.getElementById('fixture-result').textContent = JSON.stringify(result);
        }
    """ + ui + """
        function selectFixture(mode) {
            const content = '第三段 化學 複習卷。有機化合物的結構和性質。';
            const file = new File([content], mode + '.txt', { type: 'text/plain' });
            if (mode === 'batch') {
                handleFileSelect([file, new File([content], 'batch-copy.txt', {type:'text/plain'})]);
            } else handleFileSelect([file]);
        }
    """
    buttons = "".join(f'<button type="button" onclick="selectFixture(\'{mode}\')" id="fixture-{mode}">{label}</button>'
                      for mode, label in (("known", "已知分类"), ("unknown", "未知分类"),
                                          ("failed", "AI失败"), ("ocr-failed", "OCR失败"), ("batch", "多文件重复")))
    return '<!doctype html><html lang="zh-Hant" data-theme="light"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>上传功能隔离检查</title><style>' + style + \
           '</style></head><body><main style="padding:32px"><h1>上传功能隔离检查</h1><p>全部资料为测试资料，不写入你的档案库。</p>' + buttons + \
           '<pre id="fixture-result"></pre></main>' + modal + '<script>' + script + '</script></body></html>'


def run():
    with tempfile.TemporaryDirectory(prefix="hk_upload_browser_") as folder:
        root = Path(folder)
        (root / "uploads").mkdir()
        main.data_store = FixtureStore()
        main.VALID_ACCOUNTS.clear()
        main.VALID_ACCOUNTS["fixture-user"] = create_account_record("fixture-only-password", name="測試學生")
        main.UPLOAD_DIR = str(root / "uploads")
        main.DATA_DIR = str(root)
        main.extract_text_from_file = fixture_extract
        main.classify_upload = fixture_classify
        main._index_file_background = no_index
        fixture_app = FastAPI()
        fixture_app.routes.extend(route for route in main.app.routes if getattr(route, "path", None) in
                                  ("/api/upload", "/api/upload/analyze"))

        @fixture_app.get("/", response_class=HTMLResponse)
        async def fixture_page():
            return page()

        @fixture_app.get("/fixture/results")
        async def results():
            return {"files": [{key: item[key] for key in ("name", "subject", "study_period", "material_type", "notes")}
                              for item in main.data_store._files],
                    "physical_files": len(list((root / "uploads").iterdir()))}

        uvicorn.run(fixture_app, host="127.0.0.1", port=8001, log_level="warning")


if __name__ == "__main__":
    run()
