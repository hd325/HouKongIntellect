"""Offline teaching-mode contracts; no recorder, real model, or corpus access."""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

import tests  # Establish synthetic-only storage before application imports.
import main
from tests.account_fixtures import fixture_accounts
from services import teacher_skill


class TeacherSkillTests(unittest.TestCase):
    def setUp(self):
        _accounts = patch.dict(main.VALID_ACCOUNTS, fixture_accounts())
        _accounts.start()
        self.addCleanup(_accounts.stop)
        self.temp = tempfile.TemporaryDirectory(prefix="hk_teacher_skill_")
        self.root = Path(self.temp.name)
        self.skill = self.root / "skill"
        (self.skill / "references").mkdir(parents=True)
        (self.skill / "SKILL.md").write_text("---\nname: chemistry-teacher\n---\nTEST_CHEMISTRY_RULE: 先看結構再判斷。", encoding="utf-8")
        (self.skill / "references" / "teacher-style.md").write_text("VERIFIED_PERSONAL_STYLE", encoding="utf-8")
        self.corpus_path = self.root / "corpus.json"
        self.corpus = {"schema_version": 1, "version": "fixture-v1", "owner_account": "student-a",
                       "status": "ready", "style_verified": False, "coverage": {}, "chunks": [
                           {"id": "chem-1", "recording_id": "fixture-recording", "title": "乙烯課堂",
                            "text": "乙烯有碳碳雙鍵，與溴發生加成反應。", "keywords": ["乙烯", "溴水", "加成反應"],
                            "speaker": "teacher", "confidence": "verified", "timestamp_start": "00:02:10", "paragraph": 7},
                           {"id": "unrelated", "recording_id": "fixture-other", "title": "電化學",
                            "text": "原電池把化學能轉化為電能。", "keywords": ["原電池"],
                            "speaker": "teacher", "confidence": "verified"},
                       ]}
        self.write_corpus()
        self.prompts = []
        self.patches = [
            patch.object(teacher_skill, "SKILL_DIR", self.skill),
            patch.object(teacher_skill, "CORPUS_PATH", self.corpus_path),
            patch.object(main, "CONVERSATIONS_FILE", str(self.root / "conversations.json")),
            patch.object(main, "SESSIONS_FILE", str(self.root / "sessions.json")),
            patch.object(main, "stream_deepseek", self.model_stream),
            patch.object(main, "retrieval_permission_filter", AsyncMock(return_value={})),
            patch.object(main, "_append_rag_metrics", Mock()),
            patch("main.logging.info"), patch("main.logging.warning"), patch("main.logging.error"),
            patch.dict("sys.modules", {"services.retrieval.retriever": SimpleNamespace(
                retrieve=Mock(return_value=[]), format_reference_block=Mock(return_value=""),
                build_citation_payload=Mock(return_value=[]))}),
        ]
        for item in self.patches:
            item.start()
        self.store = main.DataStore()
        now = datetime.now().isoformat()
        self.store._sessions = {"owner-session": {"account": "student-a", "created_at": now},
                                "other-session": {"account": "student-b", "created_at": now}}
        self.store_patch = patch.object(main, "data_store", self.store)
        self.store_patch.start()
        # No lifespan: it initializes unrelated real backend services.
        self.client = TestClient(main.app)
        self.headers = {"X-Session-ID": "owner-session"}

    def tearDown(self):
        self.client.close()
        self.store_patch.stop()
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def write_corpus(self):
        self.corpus_path.write_text(json.dumps(self.corpus, ensure_ascii=False), encoding="utf-8")

    def install_verified_profile(self, examples=False):
        self.corpus["style_verified"] = True
        self.corpus["chunks"][0].update(teacher_id="t1", teacher_identity_verified=True)
        self.corpus["chunks"].append(dict(self.corpus["chunks"][0], id="chem-2", recording_id="fixture-second-recording"))
        profile = {"schema_version": 1, "version": self.corpus["version"],
                   "teachers": [{"id": "t1", "identity_verified": True}], "verified_rules": [
                       {"id": "micro-question-then-answer", "teacher_id": "t1", "reviewed": True,
                        "rule": "先看具体结构，小问立即自答。", "use_when": "概念或追问",
                        "routes": ["concept", "followup"], "source_ids": ["chem-1", "chem-2"]}],
                   "verified_examples": []}
        if examples:
            profile["verified_examples"] = [{"id": "worked-example", "teacher_id": "t1", "reviewed": True,
                "route": "concept", "question": "如何判断乙炔原子共线？", "response": "先看三键附近的直线结构。",
                "source_ids": ["chem-1", "chem-2"]}]
        self.corpus_path.with_name("teacher-profile.json").write_text(json.dumps(profile), encoding="utf-8")
        self.write_corpus()
        return profile

    async def model_stream(self, prompt, question, think=False):
        self.prompts.append(prompt)
        yield 'data: {"type":"content","text":"測試模型答案"}\n\n'

    def ask(self, route="/api/ask", skill_id="chemistry-teacher", **changes):
        response = self.client.post(route, headers=self.headers, json={
            "session_id": "owner-session", "question": "乙烯使溴水褪色是加成反應嗎？", "skill_id": skill_id, **changes})
        self.assertEqual(response.status_code, 200, response.text)
        return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]

    def test_rules_and_relevant_evidence_applied_to_both_routes_and_survive_reload(self):
        for route in ("/api/ask", "/api/chat"):
            with self.subTest(route=route):
                events = self.ask(route)
                context = next(item for item in events if item["type"] == "skill_context")
                done = next(item for item in events if item["type"] == "done")
                self.assertEqual(context["skill_id"], "chemistry-teacher")
                self.assertEqual(context["status"], "ready")
                self.assertEqual([item["id"] for item in context["sources"]], ["chem-1"])
                self.assertIn("TEST_CHEMISTRY_RULE", self.prompts[-1])
                self.assertIn("乙烯有碳碳雙鍵", self.prompts[-1])
                self.assertNotIn("原電池把化學能", self.prompts[-1])
                self.assertNotIn("VERIFIED_PERSONAL_STYLE", self.prompts[-1])
                self.assertEqual(done["skill_context"]["sources"], context["sources"])
        reloaded = main.DataStore()
        self.assertTrue(reloaded._conversations)
        conversation_id, conversation = next(iter(reloaded._conversations.items()))
        assistant = [message for message in conversation["messages"] if not message["is_user"]][-1]
        self.assertEqual(assistant["skill_id"], "chemistry-teacher")
        self.assertEqual(assistant["skill_context"]["sources"][0]["paragraph"], 7)
        response = self.client.get(f"/api/conversation/{conversation_id}", headers=self.headers)
        self.assertEqual(response.json()["data"]["messages"][-1]["skill_context"], assistant["skill_context"])

    def test_default_mode_does_not_load_rules_sources_or_mutate_corpus(self):
        original = self.corpus_path.read_bytes()
        for mode in (None, "none", "default"):
            for route in ("/api/ask", "/api/chat"):
                events = self.ask(route, mode)
                metadata = next(item for item in events if item["type"] == "skill_context")
                self.assertEqual(metadata["skill_id"], "none")
                self.assertEqual(metadata["sources"], [])
                self.assertNotIn("TEST_CHEMISTRY_RULE", self.prompts[-1])
                self.assertNotIn("課堂原文證據", self.prompts[-1])
        self.assertEqual(original, self.corpus_path.read_bytes())

    def test_invalid_skill_ids_are_422_before_model_or_conversation_creation(self):
        for route in ("/api/ask", "/api/chat"):
            for mode in ("../../private", "physics-teacher", {"rules": "client instruction"}):
                response = self.client.post(route, headers=self.headers,
                                            json={"session_id": "owner-session", "question": "乙烯", "skill_id": mode})
                self.assertEqual(response.status_code, 422)
        self.assertEqual(self.prompts, [])
        self.assertEqual(self.store._conversations, {})

    def test_no_recordings_and_unverified_speakers_never_claim_teacher_style(self):
        self.corpus["chunks"] = [dict(self.corpus["chunks"][0], confidence="unverified"),
                                  dict(self.corpus["chunks"][1], speaker="student")]
        self.corpus["style_verified"] = True
        self.write_corpus()
        events = self.ask()
        metadata = next(item for item in events if item["type"] == "skill_context")
        self.assertEqual(metadata["status"], "no_sources")
        self.assertFalse(metadata["style_verified"])
        self.assertEqual(metadata["sources"], [])
        self.assertNotIn("VERIFIED_PERSONAL_STYLE", self.prompts[-1])
        self.assertIn("不得模仿", self.prompts[-1])
        self.corpus_path.unlink()
        self.assertEqual(teacher_skill.prepare_skill("chemistry-teacher", "乙烯", "student-a")[1]["status"], "awaiting_authorization")

    def test_unknown_speaker_and_uncertain_transcript_are_labeled_without_style_claims(self):
        self.corpus["style_verified"] = True
        self.corpus["chunks"] = [dict(self.corpus["chunks"][0], confidence="transcript_uncertain", speaker="unknown")]
        self.write_corpus()
        rules, context = teacher_skill.prepare_skill("chemistry-teacher", "乙烯", "student-a")
        self.assertEqual(context["sources"][0]["confidence"], "transcript_uncertain")
        self.assertEqual(context["sources"][0]["speaker"], "unknown")
        self.assertFalse(context["style_verified"])
        self.assertNotIn("VERIFIED_PERSONAL_STYLE", rules)
        self.assertIn("不得用存疑片段證明化學結論", rules)

    def test_personal_classroom_sources_are_only_visible_to_explicit_owner(self):
        metadata = self.client.get("/api/skills", headers={"X-Session-ID": "other-session"}).json()["data"]["skills"][1]
        self.assertEqual(metadata["status"], "private_sources")
        self.assertEqual(metadata["source_count"], 0)
        self.assertNotIn("student-a", json.dumps(metadata))
        rules, context = teacher_skill.prepare_skill("chemistry-teacher", "乙烯", "student-b")
        self.assertEqual(context["sources"], [])
        self.assertNotIn("乙烯有碳碳雙鍵", rules)
        for owner in (None, ""):
            self.corpus["owner_account"] = owner
            self.write_corpus()
            self.assertEqual(teacher_skill.prepare_skill("chemistry-teacher", "乙烯", "student-a")[1]["status"], "private_sources")

    def test_skill_catalog_requires_auth_and_raw_files_are_not_public_static_assets(self):
        self.assertEqual(self.client.get("/api/skills").status_code, 401)
        self.assertEqual(self.client.get("/api/skills", headers={"X-Session-ID": "invalid"}).status_code, 401)
        response = self.client.get("/api/skills", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["id"] for item in response.json()["data"]["skills"]], ["none", "chemistry-teacher"])
        for path in ("work/chemistry-classroom/corpus.json", "skills/chemistry-teacher/SKILL.md"):
            self.assertEqual(self.client.get("/static/" + path).status_code, 404)

    def test_no_matching_source_and_invalid_corpus_fail_honestly(self):
        _, metadata = teacher_skill.prepare_skill("chemistry-teacher", "如何判斷宇宙紅移？", "student-a")
        self.assertEqual(metadata["status"], "no_relevant_sources")
        self.assertEqual(metadata["sources"], [])
        self.corpus_path.write_text("{broken", encoding="utf-8")
        self.assertEqual(teacher_skill.prepare_skill("chemistry-teacher", "乙烯", "student-a")[1]["status"], "source_unavailable")

    def test_reviewed_knowledge_notes_are_injected_separately_from_raw_classroom_quote(self):
        self.corpus["chunks"][0]["knowledge_notes"] = [{
            "name": "加成反應判斷", "explanation": "先看不飽和鍵，再看結構改變。",
            "conditions": ["結合具體試劑和條件"], "pitfalls": ["褪色不能單獨證明加成"],
            "derivation": "system_inference_from_classroom"}]
        self.corpus["chunks"][1]["knowledge_notes"] = [{"name": "無關補註", "explanation": "不應載入。"}]
        self.write_corpus()
        rules, metadata = teacher_skill.prepare_skill("chemistry-teacher", "乙烯", "student-a")
        self.assertIn("小智依課堂整理並核對的知識補註", rules)
        self.assertIn("褪色不能單獨證明加成", rules)
        self.assertNotIn("無關補註", rules)
        self.assertNotIn("knowledge_notes", metadata["sources"][0])
        self.assertNotIn("褪色不能單獨證明加成", metadata["sources"][0]["excerpt"])

    def test_verified_style_requires_verified_relevant_source_and_manifest_flag(self):
        self.corpus["style_verified"] = True
        self.corpus["chunks"][0].update(teacher_id="t1", teacher_identity_verified=True)
        self.corpus["chunks"].append(dict(self.corpus["chunks"][0], id="chem-2", recording_id="fixture-second-recording"))
        profile = {"schema_version": 1, "version": self.corpus["version"],
                   "teachers": [{"id": "t1", "identity_verified": True}], "verified_rules": [
                       {"id": "rule1", "teacher_id": "t1", "reviewed": True,
                        "rule": "先檢查結構再寫條件。", "source_ids": ["chem-1", "chem-2"]}]}
        self.corpus_path.with_name("teacher-profile.json").write_text(json.dumps(profile), encoding="utf-8")
        self.write_corpus()
        rules, metadata = teacher_skill.prepare_skill("chemistry-teacher", "乙烯", "student-a")
        self.assertTrue(metadata["style_verified"])
        self.assertIn("VERIFIED_PERSONAL_STYLE", rules)
        self.assertIn("先檢查結構再寫條件。", rules)
        self.assertEqual(metadata["style_rule_ids"], ["rule1"])
        rules, metadata = teacher_skill.prepare_skill("chemistry-teacher", "宇宙紅移", "student-a")
        self.assertFalse(metadata["style_verified"])
        self.assertNotIn("VERIFIED_PERSONAL_STYLE", rules)
        profile["version"] = "stale-profile"
        self.corpus_path.with_name("teacher-profile.json").write_text(json.dumps(profile), encoding="utf-8")
        self.assertFalse(teacher_skill.prepare_skill("chemistry-teacher", "乙烯", "student-a")[1]["style_verified"])


    def test_generic_chemistry_question_does_not_cite_unrelated_classroom(self):
        self.corpus["chunks"][0].update(title="有機化學課程總結", text="第一個烯烴是分子裡含有甚麼鍵？碳碳雙鍵。")
        self.write_corpus()
        rules, context = teacher_skill.prepare_skill("chemistry-teacher", "化學平衡移動有甚麼原則？", "student-a")
        self.assertEqual(context["sources"], [])
        self.assertEqual(context["status"], "no_relevant_sources")

    def test_substance_match_outranks_shared_reagent(self):
        self.corpus["chunks"].append({"id": "benzene", "recording_id": "benzene-class", "title": "苯與溴水",
            "text": "苯可以將溴從水中萃取出來。", "keywords": ["溴水", "褪色", "萃取"],
            "speaker": "teacher", "confidence": "verified"})
        self.write_corpus()
        _, context = teacher_skill.prepare_skill("chemistry-teacher", "乙烯使溴水褪色是甚麼反應？", "student-a")
        self.assertEqual(context["sources"][0]["id"], "chem-1")

    def test_compound_question_retrieves_both_steps_instead_of_duplicates(self):
        self.corpus["chunks"] = [{"id": f"hydrolysis-{i}", "recording_id": f"recording-{i}",
            "title": "溴乙烷水解", "text": "溴乙烷水解生成乙醇。", "keywords": ["溴乙烷", "水解"],
            "speaker": "teacher", "confidence": "verified"} for i in range(5)]
        self.corpus["chunks"].append({"id": "acid", "recording_id": "acid-class", "title": "酸化的作用",
            "text": "稀硝酸中和残余的氢氧化钠。", "keywords": ["稀硝酸"], "speaker": "teacher", "confidence": "verified"})
        self.write_corpus()
        _, context = teacher_skill.prepare_skill("chemistry-teacher", "溴乙烷为什么先水解再加稀硝酸？", "student-a")
        self.assertIn("acid", [source["id"] for source in context["sources"]])

    def test_confirmed_method_survives_missing_chapter_without_fabricated_sources(self):
        self.install_verified_profile()
        rules, context = teacher_skill.prepare_skill("chemistry-teacher", "化學平衡移動有甚麼原則？", "student-a")
        self.assertTrue(context["style_verified"])
        self.assertEqual(context["status"], "no_relevant_sources")
        self.assertEqual(context["sources"], [])
        self.assertIn("先看具体结构", rules)
        self.assertNotIn("乙烯有碳碳雙鍵", rules)

    def test_adapted_worked_example_is_selected_by_task_and_never_public_evidence(self):
        self.install_verified_profile(examples=True)
        rules, context = teacher_skill.prepare_skill("chemistry-teacher", "乙烯为什么反应？", "student-a")
        self.assertEqual(context["teaching_example_ids"], ["worked-example"])
        self.assertIn("先看三键附近的直线结构", rules)
        self.assertNotIn("先看三键附近的直线结构", json.dumps(context["sources"], ensure_ascii=False))
        _, context = teacher_skill.prepare_skill("chemistry-teacher", "乙烯实验怎么检验？", "student-a")
        self.assertEqual(context["teaching_example_ids"], [])

    def test_only_answers_skip_personal_rules_and_worked_examples(self):
        self.install_verified_profile(examples=True)
        _, context = teacher_skill.prepare_skill("chemistry-teacher", "只要最终答案，不用解释：乙烯是什么烃？", "student-a")
        self.assertTrue(context["style_verified"])
        self.assertEqual(context["teaching_route"], "answer_only")
        self.assertEqual(context["style_rule_ids"], [])
        self.assertEqual(context["teaching_example_ids"], [])

    def test_followup_keeps_verified_method_only_with_scoped_chemistry_context(self):
        self.install_verified_profile()
        prior = [{"is_user": True, "content": "乙烯为什么反应？"}]
        _, context = teacher_skill.prepare_skill("chemistry-teacher", "为什么？", "student-a", recent_messages=prior)
        self.assertEqual(context["teaching_route"], "followup")
        self.assertTrue(context["style_verified"])
        self.assertEqual(context["sources"], [])
        self.assertFalse(teacher_skill.prepare_skill("chemistry-teacher", "为什么？", "student-a")[1]["style_verified"])
        self.assertFalse(teacher_skill.prepare_skill("chemistry-teacher", "为什么？", "student-b", recent_messages=prior)[1]["style_verified"])

    def test_stale_or_cross_teacher_example_is_never_loaded(self):
        profile = self.install_verified_profile(examples=True)
        profile["verified_examples"][0]["teacher_id"] = "other-teacher"
        self.corpus_path.with_name("teacher-profile.json").write_text(json.dumps(profile), encoding="utf-8")
        self.assertEqual(teacher_skill.prepare_skill("chemistry-teacher", "乙烯", "student-a")[1]["teaching_example_ids"], [])
        profile["version"] = "stale"
        self.corpus_path.with_name("teacher-profile.json").write_text(json.dumps(profile), encoding="utf-8")
        self.assertFalse(teacher_skill.prepare_skill("chemistry-teacher", "化学平衡", "student-a")[1]["style_verified"])

    def test_two_stream_routes_keep_method_on_bare_followup_and_persist_route(self):
        self.install_verified_profile()
        for route in ("/api/chat", "/api/ask"):
            self.ask(route)
            events = self.ask(route, question="为什么？")
            context = next(event for event in events if event["type"] == "skill_context")
            self.assertEqual(context["teaching_route"], "followup")
            self.assertTrue(context["style_verified"])
            done = next(event for event in events if event["type"] == "done")
            self.assertEqual(done["skill_context"]["teaching_route"], "followup")

    def test_switching_off_resets_historical_teaching_format_without_loading_private_rules(self):
        prior = [{"is_user": False, "content": "先看结构，哪里发生变化？", "skill_id": "chemistry-teacher"}]
        with patch.object(teacher_skill, "_read_corpus", side_effect=AssertionError("off must not read corpus")), \
             patch.object(teacher_skill, "_read_rules", side_effect=AssertionError("off must not read skill")):
            fresh, _ = teacher_skill.prepare_skill("none", "讲解乙烯", "student-a")
            switched, context = teacher_skill.prepare_skill("none", "讲解乙烯", "student-a", recent_messages=prior)
        self.assertEqual(fresh, "")
        self.assertNotEqual(switched, "")
        self.assertEqual(context["skill_id"], "none")
        self.assertEqual(context["sources"], [])

    def test_stream_mode_switch_passes_scoped_history_to_default_boundary(self):
        for route in ("/api/chat", "/api/ask"):
            self.ask(route)
            self.ask(route, skill_id="none")
            self.assertNotIn("TEST_CHEMISTRY_RULE", self.prompts[-1])
            self.assertIn("本次教学Skill已切回默认回答", self.prompts[-1])

    def install_verified_catchphrase(self):
        profile = self.install_verified_profile()
        for chunk in self.corpus["chunks"]:
            if chunk.get("teacher_id") == "t1":
                chunk["text"] += "记住，判断要结合结构。"
        profile["verified_catchphrases"] = [{"id": "remember", "teacher_id": "t1", "reviewed": True,
            "phrase": "记住", "use_when": "收束需要记忆的判断条件", "routes": ["concept", "followup"],
            "source_ids": ["chem-1", "chem-2"]}]
        self.corpus_path.with_name("teacher-profile.json").write_text(json.dumps(profile), encoding="utf-8")
        self.write_corpus()
        return profile

    def test_reviewed_catchphrase_survives_missing_chapter_without_becoming_factual_evidence(self):
        self.install_verified_catchphrase()
        prompt, context = teacher_skill.prepare_skill("chemistry-teacher", "化学平衡怎样判断？", "student-a")
        self.assertEqual(context["catchphrase_ids"], ["remember"])
        self.assertEqual(context["sources"], [])
        self.assertIn('"phrase": "记住"', prompt)
        self.assertNotIn("记住", json.dumps(context, ensure_ascii=False))

    def test_catchphrases_respect_answer_only_task_account_and_default_boundaries(self):
        self.install_verified_catchphrase()
        for question, account in (("只要答案：乙烯是什么烃？", "student-a"),
                                  ("计算乙烯物质的量", "student-a"), ("乙烯为什么反应？", "student-b")):
            with self.subTest(question=question, account=account):
                prompt, context = teacher_skill.prepare_skill("chemistry-teacher", question, account)
                self.assertEqual(context["catchphrase_ids"], [])
                self.assertNotIn('"phrase": "记住"', prompt)
        with patch.object(teacher_skill, "_teacher_catchphrases", side_effect=AssertionError("default must skip wording")):
            teacher_skill.prepare_skill("none", "讲解乙烯", "student-a")

    def test_catchphrase_evidence_is_rechecked_for_version_teacher_recording_and_verbatim_wording(self):
        for invalid in ("stale", "teacher", "one_class", "missing_phrase", "malformed_routes"):
            with self.subTest(invalid=invalid):
                profile = self.install_verified_catchphrase()
                item = profile["verified_catchphrases"][0]
                if invalid == "stale":
                    profile["version"] = "stale"
                elif invalid == "teacher":
                    item["teacher_id"] = "other"
                elif invalid == "one_class":
                    item["source_ids"] = ["chem-1", "chem-1"]
                elif invalid == "missing_phrase":
                    item["phrase"] = "没有出现的词"
                else:
                    item["routes"] = "concept"
                self.corpus_path.with_name("teacher-profile.json").write_text(json.dumps(profile), encoding="utf-8")
                _, context = teacher_skill.prepare_skill("chemistry-teacher", "乙烯为什么反应？", "student-a")
                self.assertEqual(context["catchphrase_ids"], [])

    def test_both_stream_routes_pass_reviewed_wording_and_persist_selection(self):
        self.install_verified_catchphrase()
        for route in ("/api/chat", "/api/ask"):
            events = self.ask(route, question="乙烯为什么反应？")
            context = next(event for event in events if event["type"] == "skill_context")
            done = next(event for event in events if event["type"] == "done")
            self.assertEqual(context["catchphrase_ids"], ["remember"])
            self.assertEqual(done["skill_context"]["catchphrase_ids"], ["remember"])
            self.assertIn('"phrase": "记住"', self.prompts[-1])


if __name__ == "__main__":
    unittest.main()
