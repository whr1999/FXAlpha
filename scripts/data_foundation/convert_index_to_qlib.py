#!/usr/bin/env python3
from __future__ import annotations

import runpy
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONVERTER_SCRIPT = PROJECT_ROOT / "scripts" / "data_foundation" / "tushare_index_to_qlib.py"

if not CONVERTER_SCRIPT.exists():
    raise SystemExit(f"tushare qlib index converter not found: {CONVERTER_SCRIPT}")

sys.path.insert(0, str(CONVERTER_SCRIPT.parent))
runpy.run_path(str(CONVERTER_SCRIPT), run_name="__main__")
