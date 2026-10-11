"""Local account storage with no bundled users or plaintext passwords."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
from pathlib import Path
from typing import Any

from config.settings import DATA_DIR

ACCOUNTS_FILE = Path(DATA_DIR) / "accounts.json"
PASSWORD_ITERATIONS = 600_000
_FIELDS = {"name", "class", "grade", "role", "is_admin", "password_algorithm",
           "password_iterations", "password_salt", "password_hash"}


def create_account_record(password: str, *, name: str = "", class_name: str = "",
                          grade: str = "", role: str = "student",
                          is_admin: bool = False) -> dict[str, Any]:
    """Create a record; preserve password whitespace and generate a unique salt."""
    if not isinstance(password, str) or len(password) < 12:
        raise ValueError("Password must contain at least 12 characters.")
    if len(password.encode("utf-8")) > 1024:
        raise ValueError("Password is too long.")
    if role not in {"student", "teacher"}:
        raise ValueError("Role must be student or teacher.")
    salt = secrets.token_hex(32)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                               bytes.fromhex(salt), PASSWORD_ITERATIONS).hex()
    return {"name": name, "class": class_name, "grade": grade, "role": role,
            "is_admin": bool(is_admin), "password_algorithm": "pbkdf2_sha256",
            "password_iterations": PASSWORD_ITERATIONS,
            "password_salt": salt, "password_hash": digest}


def _validate_record(record: Any) -> bool:
    return (isinstance(record, dict) and set(record).issubset(_FIELDS)
            and record.get("role") in {"student", "teacher"}
            and record.get("password_algorithm") == "pbkdf2_sha256"
            and type(record.get("password_iterations")) is int
            and 100_000 <= record["password_iterations"] <= 2_000_000
            and isinstance(record.get("password_salt"), str)
            and re.fullmatch(r"[0-9a-f]{64}", record["password_salt"]) is not None
            and isinstance(record.get("password_hash"), str)
            and re.fullmatch(r"[0-9a-f]{64}", record["password_hash"]) is not None
            and type(record.get("is_admin", False)) is bool
            and all(isinstance(record.get(field, ""), str) for field in ("name", "class", "grade")))


def verify_password_record(record: Any, password: str) -> bool:
    """Fail closed for legacy plaintext, malformed records and unknown accounts."""
    if not _validate_record(record) or not isinstance(password, str):
        return False
    if len(password.encode("utf-8")) > 1024:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                               bytes.fromhex(record["password_salt"]),
                               record["password_iterations"]).hex()
    return hmac.compare_digest(digest, record["password_hash"])


def load_accounts(path: Path | str = ACCOUNTS_FILE) -> dict[str, dict[str, Any]]:
    path = Path(path)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        accounts = payload["accounts"]
        if payload.get("version") != 1 or not isinstance(accounts, dict):
            raise ValueError("Unsupported account file format.")
        if not all(isinstance(account, str) and account and account == account.strip()
                   and _validate_record(record) for account, record in accounts.items()):
            raise ValueError("Invalid account record.")
        return accounts
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RuntimeError("Cannot load local account file; check its format and permissions.") from exc


def save_accounts(accounts: dict[str, dict[str, Any]], path: Path | str = ACCOUNTS_FILE) -> None:
    if not all(isinstance(account, str) and account and account == account.strip()
               and len(account) <= 64 and _validate_record(record)
               for account, record in accounts.items()):
        raise ValueError("Invalid account records; plaintext passwords cannot be saved.")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                         prefix=".accounts-", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump({"version": 1, "accounts": accounts}, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
