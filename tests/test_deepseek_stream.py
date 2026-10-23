"""Offline checks for Flash request options, identity and streamed responses."""
from __future__ import annotations

import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx

import tests  # Establish synthetic-only storage before application imports.
import main
from tests.account_fixtures import fixture_accounts
from config.deepseek import DEEPSEEK_MODEL, DEEPSEEK_MODEL_VERSION, DEEPSEEK_MODEL_VERIFIED_ON


def content_line(text: str, ending: str = "\n\n") -> bytes:
    data = {"choices": [{"delta": {"content": text}}]}
    return ("data: " + json.dumps(data, ensure_ascii=False) + ending).encode("utf-8")


class ByteResponse:
    def __init__(self, chunks, failure=None):
        self.chunks = chunks
        self.failure = failure

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def raise_for_status(self):
        pass

    async def aiter_bytes(self):
        for chunk in self.chunks:
            yield chunk
        if self.failure:
            raise self.failure


class ByteClient:
    def __init__(self, response, requests):
        self.response = response
        self.requests = requests

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def stream(self, *args, **kwargs):
        self.requests.append(kwargs["json"])
        return self.response


class DeepseekStreamTests(unittest.IsolatedAsyncioTestCase):
    async def collect(self, responses, think=False):
        self.requests = []
        clients = [ByteClient(response, self.requests) for response in responses]
        with (
            patch("main.httpx.AsyncClient", side_effect=clients) as factory,
            patch.dict(main.API_CONFIG, {"DEEPSEEK_API_KEY": "offline-test-key"}),
            patch.object(main.logger, "info"),
            patch("main.asyncio.sleep", new_callable=AsyncMock) as sleep,
        ):
            output = [event async for event in main.stream_deepseek("test prompt", "test question", think=think)]
        events = [json.loads(event.removeprefix("data: ").strip()) for event in output]
        return events, factory.call_count, sleep.await_count

    async def test_regular_chat_explicitly_disables_provider_default_thinking(self):
        await self.collect([ByteResponse([content_line("答案")])])
        request = self.requests[0]
        self.assertEqual(request["model"], "deepseek-flash")
        self.assertEqual(request["thinking"], {"type": "disabled"})
        self.assertEqual(request["temperature"], .1)
        self.assertNotIn("reasoning_effort", request)

    async def test_thinking_uses_native_parameters_and_preserves_original_question(self):
        await self.collect([ByteResponse([content_line("答案")])], think=True)
        request = self.requests[0]
        self.assertEqual(request["model"], "deepseek-flash")
        self.assertEqual(request["thinking"], {"type": "enabled"})
        self.assertEqual(request["reasoning_effort"], "high")
        self.assertNotIn("temperature", request)
        self.assertEqual(request["messages"][1], {"role": "user", "content": "test question"})

    async def test_native_reasoning_is_separated_from_final_answer_for_existing_renderer(self):
        def line(delta):
            return ("data: " + json.dumps({"choices": [{"delta": delta}]}, ensure_ascii=False) + "\n\n").encode()
        wire = (line({"reasoning_content": "思考片段🧪"})
                + line({"reasoning_content": "完成"}) + line({"content": "最終答案"}))
        events, _, _ = await self.collect([ByteResponse([wire[i:i + 1] for i in range(len(wire))])], think=True)
        self.assertEqual("".join(event["text"] for event in events),
                         "<think>\n思考片段🧪完成\n</think>\n\n最終答案")
        events, _, _ = await self.collect([ByteResponse([wire])], think=False)
        self.assertEqual("".join(event["text"] for event in events), "最終答案")

    async def test_upstream_model_metadata_is_reported_once_without_claiming_revision(self):
        data = {"model": "deepseek-flash", "choices": [{"delta": {"content": "答案"}}]}
        wire = ("data: " + json.dumps(data) + "\n\n").encode()
        events, _, _ = await self.collect([ByteResponse([wire, wire])])
        metadata = [event for event in events if event["type"] == "model_info"]
        self.assertEqual(metadata, [{"type": "model_info", "requested_model": DEEPSEEK_MODEL,
                                    "returned_model": "deepseek-flash", "documented_version": DEEPSEEK_MODEL_VERSION,
                                    "verified_on": DEEPSEEK_MODEL_VERIFIED_ON}])

    async def test_reasoning_only_eof_closes_display_block_and_reports_missing_answer(self):
        wire = b'data: {"choices":[{"delta":{"reasoning_content":"partial"}}]}\n\n'
        events, calls, waits = await self.collect([ByteResponse([wire])], think=True)
        text = "".join(event.get("text", "") for event in events)
        self.assertEqual(text.count("<think>"), 1)
        self.assertEqual(text.count("</think>"), 1)
        self.assertEqual(events[-1]["type"], "error")
        self.assertEqual((calls, waits), (1, 0))

    async def test_timeout_after_visible_reasoning_closes_block_without_duplicate_retry(self):
        wire = b'data: {"choices":[{"delta":{"reasoning_content":"partial"}}]}\n\n'
        failed = ByteResponse([wire], httpx.ReadTimeout("offline timeout"))
        events, calls, waits = await self.collect([failed], think=True)
        text = "".join(event.get("text", "") for event in events)
        self.assertEqual(text, "<think>\npartial\n</think>\n\n")
        self.assertEqual(events[-1]["type"], "error")
        self.assertEqual((calls, waits), (1, 0))

    async def test_chinese_and_four_byte_characters_survive_single_byte_chunks(self):
        text = "判斷依據：先加稀硝酸，再檢驗溴離子。🧪"
        wire = content_line(text) + b"data: [DONE]\n\n"
        events, calls, waits = await self.collect([ByteResponse([wire[i:i + 1] for i in range(len(wire))])])
        self.assertEqual(events, [{"type": "content", "text": text, "done": False}])
        self.assertEqual((calls, waits), (1, 0))

    async def test_fragmented_sse_and_json_preserve_content_event_order(self):
        wire = (b": heartbeat\r\n\r\ndata: {invalid}\r\n\r\n"
                + content_line("先看C—Br。", "\r\n\r\n")
                + b'data: {"choices": [{"delta": {}}]}\r\n\r\n'
                + content_line("換成OH，碳骨架照寫。", "\r\n\r\n")
                + b"data: [DONE]\r\n\r\n")
        chunks = [wire[i:i + 7] for i in range(0, len(wire), 7)]
        events, calls, _ = await self.collect([ByteResponse(chunks)])
        self.assertEqual([event["text"] for event in events], ["先看C—Br。", "換成OH，碳骨架照寫。"])
        self.assertTrue(all(event["type"] == "content" and event["done"] is False for event in events))
        self.assertEqual(calls, 1)

    async def test_final_data_line_without_newline_is_emitted_once(self):
        text = "水解後冷卻，再用稀硝酸酸化。"
        wire = content_line(text, "")
        marker = wire.index("硝".encode("utf-8"))
        chunks = [wire[:marker + 1], b"", wire[marker + 1:marker + 2], wire[marker + 2:]]
        events, calls, _ = await self.collect([ByteResponse(chunks)])
        self.assertEqual(events, [{"type": "content", "text": text, "done": False}])
        self.assertEqual(calls, 1)

    async def test_retry_resets_decoder_after_incomplete_character(self):
        wire = content_line("加成反應")
        marker = wire.index("加".encode("utf-8"))
        failed = ByteResponse([wire[:marker + 1]], httpx.ReadTimeout("offline timeout"))
        success = ByteResponse([wire[:marker + 2], wire[marker + 2:]])
        events, calls, waits = await self.collect([failed, success])
        self.assertEqual(events, [{"type": "content", "text": "加成反應", "done": False}])
        self.assertEqual((calls, waits), (2, 1))


