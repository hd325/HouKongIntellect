"""Reuse existing authenticated conversations, file records, preview and downloads."""
from __future__ import annotations

import asyncio
import copy
import json
import re
import uuid
from datetime import datetime
from pathlib import Path

from config.settings import ENABLE_FILE_AGENT
from services.resource_limits import PROCESSING_SLOT
from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from .agent import DeepSeekFileAgent, should_handle
from .runtime import FileAgentError, TaskRuntime

_locks: dict[tuple[str, str], asyncio.Lock] = {}
_active_users: dict[str, int] = {}
_active_tasks = 0


def _event(value):
    return "data: " + json.dumps(value, ensure_ascii=False) + "\n\n"


async def maybe_file_response(host, request, session_id):
    """Return None for normal chat; otherwise run a complete scoped file task."""
    session = await host.data_store.get_session(session_id)
    if not session:
        raise HTTPException(401, "會話無效")
    user_id = session["account"]
    current_id = session.get("current_conversation_id")
    conversation = await host.data_store.get_conversation(current_id) if current_id else None
    history = conversation.get("messages", []) if conversation and conversation.get("user_id") == user_id else []
    if not should_handle(request.question, request.file_references, request.file_ids, history):
        return None
    if not ENABLE_FILE_AGENT:
        raise HTTPException(503, "文件編輯功能尚未開啟，請先安裝選裝依賴並設定 ENABLE_FILE_AGENT=true。")
    if conversation and conversation.get("user_id") != user_id:
        raise HTTPException(403, "無權訪問此對話")
    if not conversation:
        response = await host.new_conversation(host.NewConversationRequest(session_id=session_id), session_id)
        current_id = response["data"]["conversation_id"]
        conversation = await host.data_store.get_conversation(current_id)
    lock_key = (user_id, current_id)
    lock = _locks.setdefault(lock_key, asyncio.Lock())
    if lock.locked():
        raise HTTPException(409, "本對話有文件任務正在執行，請完成後再修改。")
    global _active_tasks
    if _active_tasks >= 1 or PROCESSING_SLOT.locked() or _active_users.get(user_id, 0) >= 1:
        raise HTTPException(429, "文件任務繁忙，請等待現有任務完成。")

    # IDs are preferred; legacy names must resolve uniquely among authorized files.
    records = await host.data_store.get_all_files(user_id)
    by_id = {record["id"]: record for record in records}
    chosen = []
    for file_id in request.file_ids:
        if file_id not in by_id:
            raise HTTPException(403, "引用檔案不存在或沒有使用權限")
        if file_id not in chosen:
            chosen.append(file_id)
    names = [name.strip() for name in request.file_references.splitlines() if name.strip()]
    inherit_previous = bool(re.search(r"上一|剛才|刚才|這份|这份|那份|繼續|继续|再|修改|更改|改成|改為|改为|加上|增加|刪除|删除|替換|替换|字體|字体|顏色|颜色|公式|排版", request.question))
    if not chosen:
        for name in names or host._extract_referenced_filenames(request.question):
            matches = [file_id for file_id, record in by_id.items()
                       if name in {record.get("name"), record.get("original_name"), record.get("original_filename")}]
            if len(matches) != 1:
                raise HTTPException(400, "引用檔案無法唯一識別，請用原有引用入口重新選取。")
            if matches[0] not in chosen:
                chosen.append(matches[0])
    # Follow up the last generated version from this same, verified conversation.
    if not chosen and inherit_previous:
        for message in reversed(history[-12:]):
            if not message.get("is_user") and message.get("artifacts"):
                chosen = [item["id"] for item in message["artifacts"] if item.get("id") in by_id]
                break
    if len(chosen) > 12:
        raise HTTPException(400, "每次最多引用 12 份檔案。")
    # Recheck after all awaited lookups, then reserve without yielding.
    lock = _locks.setdefault(lock_key, asyncio.Lock())
    if lock.locked():
        raise HTTPException(409, "本對話有文件任務正在執行，請完成後再修改。")
    if _active_tasks >= 1 or PROCESSING_SLOT.locked() or _active_users.get(user_id, 0) >= 1:
        raise HTTPException(429, "文件任務繁忙，請等待現有任務完成。")
    await lock.acquire()
    await PROCESSING_SLOT.acquire()
    _active_tasks += 1
    _active_users[user_id] = _active_users.get(user_id, 0) + 1

    async def generate():
        runtime = None
        committed = False
        queue = asyncio.Queue()
        task = None
        artifacts = []
        reply_id = str(uuid.uuid4())
        history_before = copy.deepcopy(history)

        async def authorize():
            current_session = await host.data_store.get_session(session_id)
            current_conv = await host.data_store.get_conversation(current_id)
            if not current_session or current_session.get("account") != user_id:
                raise FileAgentError("登入已失效，文件任務已停止。")
            if not current_conv or current_conv.get("user_id") != user_id:
                raise FileAgentError("對話權限已失效，文件任務已停止。")
            for file_id in chosen:
                record = await host.data_store.find_file_by_id(file_id)
                if not record or not host.check_file_permission(record, user_id):
                    raise FileAgentError("引用檔案權限已變更，文件任務已停止。")

        async def produce():
            try:
                agent = DeepSeekFileAgent(runtime, host.API_CONFIG, authorize)
                async for item in agent.run(request.question, history_before):
                    await queue.put(item)
            except BaseException as exc:
                await queue.put(exc)
            finally:
                await queue.put(None)

        try:
            runtime = TaskRuntime(Path(host.UPLOAD_DIR), user_id, current_id)
            runtime.write_state("running")
            yield _event({"type": "file_status", "task_id": runtime.task_id,
                          "status": "reading", "message": "正在讀取引用資料"})
            await authorize()
            for file_id in chosen:
                record = by_id[file_id]
                source = host.resolve_storage_path(record.get("storage_path"), record)
                if not source or not Path(source).is_file():
                    raise FileAgentError("引用檔案原始文件不存在，請重新上傳後再處理。")
                text = await host.data_store.get_file_content(file_id) or ""
                await asyncio.to_thread(runtime.add_input, record, Path(source), text)
            conversation["messages"].append({"id": str(uuid.uuid4()), "content": request.question,
                "is_user": True, "timestamp": datetime.now().isoformat(),
                "file_ids": chosen, "file_references": request.file_references})
            conversation["updated_at"] = datetime.now().isoformat()
            await host.data_store.set_conversation(current_id, conversation)
            task = asyncio.create_task(produce())
            summary = ""
            output_ids = []
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), 15)
                except TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                if isinstance(item, BaseException):
                    raise item
                if item is None:
                    break
                if item["type"] == "result":
                    summary = item["summary"]
                    output_ids = item["output_ids"]
                else:
                    yield _event({**item, "task_id": runtime.task_id})
            if not runtime.generated:
                raise FileAgentError("本次任務未產生可下載的檔案。")
            await authorize()
            yield _event({"type": "file_status", "task_id": runtime.task_id,
                          "status": "validating", "message": "正在檢查並保存完整文件"})
            # Keep final leaves; internal edit iterations do not become user downloads.
            final = {key: runtime.generated[key] for key in output_ids}
            records_to_save = []
            for file_id, value in final.items():
                path = runtime.paths[file_id]
                validation = await runtime.call("validate_file", {"file_id": file_id})
                parent_id = value.get("parent_id")
                while parent_id in runtime.generated:
                    parent_id = runtime.generated[parent_id].get("parent_id")
                parent = by_id.get(parent_id, {})
                root_id = parent.get("root_id") or parent_id or file_id
                owner_versions = [int(record.get("version", 1)) for record in records
                                  if record.get("owner") == user_id and (record.get("root_id") or record["id"]) == root_id]
                version = max(owner_versions, default=int(parent.get("version", 0))) + 1 if parent_id else 1
                name = value.get("filename") or path.name
                if Path(name).name != name or "\\" in name:
                    raise FileAgentError("輸出檔案名稱不合法。")
                try:
                    text_result = await runtime.call("read_file", {"file_id": file_id, "limit": 5})
                except FileAgentError:
                    text_result = {"content": "文件已通過結構檢查。文字預覽無法完整載入，請下載原檔查看。", "truncated": True}
                # Searchable preview text; the original binary is always complete.
                extracted = json.dumps(text_result, ensure_ascii=False)
                now = datetime.now().isoformat()
                record = {"id": file_id, "name": name, "original_name": name,
                    "original_filename": name, "size": path.stat().st_size,
                    "uploaded_at": now, "owner": user_id, "audience": "personal",
                    "review_status": "published", "storage_path": path.relative_to(Path(host.UPLOAD_DIR).resolve()).as_posix(),
                    "file_task_id": runtime.task_id, "conversation_id": current_id,
                    "parent_id": parent_id, "root_id": root_id, "version": version,
                    "subject": "other", "material_type": "other", "study_period": "",
                    "notes": "AI 文件任務成果", "extraction_status": "partial" if text_result.get("has_more") or text_result.get("truncated") else "success",
                    "is_truncated": bool(text_result.get("has_more") or text_result.get("truncated")),
                    "page_count": validation.get("page_count", 0)}
                records_to_save.append((record, extracted))
                artifacts.append({"id": file_id, "name": name, "filename": name,
                    "size": record["size"], "file_size": record["size"], "has_original": True,
                    "version": version, "parent_id": parent_id,
                    "description": "已檢查並保存；可預覽及下載完整文件。"})
            await authorize()
            # All output metadata and the assistant message become durable together.
            # Use server-derived output names, not unverified model download claims.
            result_text = "已完成並檢查以下文件：" + "、".join(item["name"] for item in artifacts) + "。"
            message = {"id": reply_id, "content": result_text, "is_user": False,
                       "timestamp": datetime.now().isoformat(), "artifacts": artifacts,
                       "file_task_id": runtime.task_id, "file_status": "complete", "referenced_files": chosen}
            await host.data_store.commit_file_task(current_id, user_id, records_to_save, message)
            committed = True
            try:
                runtime.write_state("complete", output_ids=[item["id"] for item in artifacts])
            except OSError:
                pass  # The atomic file/message commit above is the durable authority.
            yield _event({"type": "artifacts", "task_id": runtime.task_id, "artifacts": artifacts})
            yield _event({"type": "content", "text": result_text})
            yield _event({"type": "file_status", "task_id": runtime.task_id, "status": "complete", "message": "文件已完成"})
            yield _event({"type": "done", "artifacts": artifacts, "file_task_id": runtime.task_id,
                          "file_status": "complete", "citation_map": [], "references": []})
        except asyncio.CancelledError:
            if runtime and not committed:
                runtime.write_state("cancelled")
            raise
        except Exception as exc:
            detail = str(exc) if isinstance(exc, FileAgentError) else (
                "文件任務執行逾時，請拆分要求再試。" if isinstance(exc, TimeoutError)
                else "文件任務失敗，未保存不完整成果。請檢查原始文件或重試。")
            if runtime and not committed:
                runtime.write_state("failed", error=detail)
            if not committed:
                current = await host.data_store.get_conversation(current_id)
                if current and current.get("user_id") == user_id:
                    current["messages"].append({"id": reply_id, "content": detail, "is_user": False,
                        "timestamp": datetime.now().isoformat(), "file_status": "error",
                        "file_task_id": runtime.task_id if runtime else None})
                    await host.data_store.set_conversation(current_id, current)
            yield _event({"type": "file_status", "status": "error", "message": detail})
            yield _event({"type": "error", "detail": detail})
        finally:
            if task and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            if runtime:
                try:
                    runtime.cleanup_temporary([item["id"] for item in artifacts] if committed else [])
                except OSError:
                    pass
            global _active_tasks
            _active_tasks -= 1
            PROCESSING_SLOT.release()
            _active_users[user_id] -= 1
            if not _active_users[user_id]:
                _active_users.pop(user_id, None)
            lock.release()
            if _locks.get(lock_key) is lock and not lock.locked():
                _locks.pop(lock_key, None)

    return StreamingResponse(generate(), media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
