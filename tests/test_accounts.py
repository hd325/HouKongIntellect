"""Account initialization, password verification and public login contracts."""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

import tests  # Establish synthetic-only storage before application imports.
import main
from services.accounts import create_account_record, load_accounts, save_accounts, verify_password_record
from scripts import init_accounts

TEST_PASSWORD = "synthetic-password-for-tests"


class AccountStorageTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="hk_accounts_")
        self.path = Path(self.folder.name) / "accounts.json"

    def tearDown(self):
        self.folder.cleanup()

    def test_new_store_has_no_default_users_and_does_not_create_a_file(self):
        self.assertEqual(load_accounts(self.path), {})
        self.assertFalse(self.path.exists())

    def test_hashes_have_unique_salts_and_roundtrip_without_plaintext(self):
        first = create_account_record(TEST_PASSWORD, role="teacher", is_admin=True)
        second = create_account_record(TEST_PASSWORD)
        self.assertNotEqual(first["password_salt"], second["password_salt"])
        self.assertNotEqual(first["password_hash"], second["password_hash"])
        save_accounts({"synthetic-teacher": first, "synthetic-student": second}, self.path)
        self.assertNotIn(TEST_PASSWORD, self.path.read_text(encoding="utf-8"))
        loaded = load_accounts(self.path)
        self.assertTrue(verify_password_record(loaded["synthetic-teacher"], TEST_PASSWORD))
        self.assertFalse(verify_password_record(first, "wrong-password"))

    def test_whitespace_is_part_of_the_password(self):
        password = "  synthetic-spaces  "
        record = create_account_record(password)
        self.assertTrue(verify_password_record(record, password))
        self.assertFalse(verify_password_record(record, password.strip()))

    def test_legacy_plaintext_and_invalid_hashes_fail_closed(self):
        self.assertFalse(verify_password_record({"password": TEST_PASSWORD}, TEST_PASSWORD))
        self.assertFalse(verify_password_record(None, TEST_PASSWORD))
        record = create_account_record(TEST_PASSWORD)
        record["password_salt"] = "invalid"
        self.assertFalse(verify_password_record(record, TEST_PASSWORD))
        with self.assertRaises(ValueError):
            save_accounts({"synthetic-user": {"password": TEST_PASSWORD}}, self.path)

    def test_corrupt_store_and_unknown_roles_are_rejected(self):
        self.path.write_text('{"version":1,"accounts":{"test":{"password":"plaintext"}}}', encoding="utf-8")
        with self.assertRaises(RuntimeError):
            load_accounts(self.path)
        with self.assertRaises(ValueError):
            create_account_record(TEST_PASSWORD, role="admin")
        with self.assertRaises(ValueError):
            create_account_record("short")