class PromptModelTests(unittest.TestCase):
    def test_model_identity_matches_config_for_both_direct_and_generated_answers(self):
        for question in ("你是誰？", "你是什麼模型？", "你用的是什么模型？"):
            answer = main.get_direct_system_response(question)
            self.assertIn(DEEPSEEK_MODEL, answer)
            self.assertIn(DEEPSEEK_MODEL_VERSION, answer)
            self.assertIn(DEEPSEEK_MODEL_VERIFIED_ON, answer)
        self.assertIn(DEEPSEEK_MODEL_VERSION, main.SYSTEM_PROMPT)
        self.assertNotIn("__MODEL_", main.SYSTEM_PROMPT)

    def test_source_rules_allow_attribution_and_do_not_claim_full_file_access(self):
        prompt = main.build_rag_prompt("問題", "", "[1] 參考資料")
        self.assertIn("不假設已讀取整份檔案", prompt)
        self.assertIn("凡依據參考資料寫出的事實", prompt)
        self.assertNotIn("像你本來就知道", prompt)
        self.assertNotIn("每一個包含事實、數據或結論", prompt)

    def test_ocr_failure_does_not_claim_file_is_empty(self):
        prompt = main.build_prompt_with_context("圖片內容", "", [
            {"name": "image.png", "filename_match": True, "empty_content": True}
        ], "", "file")
        self.assertIn("未能從該檔案擷取可讀文字", prompt)
        self.assertIn("不要斷言檔案沒有文字", prompt)
        self.assertNotIn("確實沒有可識別的文字", prompt)


class HealthModelTests(unittest.IsolatedAsyncioTestCase):
    async def test_health_reports_documented_mapping_without_upstream_call(self):
        with patch.object(main.data_store, "get_all_sessions", new_callable=AsyncMock, return_value={}):
            health = await main.health_check()
        self.assertEqual(health["model"]["api_name"], DEEPSEEK_MODEL)
        self.assertEqual(health["model"]["documented_version"], DEEPSEEK_MODEL_VERSION)
        self.assertEqual(health["model"]["verified_on"], DEEPSEEK_MODEL_VERIFIED_ON)


if __name__ == "__main__":
    unittest.main()
