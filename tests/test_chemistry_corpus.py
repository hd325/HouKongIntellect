"""Account-scoped chemistry corpus promotion from temporary reviewed fixtures."""
from __future__ import annotations

import json
import copy
import tempfile
import unittest
from pathlib import Path

import tests  # Establish synthetic-only storage before application imports.
from scripts.build_chemistry_corpus import build


class ChemistryCorpusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hk_chemistry_corpus_")
        self.folder = Path(self.temp.name)
        self.inventory = {"inventory_complete": True, "recordings": [{"recordingId": "r1", "title": "課堂一"},
                                                                      {"recordingId": "r2", "title": "課堂二"}]}
        self.transcript = {"status": "complete", "content_version": "version-1", "title": "化學課堂",
                           "paragraphs": [{"paragraph": 1, "text": "乙烯含有碳碳雙鍵。"},
                                          {"paragraph": 2, "text": "先看結構，再判斷反應類型。"}]}
        self.source = {"id": "s1", "recording_id": "r1", "content_version": "version-1", "paragraphs": [1],
                       "quote": "乙烯含有碳碳雙鍵。", "keywords": ["乙烯"], "chemistry_verified": True,
                       "speaker_role_verified": False}
        self.curation = {"schema_version": 1, "sources": [self.source], "teachers": [], "concepts": [], "style_rules": []}
        self.save()

    def tearDown(self):
        self.temp.cleanup()

    def put(self, path, value):
        target = self.folder / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def save(self):
        self.put("inventory.json", self.inventory)
        self.put("coverage.json", {"incomplete": False})
        self.put("curation.json", self.curation)
        self.put("transcripts/r1.json", self.transcript)
        self.put("transcripts/r2.json", self.transcript)

    def install_catchphrase(self):
        self.source.update(teacher_id="t1", speaker_role_verified=True)
        self.curation["sources"].append(dict(self.source, id="s2", recording_id="r2"))
        self.curation["teachers"] = [{"id": "t1", "identity_verified": True}]
        self.curation["catchphrases"] = [{"id": "phrase1", "teacher_id": "t1", "phrase": "乙烯",
            "source_ids": ["s1", "s2"], "reviewed": True,
            "routes": ["concept", "followup"], "use_when": "讲到这种物质时",
            "statistics": {"occurrences": 2, "recordings": 2}}]
        self.save()

    def profile(self):
        return json.loads((self.folder / "teacher-profile.json").read_text(encoding="utf-8"))

    def test_verified_chemistry_does_not_establish_teacher_identity(self):
        corpus = build(self.folder, "fixture-owner")
        self.assertEqual(corpus["owner_account"], "fixture-owner")
        self.assertEqual(corpus["chunks"][0]["speaker"], "unknown")
        self.assertEqual(corpus["chunks"][0]["confidence"], "verified")
        self.assertFalse(corpus["style_verified"])
        self.assertTrue((self.folder / "source-index.json").exists())
        self.assertEqual(json.loads((self.folder / "corpus.json").read_text(encoding="utf-8")), corpus)
        self.assertEqual(self.profile()["verified_catchphrases"], [])

    def test_unreviewed_chemistry_is_excluded(self):
        self.source["chemistry_verified"] = False
        self.save()
        self.assertEqual(build(self.folder, "fixture-owner")["chunks"], [])

    def test_partial_stale_or_unmatched_evidence_cannot_be_promoted(self):
        for change in ("partial", "stale", "bad_quote", "outside_inventory"):
            with self.subTest(change=change):
                self.transcript["status"] = "complete"
                self.source.update(content_version="version-1", recording_id="r1", quote="乙烯含有碳碳雙鍵。")
                if change == "partial":
                    self.transcript["status"] = "partial"
                elif change == "stale":
                    self.source["content_version"] = "old-version"
                elif change == "bad_quote":
                    self.source["quote"] = "這句話從未出現過。"
                else:
                    self.source["recording_id"] = "not-in-inventory"
                self.save()
                with self.assertRaises(ValueError):
                    build(self.folder, "fixture-owner")
                self.assertFalse((self.folder / "corpus.json").exists())

    def test_owner_and_duplicate_sources_are_rejected(self):
        with self.assertRaises(ValueError):
            build(self.folder, " ")
        self.curation["sources"].append(dict(self.source))
        self.save()
        with self.assertRaises(ValueError):
            build(self.folder, "fixture-owner")

    def test_style_requires_reviewed_same_identified_teacher_across_recordings(self):
        self.source.update(teacher_id="t1", speaker_role_verified=True)
        self.curation["sources"].append(dict(self.source, id="s2", recording_id="r2"))
        self.curation["teachers"] = [{"id": "t1", "identity_verified": True}]
        self.curation["style_rules"] = [{"id": "style1", "teacher_id": "t1", "reviewed": True,
                                          "rule": "先看結構，再判斷反應類型。", "source_ids": ["s1", "s2"]}]
        self.save()
        corpus = build(self.folder, "fixture-owner")
        self.assertTrue(corpus["style_verified"])
        profile = json.loads((self.folder / "teacher-profile.json").read_text(encoding="utf-8"))
        self.assertEqual(len(profile["verified_rules"]), 1)
        for change in ("same_recording", "unknown_speaker", "unknown_identity", "missing_evidence"):
            with self.subTest(change=change):
                self.curation["sources"][1].update(recording_id="r2", speaker_role_verified=True)
                self.curation["teachers"][0]["identity_verified"] = True
                self.curation["style_rules"][0]["source_ids"] = ["s1", "s2"]
                if change == "same_recording":
                    self.curation["sources"][1]["recording_id"] = "r1"
                elif change == "unknown_speaker":
                    self.curation["sources"][1]["speaker_role_verified"] = False
                elif change == "unknown_identity":
                    self.curation["teachers"][0]["identity_verified"] = False
                else:
                    self.curation["style_rules"][0]["source_ids"] = ["s1", "missing"]
                self.save()
                self.assertFalse(build(self.folder, "fixture-owner")["style_verified"])

    def test_concepts_require_valid_reviewed_sources(self):
        self.curation["concepts"] = [{"id": "addition", "term": "加成反應", "source_ids": ["missing"]}]
        self.save()
        with self.assertRaises(ValueError):
            build(self.folder, "fixture-owner")

    def test_worked_examples_require_reviewed_same_teacher_and_affect_version(self):
        self.source.update(teacher_id="t1", speaker_role_verified=True)
        self.curation["teachers"] = [{"id": "t1", "identity_verified": True}]
        self.curation["teaching_examples"] = [{"id": "example1", "teacher_id": "t1", "reviewed": True,
            "route": "concept", "question": "如何判断？", "response": "先看结构。", "source_ids": ["s1"]}]
        self.save()
        first = build(self.folder, "fixture-owner")
        profile = json.loads((self.folder / "teacher-profile.json").read_text(encoding="utf-8"))
        self.assertEqual(profile["verified_examples"][0]["derivation"], "system_adaptation_of_reviewed_teaching")
        self.curation["teaching_examples"][0]["response"] = "先看条件。"
        self.save()
        self.assertNotEqual(build(self.folder, "fixture-owner")["version"], first["version"])
        self.curation["teaching_examples"][0]["source_ids"] = ["not-reviewed"]
        self.save()
        with self.assertRaises(ValueError):
            build(self.folder, "fixture-owner")

    def test_reviewed_catchphrase_preserves_exact_phrase_evidence_and_metadata(self):
        self.install_catchphrase()
        corpus = build(self.folder, "fixture-owner")
        self.assertEqual(self.profile()["verified_catchphrases"], self.curation["catchphrases"])
        self.assertEqual(self.profile()["version"], corpus["version"])

    def test_unreviewed_catchphrase_is_ignored_without_changing_version(self):
        first = build(self.folder, "fixture-owner")
        self.curation["catchphrases"] = [{"reviewed": False, "phrase": "未核实的话", "source_ids": "invalid"}]
        self.save()
        self.assertEqual(build(self.folder, "fixture-owner")["version"], first["version"])
        self.assertEqual(self.profile()["verified_catchphrases"], [])

    def test_reviewed_catchphrase_rejects_unverified_or_cross_classroom_evidence(self):
        self.install_catchphrase()
        original = copy.deepcopy(self.curation)
        for change in ("same_recording", "different_teacher", "unknown_speaker", "unknown_identity",
                       "missing_evidence", "unreviewed_evidence", "phrase_missing_in_one_source"):
            with self.subTest(change=change):
                self.curation = copy.deepcopy(original)
                if change == "same_recording":
                    self.curation["sources"][1]["recording_id"] = "r1"
                elif change == "different_teacher":
                    self.curation["teachers"].append({"id": "t2", "identity_verified": True})
                    self.curation["sources"][1]["teacher_id"] = "t2"
                elif change == "unknown_speaker":
                    self.curation["sources"][1]["speaker_role_verified"] = False
                elif change == "unknown_identity":
                    self.curation["teachers"][0]["identity_verified"] = False
                elif change == "missing_evidence":
                    self.curation["catchphrases"][0]["source_ids"] = ["s1", "missing"]
                elif change == "unreviewed_evidence":
                    self.curation["sources"][1]["chemistry_verified"] = False
                else:
                    self.curation["sources"][1].update(paragraphs=[2], quote="先看結構，再判斷反應類型。")
                self.save()
                with self.assertRaises(ValueError):
                    build(self.folder, "fixture-owner")
                self.assertFalse((self.folder / "corpus.json").exists())

    def test_reviewed_catchphrase_validates_short_phrase_and_list_fields(self):
        self.install_catchphrase()
        original = copy.deepcopy(self.curation)
        invalid_fields = [("phrase", ""), ("phrase", "  "), ("phrase", "乙" * 31),
                          ("phrase", "乙\n烯"), ("phrase", 1), ("id", ""), ("teacher_id", None),
                          ("source_ids", "s1"), ("source_ids", []), ("source_ids", ["s1", {}]),
                          ("routes", "concept"), ("routes", ["concept", None]), ("use_when", [])]
        for field, value in invalid_fields:
            with self.subTest(field=field, value=value):
                self.curation = copy.deepcopy(original)
                self.curation["catchphrases"][0][field] = value
                self.save()
                with self.assertRaises(ValueError):
                    build(self.folder, "fixture-owner")

    def test_catchphrase_collection_and_duplicate_ids_are_rejected(self):
        self.install_catchphrase()
        original = copy.deepcopy(self.curation)
        for invalid in (None, "phrase", ["phrase"], original["catchphrases"] * 2):
            with self.subTest(value=invalid):
                self.curation["catchphrases"] = invalid
                self.save()
                with self.assertRaises(ValueError):
                    build(self.folder, "fixture-owner")

    def test_catchphrase_changes_profile_version_even_with_same_sources(self):
        self.install_catchphrase()
        first = build(self.folder, "fixture-owner")
        self.curation["catchphrases"][0]["phrase"] = "碳碳雙鍵"
        self.save()
        second = build(self.folder, "fixture-owner")
        self.assertNotEqual(first["version"], second["version"])
        self.assertEqual(self.profile()["version"], second["version"])
        self.assertEqual(self.profile()["verified_catchphrases"][0]["phrase"], "碳碳雙鍵")


if __name__ == "__main__":
    unittest.main()