class AccountApiTests(unittest.TestCase):
    def setUp(self):
        records = {"synthetic-teacher": create_account_record(TEST_PASSWORD, role="teacher", is_admin=True),
                   "100synthetic": create_account_record(TEST_PASSWORD, class_name="測試班")}
        self.sessions = {}

        async def set_session(session_id, data, **_kwargs):
            self.sessions[session_id] = data

        self.accounts = patch.dict(main.VALID_ACCOUNTS, records, clear=True)
        self.accounts.start()
        self.store = patch.object(main, "data_store")
        mocked_store = self.store.start()
        mocked_store.set_session = AsyncMock(side_effect=set_session)
        mocked_store.get_session = AsyncMock(side_effect=lambda session_id: self.sessions.get(session_id))
        mocked_store.delete_session = AsyncMock(side_effect=lambda session_id: self.sessions.pop(session_id, None))
        self.client = TestClient(main.app)

    def tearDown(self):
        self.client.close()
        self.store.stop()
        self.accounts.stop()

    def test_role_and_admin_permissions_come_from_the_account_record(self):
        response = self.client.post("/api/login", json={"account": "synthetic-teacher", "password": TEST_PASSWORD})
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertTrue(data["user_info"]["is_teacher"])
        self.assertTrue(data["user_info"]["is_admin"])
        self.assertFalse(main.is_teacher_account("100synthetic"))
        self.assertFalse(main.public_user_info("100synthetic")["is_admin"])
        serialized = json.dumps([data, self.sessions])
        self.assertNotIn(TEST_PASSWORD, serialized)
        self.assertNotIn("password_hash", serialized)
        self.assertNotIn("password_salt", serialized)

    def test_unknown_and_invalid_passwords_never_create_sessions(self):
        for account, password in (("missing", TEST_PASSWORD), ("synthetic-teacher", "wrong")):
            response = self.client.post("/api/login", json={"account": account, "password": password})
            self.assertEqual(response.status_code, 401)
        self.assertEqual(self.sessions, {})

    def test_oversized_login_fields_are_rejected_before_verification(self):
        for body in ({"account": "a" * 65, "password": TEST_PASSWORD},
                     {"account": "synthetic-teacher", "password": "a" * 1025}):
            self.assertEqual(self.client.post("/api/login", json=body).status_code, 422)
        self.assertEqual(self.sessions, {})

    def test_removed_account_and_malformed_session_are_invalidated(self):
        response = self.client.post("/api/login", json={"account": "synthetic-teacher", "password": TEST_PASSWORD})
        session_id = response.json()["data"]["session_id"]
        del main.VALID_ACCOUNTS["synthetic-teacher"]
        self.assertEqual(self.client.get("/api/session", headers={"X-Session-ID": session_id}).status_code, 401)
        self.assertNotIn(session_id, self.sessions)
        self.sessions["malformed"] = {"account": "100synthetic", "created_at": "invalid-timestamp"}
        self.assertEqual(self.client.get("/api/session", headers={"X-Session-ID": "malformed"}).status_code, 401)
        self.assertNotIn("malformed", self.sessions)

    def test_missing_model_key_produces_friendly_event_without_network(self):
        async def collect():
            return [event async for event in main.stream_deepseek("prompt", "question")]

        with patch.dict(main.API_CONFIG, {"DEEPSEEK_API_KEY": ""}), patch("main.httpx.AsyncClient") as client:
            events = asyncio.run(collect())
            client.assert_not_called()
        self.assertEqual(len(events), 1)
        self.assertIn('"type": "error"', events[0])
        self.assertIn("DEEPSEEK_API_KEY", events[0])


class AccountCliTests(unittest.TestCase):
    def test_interactive_password_and_explicit_teacher_role_create_hashed_record(self):
        with tempfile.TemporaryDirectory(prefix="hk_account_cli_") as folder:
            path = Path(folder) / "accounts.json"
            with patch("sys.argv", ["init_accounts.py", "--account", "cli-synthetic", "--role", "teacher", "--admin"]), \
                 patch("sys.stdin.isatty", return_value=True), \
                 patch("scripts.init_accounts.getpass.getpass", side_effect=[TEST_PASSWORD, TEST_PASSWORD]), \
                 patch("scripts.init_accounts.load_accounts", return_value={}), \
                 patch("scripts.init_accounts.save_accounts", side_effect=lambda accounts: save_accounts(accounts, path)), \
                 patch("builtins.print"):
                self.assertEqual(init_accounts.main(), 0)
            record = load_accounts(path)["cli-synthetic"]
            self.assertEqual(record["role"], "teacher")
            self.assertTrue(record["is_admin"])
            self.assertTrue(verify_password_record(record, TEST_PASSWORD))
            self.assertNotIn(TEST_PASSWORD, path.read_text(encoding="utf-8"))

    def test_noninteractive_invocation_never_reads_or_writes_accounts(self):
        with patch("sys.argv", ["init_accounts.py"]), patch("sys.stdin.isatty", return_value=False), \
             patch("scripts.init_accounts.load_accounts") as load, \
             patch("scripts.init_accounts.save_accounts") as save, patch("sys.stderr"):
            with self.assertRaises(SystemExit) as failure:
                init_accounts.main()
            self.assertEqual(failure.exception.code, 2)
            load.assert_not_called()
            save.assert_not_called()

    def test_existing_account_cannot_be_replaced_without_explicit_flag(self):
        with patch("sys.argv", ["init_accounts.py", "--account", "cli-synthetic", "--role", "student"]), \
             patch("sys.stdin.isatty", return_value=True), \
             patch("scripts.init_accounts.load_accounts", return_value={"cli-synthetic": {}}), \
             patch("scripts.init_accounts.getpass.getpass") as password, \
             patch("scripts.init_accounts.save_accounts") as save, patch("sys.stderr"):
            with self.assertRaises(SystemExit):
                init_accounts.main()
            password.assert_not_called()
            save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
