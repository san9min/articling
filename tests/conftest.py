"""Puts the repo root on sys.path so the `articling` package is found even
when just running `pytest` without `pip install -e .`."""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
