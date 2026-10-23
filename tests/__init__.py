"""Offline tests and synthetic fixtures for the public release."""
"""Set up synthetic storage before any application import in the test suite."""
import atexit
import os
from pathlib import Path
import shutil
import sys
import tempfile

if "config.settings" in sys.modules:
    raise RuntimeError("Start tests in a fresh Python process, before importing the application.")

_temp_parent = Path(tempfile.gettempdir()).resolve()
_test_root = Path(tempfile.mkdtemp(prefix="houkong-synthetic-tests-")).resolve()
(_test_root / ".synthetic-fixture").write_text("synthetic test data only", encoding="utf-8")
for _key in ("DEEPSEEK_API_KEY", "TENCENT_SECRET_ID", "TENCENT_SECRET_KEY", "SERPER_API_KEY",
             "HUAWEI_AK", "HUAWEI_SK", "HUAWEI_PROJECT_ID"):
    os.environ[_key] = ""
os.environ.update(HOUKONG_DATA_DIR=str(_test_root / "data"),
                  HOUKONG_CHROMA_DIR=str(_test_root / "chroma"),
                  EMBEDDING_BACKEND="tfidf", ENABLE_FILE_AGENT="true")


def _cleanup():
    resolved = _test_root.resolve()
    if (resolved.is_relative_to(_temp_parent) and resolved.name.startswith("houkong-synthetic-tests-")
            and (resolved / ".synthetic-fixture").is_file()):
        shutil.rmtree(resolved, ignore_errors=True)


atexit.register(_cleanup)
