"""Isolated browser fixture for the production teaching-mode UI.

Run: venv312/Scripts/python.exe tests/teacher_skill_browser_harness.py
Open http://127.0.0.1:8001/fixture. All accounts, classroom excerpts, replies
and history are synthetic; this server never imports main or contacts a model.
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
USER = {"account": "teacher-skill-fixture", "name": "測試學生", "class": "高二9班", "role": "學生"}


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
        path = urlparse(self.path).path
        if path == "/fixture":
            source = (ROOT / "index.html").read_text(encoding="utf-8")
            bootstrap = '<script>localStorage.setItem("sessionId","teacher-skill-fixture");' + \
                        'localStorage.setItem("userInfo",JSON.stringify(' + json.dumps(USER, ensure_ascii=False) + '));</script>'
            raw = source.replace("<head>", "<head>" + bootstrap, 1).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        elif path == "/fixture/requests":
            self.send_json(REQUESTS)
        elif path == "/api/session":
            self.send_json(envelope({"user_info": USER, "current_conversation": {"id": "fixture-conversation", "messages": MESSAGES.copy()}}))
        elif path == "/api/skills":
            self.send_json(envelope({"skills": [
                {"id": "none", "label": "不啟用 Skill", "status": "disabled", "available": True},
                {"id": "chemistry-teacher", "label": "化學老師", "status": "ready", "available": True,
                 "style_verified": True, "source_count": 1, "version": "fixture-v2"}
            ]}))
        elif path == "/api/files":
            self.send_json(envelope({"files": []}))
        elif path == "/api/conversations":
            self.send_json(envelope({"conversations": []}))
        elif path.startswith("/api/"):
            self.send_json(envelope({}))
        elif path in {"/static/tailwindcss.js", "/static/marked.js", "/static/pdf.min.js", "/static/pdf.worker.min.js",
                      "/static/font-awesome.css", "/static/logo.png", "/static/fonts/fontawesome-webfont.woff2"}:
            self.path = self.path.removeprefix("/static")
            super().do_GET()
        else:
            self.send_error(404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        path = urlparse(self.path).path
        if path == "/fixture/reset":
            MESSAGES.clear()
            REQUESTS.clear()
            self.send_json(envelope({}))
            return
        if path != "/api/chat":
            self.send_json(envelope({"conversation_id": "fixture-conversation"}))
            return
        REQUESTS.append(body)
        selected = body.get("skill_id") == "chemistry-teacher"
        pending = "無來源" in body.get("question", "")
        skill = {"skill_id": "chemistry-teacher" if selected else "none",
                 "label": "化學老師" if selected else "不啟用 Skill", "version": "fixture",
                 "status": "no_relevant_sources" if selected and pending else "ready" if selected else "disabled",
                 "style_verified": selected, "sources": [], "teaching_version": "2.0",
                 "method_label": "看決定結構 → 小問自答 → 形成判斷"}
        if selected and not pending:
            skill["sources"] = [{"id": "fixture-source", "recording_id": "fixture-recording",
                                 "title": "測試課堂 <img src=x onerror=window.fixtureInjection=1>",
                                 "date": "2026-10-15", "timestamp_start": 62, "timestamp_end": 82,
                                 "paragraph": 4, "confidence": "verified", "verification_scope": "transcript_text_and_chemistry",
                                 "excerpt": "合成測試資料：先觀察碳碳雙鍵。<script>window.fixtureInjection=1</script>"}]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        answer = "合成測試回答：先觀察結構，再判斷反應類型。" if selected else "合成測試回答：乙烯與溴發生加成反應。"
        events = [{"type": "skill_context", **skill},
                  {"type": "no_relevant_context", "status": "no_relevant_context", "chunks": []},
                  {"type": "content", "text": answer},
                  {"type": "done", "skill_id": skill["skill_id"], "skill_context": skill, "citation_map": []}]
        # Match the real backend: history is committed before the final stream event.
        now = datetime.now(timezone.utc).isoformat()
        MESSAGES.extend([{"content": body["question"], "is_user": True, "timestamp": now},
                         {"content": answer, "is_user": False, "timestamp": now,
                          "skill_id": skill["skill_id"], "skill_context": skill, "citation_map": []}])
        try:
            for event in events:
                self.wfile.write(("data: " + json.dumps(event, ensure_ascii=False) + "\n\n").encode("utf-8"))
                self.wfile.flush()
                time.sleep(0.2)
        except (ConnectionAbortedError, BrokenPipeError):
            return


if __name__ == "__main__":
    print("Synthetic teaching-mode UI fixture: http://127.0.0.1:8001/fixture", flush=True)
    ThreadingHTTPServer(("127.0.0.1", 8001), Handler).serve_forever()
