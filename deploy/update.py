"""Host launcher: python deploy/update.py native|docker --help."""

from __future__ import annotations

import sys
from pathlib import Path

if sys.version_info < (3, 11):  # noqa: UP036 - explain unsupported host Python versions
    raise SystemExit("更新管理器需要 Python 3.11+")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dyspark.update_manager import main

if __name__ == "__main__":
    raise SystemExit(main())
