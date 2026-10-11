"""Synthetic accounts for isolated tests; never written to application storage."""
from __future__ import annotations

import copy

from services.accounts import create_account_record

FIXTURE_PASSWORD = "fixture-only-password"
_ACCOUNTS = {
    "student-test": create_account_record(FIXTURE_PASSWORD, name="測試學生", class_name="測試班"),
    **{f"fixture-teacher-{index}": create_account_record(
        FIXTURE_PASSWORD, name=f"測試老師 {index}", role="teacher") for index in range(4)},
    "review-student": create_account_record(FIXTURE_PASSWORD, name="測試學生甲",
                                             class_name="高二9班", grade="高二"),
    "review-other": create_account_record(FIXTURE_PASSWORD, name="測試學生乙",
                                           class_name="高二9班", grade="高二"),
    "fixture-prefix-student": create_account_record(FIXTURE_PASSWORD, name="測試學生丙",
                                                      class_name="高二8班", grade="高二"),
}


def fixture_accounts():
    return copy.deepcopy(_ACCOUNTS)
