"""Runtime configuration from environment variables."""
from __future__ import annotations

import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("PRINTER_DASH_DATA", Path.cwd() / "data")).resolve()
DB_PATH = DATA_DIR / "dashboard.db"
THUMB_DIR = DATA_DIR / "thumbnails"
