"""Run parsers out of the API process, with one bounded job and forced cleanup."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import signal
from pathlib import Path

from config.settings import DATA_DIR, PARSER_TIMEOUT, PROJECT_ROOT
from services.resource_limits import MAX_FILE_BYTES, PROCESSING_SLOT, ParserLimitError


async def stop_worker(process):
    if process.returncode is not None:
        return
    if os.name == "nt":
        executable = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "taskkill.exe"
        killer = await asyncio.create_subprocess_exec(str(executable), "/PID", str(process.pid), "/T", "/F",
                     stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await killer.wait()
    else:
        try: os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError: pass
    if process.returncode is None:
        try: process.kill()
        except ProcessLookupError: pass
    await process.wait()


async def parse_upload_isolated(file=None, *, progress_callback=None, retry=True, office_source=None, office_output=None):
    # Fail promptly instead of retaining queued 50 MB bodies on a 2 GB host.
    if PROCESSING_SLOT.locked():
        from fastapi import HTTPException
        raise HTTPException(429, "文件處理繁忙，請在目前任務完成後重試。")
    await PROCESSING_SLOT.acquire()
    process = None
    try:
        root = Path(DATA_DIR) / ".parser_tasks"
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="parse-", dir=root) as folder:
            task = Path(folder)
            raw_path = task / "input.bin"
            size = 0
            if office_source is None:
                file.file.seek(0)
                with raw_path.open("wb") as out:
                    while chunk := await file.read(1024 * 1024):
                        size += len(chunk)
                        if size > MAX_FILE_BYTES:
                            raise ParserLimitError("檔案超過 50 MB。")
                        out.write(chunk)
                file.file.seek(0)
            manifest = task / "context.json"
            context = ({"operation": "office_preview", "input": str(office_source), "output": str(office_output)}
                       if office_source is not None else
                       {"input": str(raw_path), "filename": file.filename, "retry": retry})
            manifest.write_text(json.dumps(context), encoding="utf-8")
            env = os.environ.copy()
            env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8", TEMP=str(task), TMP=str(task))
            env.update(OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1", MKL_NUM_THREADS="1")
            with (task / "worker.log").open("wb") as errors:
                process = await asyncio.create_subprocess_exec(
                    sys.executable, "-X", "utf8", "-m", "services.parser_worker", str(manifest),
                    cwd=PROJECT_ROOT, env=env, stdout=asyncio.subprocess.PIPE,
                    stderr=errors, limit=16 * 1024 * 1024,
                    **({"start_new_session": True} if os.name != "nt" else {}),
                )
                result = None
                try:
                    async with asyncio.timeout(PARSER_TIMEOUT):
                        async for line in process.stdout:
                            event = json.loads(line)
                            if "progress" in event and progress_callback:
                                progress_callback(event["progress"])
                            if "limit_error" in event:
                                raise ParserLimitError(event["limit_error"])
                            if "parser_error" in event:
                                raise ParserLimitError(event["parser_error"])
                            if "result" in event:
                                result = event["result"]
                        if await process.wait() != 0 or result is None:
                            raise ParserLimitError("文件解析程序未能完成，請縮小文件後重試。")
                except TimeoutError as error:
                    raise ParserLimitError("文件解析超過 120 秒，已停止處理。") from error
                finally:
                    if process.returncode is None:
                        await stop_worker(process)
                return result
    finally:
        # Also runs on client disconnect/cancellation, after the child is reaped.
        if process is not None and process.returncode is None:
            await stop_worker(process)
        PROCESSING_SLOT.release()


async def office_preview_isolated(source: Path, directory: Path) -> Path:
    result = await parse_upload_isolated(office_source=source, office_output=directory)
    path = Path(result["path"]).resolve()
    if not path.is_relative_to(directory.resolve()) or not path.is_file():
        raise ParserLimitError("文件預覽結果不合法。")
    return path
