"""Loaded by pytest before any test module: every test run uses its own ports and run directory,
so tests never touch the development sandbox or an evaluation in progress."""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("SANDBOX_PORT_BASE", "17000")
os.environ.setdefault("SANDBOX_RUN_DIR", str(ROOT / ".run-test"))
