"""Isolated startup configuration checks; never read a developer's local .env."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
CONFIG_KEYS = (
    "DEEPSEEK_API_KEY", "DEEPSEEK_API_URL", "DEEPSEEK_MODEL", "DEEPSEEK_REASONING_MODEL",
    "TENCENT_SECRET_ID", "TENCENT_SECRET_KEY", "TENCENT_OCR_REGION", "SERPER_API_KEY",
    "HOUKONG_DATA_DIR", "HOUKONG_CHROMA_DIR", "HOUKONG_HOST", "HOUKONG_PORT",
    "HOUKONG_RELOAD", "EMBEDDING_BACKEND", "LOCAL_EMBEDDING_MODEL",
)


class PublicConfigurationTests(unittest.TestCase):
    def test_suite_import_ignores_existing_private_storage_and_provider_key(self):
        with tempfile.TemporaryDirectory(prefix="pretend_private_config_") as name:
            private = Path(name)
            (private / "accounts.json").write_text("must not be loaded", encoding="utf-8")
            code = ("import os; previous=os.environ['HOUKONG_DATA_DIR']; import tests; "
                    "import main; from config import settings; "
                    "assert str(settings.DATA_DIR)!=previous; "
                    "assert not settings.DEEPSEEK_API_KEY; assert main.VALID_ACCOUNTS=={}")
            result = subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                env=os.environ | {"HOUKONG_DATA_DIR": str(private), "DEEPSEEK_API_KEY": "synthetic-key-must-be-cleared"},
                capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((private / "accounts.json").read_text(encoding="utf-8"), "must not be loaded")

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="houkong_config_")
        # Windows runners may expose TEMP through an 8.3 alias. Compare the
        # canonical fixture location with settings' resolved storage paths.
        self.base = Path(self.folder.name).resolve()
        self.app = self.base / "app"
        (self.app / "config").mkdir(parents=True)
        (self.app / "config" / "__init__.py").write_text("", encoding="utf-8")
        shutil.copyfile(ROOT / "config" / "settings.py", self.app / "config" / "settings.py")
        self.outside = self.base / "outside"
        self.outside.mkdir()
        self.env = os.environ.copy()
        for key in CONFIG_KEYS:
            self.env.pop(key, None)
        self.env["PYTHONPATH"] = str(self.app)
        self.env["LOCALAPPDATA"] = str(self.base / "local")

    def tearDown(self):
        self.folder.cleanup()

    def run_settings(self, updates=None):
        script = (
            "import json; from config import settings as s; "
            "print(json.dumps({'root':str(s.PROJECT_ROOT),'data':str(s.DATA_DIR),"
            "'chroma':str(s.CHROMA_DIR),'host':s.HOST,'port':s.PORT,'reload':s.RELOAD,"
            "'backend':s.EMBEDDING_BACKEND,'model_key_empty':not bool(s.DEEPSEEK_API_KEY),"
            "'search_key_empty':not bool(s.SERPER_API_KEY),'ocr':s.is_tencent_ocr_configured()}))"
        )
        return subprocess.run(
            [sys.executable, "-B", "-c", script], cwd=self.outside,
            env=self.env | (updates or {}), capture_output=True, text=True, check=False,
        )

    def read_settings(self, updates=None):
        result = self.run_settings(updates)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_no_key_defaults_are_offline_and_paths_ignore_working_directory(self):
        values = self.read_settings()
        self.assertEqual(Path(values["root"]), self.app.resolve())
        self.assertEqual(Path(values["data"]), self.app / "data")
        self.assertEqual(values["host"], "127.0.0.1")
        self.assertEqual(values["port"], 8000)
        self.assertFalse(values["reload"])
        self.assertEqual(values["backend"], "tfidf")
        self.assertTrue(values["model_key_empty"] and values["search_key_empty"])
        self.assertFalse(values["ocr"])

    def test_only_checkout_dotenv_is_loaded_and_environment_takes_precedence(self):
        (self.base / ".env").write_text("HOUKONG_PORT=8120\n", encoding="utf-8")
        self.assertEqual(self.read_settings()["port"], 8000)
        (self.app / ".env").write_text("HOUKONG_PORT=8121\n", encoding="utf-8")
        self.assertEqual(self.read_settings()["port"], 8121)
        self.assertEqual(self.read_settings({"HOUKONG_PORT": "8122"})["port"], 8122)

    def test_custom_relative_storage_and_startup_options(self):
        values = self.read_settings({"HOUKONG_DATA_DIR": "state", "HOUKONG_CHROMA_DIR": "index",
                                     "HOUKONG_HOST": "127.0.0.2", "HOUKONG_PORT": "8123",
                                     "HOUKONG_RELOAD": "true"})
        self.assertEqual(Path(values["data"]), self.app / "state")
        self.assertEqual(Path(values["chroma"]), self.app / "index")
        self.assertEqual((values["host"], values["port"], values["reload"]),
                         ("127.0.0.2", 8123, True))

    def test_windows_unicode_store_is_isolated_from_private_namespace(self):
        values = self.read_settings({"HOUKONG_DATA_DIR": "资料"})
        if os.name == "nt":
            self.assertIn("HouKongIntellectPublic", Path(values["chroma"]).parts)
            self.assertNotIn("HouKongIntellect", Path(values["chroma"]).parts)
            other = self.read_settings({"HOUKONG_DATA_DIR": "其他资料"})
            self.assertNotEqual(values["chroma"], other["chroma"])
        else:
            self.assertEqual(Path(values["chroma"]), self.app / "资料" / "chroma_db")

    def test_invalid_startup_options_and_unsafe_storage_fail_before_creation(self):
        cases = [
            {"HOUKONG_PORT": "0"}, {"HOUKONG_PORT": "65536"},
            {"HOUKONG_PORT": "not-an-integer"}, {"HOUKONG_RELOAD": "invalid"},
            {"HOUKONG_DATA_DIR": "."}, {"HOUKONG_CHROMA_DIR": "."},
            {"HOUKONG_DATA_DIR": "state", "HOUKONG_CHROMA_DIR": "state"},
        ]
        for options in cases:
            with self.subTest(options=options):
                result = self.run_settings(options)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("HOUKONG_", result.stderr)
        self.assertFalse((self.app / "data").exists())
        self.assertFalse((self.app / "state").exists())


if __name__ == "__main__":
    unittest.main()
