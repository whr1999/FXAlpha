#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from domain.data_foundation.stock_metadata import build_stock_identity_cache, stock_identity_cache_status


def main() -> None:
    parser = argparse.ArgumentParser(description="Build FXAlpha stock identity cache from the canonical Tushare production HDF5.")
    parser.add_argument("--force", action="store_true", help="rebuild even when the cache is fresh")
    parser.add_argument("--status", action="store_true", help="print cache status without rebuilding")
    args = parser.parse_args()

    result = stock_identity_cache_status() if args.status else build_stock_identity_cache(force=args.force)
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
