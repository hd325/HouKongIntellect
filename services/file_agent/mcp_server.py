"""Private stdio MCP server. Only the authenticated backend starts this process.

No HTTP listener, client-selected roots, shell, eval, or provider credentials.
Each process is tied to exactly one server-created account/task manifest.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path


def _resource_limit():
    from services.process_limits import limit_worker
    return limit_worker()


async def serve():
    import mcp.types as types
    from mcp.server.lowlevel import Server
    from mcp.server.stdio import stdio_server
    from .skills import FileSkills, TOOL_DEFINITIONS

    manifest = Path(os.environ["FILE_AGENT_CONTEXT"]).resolve()
    context = json.loads(manifest.read_text(encoding="utf-8"))
    root = manifest.parent
    if root.name != context["task_id"] or root.parent.name != context["user_key"]:
        raise ValueError("Invalid task binding")
    output = Path(context["output_dir"]).resolve()
    paths = {key: Path(path).resolve() for key, path in context["paths"].items()}
    if not output.is_relative_to(root) or any(not path.is_relative_to(root) for path in paths.values()):
        raise ValueError("Invalid task paths")
    skills = FileSkills(paths, output)
    server = Server("houkong-private-file-tools")

    @server.list_tools()
    async def list_tools():
        return [types.Tool(name=tool["function"]["name"],
                           description=tool["function"].get("description", ""),
                           inputSchema=tool["function"]["parameters"])
                for tool in TOOL_DEFINITIONS]

    @server.call_tool()
    async def call_tool(name, arguments):
        try:
            result = skills.execute(name, arguments or {})
            if name == "read_file":
                identifier = str((arguments or {}).get("file_id"))
                ocr = context.get("ocr_text", {}).get(identifier, "")
                if ocr:
                    start = int((arguments or {}).get("start", 0))
                    limit = int((arguments or {}).get("limit", 100))
                    parts = re.split(r"---\s*第(\d+)[页頁]\s*---", ocr)
                    if len(parts) > 1:
                        selected = [(int(parts[index]), parts[index + 1]) for index in range(1, len(parts), 2)
                                    if start < int(parts[index]) <= start + limit]
                        page_text = "\n\n".join(f"第{number}頁：\n{text}" for number, text in selected)
                    else:
                        page_text = ocr if start == 0 else ""
                    if len(page_text) > 24000:
                        return [types.TextContent(type="text", text=json.dumps({"error": "本次OCR文字超出單次讀取限制，請減小 read_file 的limit分頁讀取；不能忽略被截斷資料。"}, ensure_ascii=False))]
                    if page_text:
                        result["uploaded_extracted_text"] = page_text
                        result["uploaded_extracted_text_truncated"] = False
                        result["uploaded_extraction_note"] = "上傳時既有提取/OCR結果；原檔結構以本次讀取為準。"
                missing_pages = [item.get("index", 0) + 1 for item in result.get("items", []) if item.get("needs_ocr") and item.get("type") == "page"]
                image_only = any(item.get("type") == "image" for item in result.get("items", []))
                if image_only:
                    result["capabilities"] = {"image_asset": True, "text_readable": bool(ocr.strip())}
                    if not ocr.strip():
                        result["content_warning"] = "此圖片沒有可讀OCR文字，只能作為插圖使用；不得宣稱已讀取、提取或理解其文字。"
                if missing_pages:
                    extraction = context.get("extraction", {}).get(identifier, {})
                    recognized = {int(parts[index]) for index in range(1, len(parts), 2)} if ocr and len(parts) > 1 else set()
                    if not ocr or extraction.get("truncated") or extraction.get("status") in {"partial", "failed", "empty"} or (recognized and any(page not in recognized for page in missing_pages)):
                        return [types.TextContent(type="text", text=json.dumps({"error": "此PDF部分頁面需要OCR，但原上傳沒有完整識別結果。請用原有上傳入口重新識別後再處理，不能猜測內容。"}, ensure_ascii=False))]
            return [types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]
        except Exception as exc:
            # Skill errors are already designed to be safe to display.
            from .skills import FileSkillError
            error = str(exc) if isinstance(exc, FileSkillError) else "檔案工具處理失敗，檔案可能損壞或格式不受支援。"
            return [types.TextContent(type="text", text=json.dumps({"error": error}, ensure_ascii=False))]

    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    _job = _resource_limit()
    asyncio.run(serve())
