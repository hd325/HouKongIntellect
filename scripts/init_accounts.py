"""Create local accounts interactively; passwords never appear in arguments."""
from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.accounts import ACCOUNTS_FILE, create_account_record, load_accounts, save_accounts


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a local HouKongIntellect account.")
    parser.add_argument("--account", help="Account identifier; prompted when omitted.")
    parser.add_argument("--role", choices=("student", "teacher"), help="Explicit account role.")
    parser.add_argument("--name", default="", help="Optional display name.")
    parser.add_argument("--class-name", default="", help="Class used for file permissions.")
    parser.add_argument("--grade", default="", help="Grade used for file permissions.")
    parser.add_argument("--admin", action="store_true", help="Grant permission to delete all files.")
    parser.add_argument("--replace", action="store_true", help="Explicitly replace an existing account.")
    args = parser.parse_args()
    if not sys.stdin.isatty():
        parser.error("Run this command in an interactive terminal to enter a hidden password.")
    account = (args.account or input("Account identifier: ")).strip()
    if not account or len(account) > 64 or any(character.isspace() for character in account):
        parser.error("Account identifier must contain 1 to 64 characters without whitespace.")
    role = args.role or input("Role (student/teacher): ").strip().lower()
    if role not in {"student", "teacher"}:
        parser.error("Role must be student or teacher.")
    try:
        accounts = load_accounts()
        if account in accounts and not args.replace:
            parser.error("Account already exists; use --replace to reset it explicitly.")
        password = getpass.getpass("Password (at least 12 characters): ")
        if password != getpass.getpass("Confirm password: "):
            parser.error("Passwords do not match.")
        accounts[account] = create_account_record(password, name=args.name, class_name=args.class_name,
                                                  grade=args.grade, role=role, is_admin=args.admin)
        save_accounts(accounts)
    except (ValueError, RuntimeError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"Account saved in {ACCOUNTS_FILE}. Restart the application to load account changes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
