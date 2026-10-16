#!/usr/bin/env python3
"""Thin command-line entry for the shared scanner in ``noah.secret_scan``.

    python3 scripts/secret_scan.py [--strings] <file-or-dir> ...   (exit 0 clean, 3 hit, 2 error)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from noah.secret_scan.__main__ import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
