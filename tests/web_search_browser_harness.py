"""Offline production-HTML fixture for web search and fetch UI (port 8012).

All accounts, replies and sources are synthetic. Never imports main or calls APIs.
Run: venv312/Scripts/python.exe tests/web_search_browser_harness.py
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
MESSAGES = []
REQUESTS = []
SOURCE_REQUESTS = []
USER = {"account": "web-search-fixture", "name": "網頁測試", "class": "高二測試班", "role": "學生"}
CONVERSATION = "web-fixture-conversation"
GROUPED_FILENAME = "高二化學下學期預習與課堂綜合練習暨課程重點整理2026-2027學年完整修訂版.pdf"
GROUPED_CHUNKS = {
    "group-a-three-first": ("group-doc-a", 3),
    "group-a-one": ("group-doc-a", 1),
    "group-a-three-second": ("group-doc-a", 3),
    "group-b-two": ("group-doc-b", 2),
}


def envelope(data):
    return {"code": 200, "message": "fixture", "data": data}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, *_):
        pass

    def send_json(self, data):
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        route = urlparse(self.path).path
        if route == "/fixture":
            source = (ROOT / "index.html").read_text(encoding="utf-8")
            # Production reserves 8000/8001 for dev; keep this fixture isolated on 8012.
            source = source.replace("window.location.port === '8001'", "(window.location.port === '8001' || window.location.port === '8012')", 1)
            bootstrap = '<script>localStorage.setItem("sessionId","web-search-fixture");' + \
                'localStorage.setItem("userInfo",JSON.stringify(' + json.dumps(USER, ensure_ascii=False) + '));</script>'
            raw = source.replace("<head>", "<head>" + bootstrap, 1).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        elif route == "/fixture/requests":
            self.send_json(REQUESTS)
        elif route == "/fixture/source-requests":
            self.send_json(SOURCE_REQUESTS)
        elif route.startswith("/api/knowledge/chunks/"):
            chunk_id = route.rsplit("/", 1)[-1]
            SOURCE_REQUESTS.append(chunk_id)
            document_id, page = GROUPED_CHUNKS.get(chunk_id, ("synthetic-doc", 2))
            self.send_json(envelope({"document_id": document_id, "filename": GROUPED_FILENAME,
                                     "page": page, "file_type": "txt", "content": "精確片段：" + chunk_id,
                                     "context_before": [], "context_after": []}))
        elif route == "/api/session":
            self.send_json(envelope({"user_info": USER, "current_conversation": {"id": CONVERSATION, "messages": MESSAGES.copy()}}))
        elif route == "/api/skills":
            self.send_json(envelope({"skills": [{"id": "none", "label": "不啟用 Skill", "status": "disabled", "available": True}]}))
        elif route == "/api/files":
            self.send_json(envelope({"files": []}))
        elif route == "/api/conversations":
            history = [{"id": CONVERSATION, "title": "網頁測試歷史", "message_count": len(MESSAGES)}] if MESSAGES else []
            self.send_json(envelope({"conversations": history}))
        elif route.startswith("/api/"):
            self.send_json(envelope({}))
        elif route.startswith("/static/"):
            asset = route.removeprefix("/static/")
            allowed = {"tailwindcss.js", "marked.js", "font-awesome.css", "logo.png", "fonts/fontawesome-webfont.woff2"}
            if asset not in allowed:
                self.send_error(404)
                return
            self.path = "/" + asset
            super().do_GET()
        else:
            self.send_error(404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        route = urlparse(self.path).path
        if route == "/fixture/reset":
            MESSAGES.clear()
            REQUESTS.clear()
            SOURCE_REQUESTS.clear()
            self.send_json(envelope({}))
            return
        if route.endswith("/switch"):
            self.send_json(envelope({"messages": MESSAGES.copy()}))
            return
        if route != "/api/chat":
            self.send_json(envelope({"conversation_id": CONVERSATION}))
            return
        REQUESTS.append(body)
        question = body.get("question", "")
        search = bool(body.get("search"))
        partial = "摘要" in question
        interrupted = "处理中断" in question
        unavailable = "直链失败" in question
        failed = "失败" in question and not partial
        empty = "无结果" in question
        mixed = "混合" in question
        grouped = "多段文件" in question
        status = "disabled" if not search else "error" if failed else "no_results" if empty else "partial" if partial else "success"
        messages = {"success": "Google 搜尋完成，已透過 DeepSeek Fetch 讀取正文", "partial": "部分網頁只能取得搜尋摘要",
                    "error": "Google 搜尋暫時無法使用", "no_results": "未找到可用的網頁資料", "disabled": ""}
        web_state = {"status": status, "message": messages[status]}
        sources = []
        if search and not failed and not empty:
            sources = [{"id": "W1", "title": "English science <img src=x onerror=window.fixtureInjection=1>",
                        "url": "https://www.example.org/science?language=en", "snippet": "合成資料：公開英文科學正文。<script>window.fixtureInjection=1</script>",
                        "content_status": "snippet_only" if partial else "fetched", "truncated": not partial},
                       {"id": "W2", "title": "中文摘要來源", "url": "https://www.example.com/reference",
                        "snippet": "另一項搜尋摘要", "content_status": "snippet_only"},
                       {"id": "W3", "title": "不安全網址必須忽略", "url": "javascript:window.fixtureInjection=1", "content_status": "fetched"}]
        if unavailable:
            sources = [{"id": "W1", "title": "未能讀取的指定網頁", "url": "https://www.example.org/unavailable", "snippet": "", "content_status": "unavailable"}]
        citations = [{"index": 1, "filename": "合成課本.pdf", "page": 2, "document_id": "synthetic-doc", "chunk_id": "synthetic-chunk", "vector_score": 0.92},
                     {"index": 2, "filename": "未引用候選.pdf", "page": 8, "document_id": "unused-doc", "chunk_id": "unused-chunk"}] if mixed else []
        if grouped:
            citations = [{"index": 1, "filename": GROUPED_FILENAME, "page": 1, "document_id": "group-doc-a", "chunk_id": "group-a-one"},
                         {"index": 2, "filename": GROUPED_FILENAME, "page": 3, "document_id": "group-doc-a", "chunk_id": "group-a-three-second"},
                         {"index": 3, "filename": GROUPED_FILENAME, "page": 3, "document_id": "group-doc-a", "chunk_id": "group-a-three-first"},
                         {"index": 4, "filename": GROUPED_FILENAME, "page": 2, "document_id": "group-doc-b", "chunk_id": "group-b-two"},
                         {"index": 5, "filename": "未引用候選.pdf", "page": 9, "document_id": "unused-doc", "chunk_id": "unused-chunk"}]
            for source in citations:
                source["excerpt"] = "不應顯示的冗長檔案節錄：" + "逐行表格與無關頁首\n" * 80
        rag_state = {"status": "ready" if mixed or grouped else "skipped"}
        answer = ("<think>合成思考：核對資料。思考中的 [W2] 與 [2] 不是正文引用。</think>" if body.get("think") else "") + \
            ("已結合本地文件 [1] 與網頁 [W1]。" if mixed else "合成回答：網頁資料 [W1]。" if sources and not unavailable else "合成回答：本題未取得網頁資料。") + \
            "\n\n數學：$a+b=2$。\n\n<img src=x onerror=window.fixtureInjection=1>\n\n[危險連結](javascript:window.fixtureInjection=1)\n\n`[W1]`"
        if grouped:
            answer = "合成分組測試：同一文件多段[3][1][2]，同名另一份文件[4]；再次引用第一頁[1]。"
        skill = {"skill_id": "none", "label": "不啟用 Skill", "status": "disabled", "sources": []}
        events = [{"type": "skill_context", **skill},
                  {"type": "rag_context", **rag_state, "chunks": citations}]
        if search:
            events.extend([{"type": "web_status", "status": "searching", "message": "正在搜尋 Google 網頁…"},
                           {"type": "web_status", "status": "fetching", "message": "正在讀取網頁正文…"},
                           {"type": "web_sources", "sources": sources, **web_state}])
        events.extend([{"type": "content", "text": answer},
                       {"type": "done", "skill_id": "none", "skill_context": skill, "citation_map": citations,
                        "web_sources": sources, "web_status": web_state, "rag_status": rag_state}])
        if interrupted:
            events = events[:2] + [{"type": "web_status", "status": "processing", "message": "正在搜尋並讀取網頁資料…"}]
        now = datetime.now(timezone.utc).isoformat()
        # Persist before done so the production UI's immediate history refresh is deterministic.
        if not interrupted:
            MESSAGES.extend([{"content": question, "is_user": True, "timestamp": now},
                             {"content": answer, "is_user": False, "timestamp": now, "citation_map": citations,
                              "skill_id": "none", "skill_context": skill, "web_sources": sources, "web_status": web_state, "rag_status": rag_state}])
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            for event in events:
                self.wfile.write(("data: " + json.dumps(event, ensure_ascii=False) + "\n\n").encode("utf-8"))
                self.wfile.flush()
                time.sleep(0.12)
        except (ConnectionAbortedError, BrokenPipeError):
            pass


if __name__ == "__main__":
    print("Synthetic web search UI fixture: http://127.0.0.1:8012/fixture", flush=True)
    ThreadingHTTPServer(("127.0.0.1", 8012), Handler).serve_forever()
