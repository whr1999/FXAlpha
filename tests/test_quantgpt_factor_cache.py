import sys
from pathlib import Path
import asyncio

import pandas as pd


QUANTGPT_ROOT = Path(__file__).resolve().parents[1] / "third_party" / "quantgpt"
if str(QUANTGPT_ROOT) not in sys.path:
    sys.path.insert(0, str(QUANTGPT_ROOT))

from quantgpt import mcp_server


def test_factor_df_cache_returns_copy_and_respects_lru_limit(monkeypatch):
    monkeypatch.setattr(mcp_server, "_FACTOR_DF_CACHE_MAX", 2)
    mcp_server._FACTOR_DF_CACHE.clear()

    keys = [("expr", idx) for idx in range(3)]
    for idx, key in enumerate(keys):
        mcp_server._store_cached_factor_df(key, pd.DataFrame({"factor_value": [idx]}))

    assert keys[0] not in mcp_server._FACTOR_DF_CACHE
    cached = mcp_server._get_cached_factor_df(keys[-1])
    assert cached is not None
    cached.loc[0, "factor_value"] = 999

    cached_again = mcp_server._get_cached_factor_df(keys[-1])
    assert cached_again.loc[0, "factor_value"] == 2

    mcp_server._FACTOR_DF_CACHE.clear()


def test_factor_df_validation_requires_existing_cache(monkeypatch):
    mcp_server._FACTOR_DF_CACHE.clear()

    def fail_fetch(*args, **kwargs):
        raise AssertionError("validation should not silently rebuild factor_df")

    monkeypatch.setattr(mcp_server, "_fetch_data_for_market", fail_fetch)

    result = asyncio.run(
        mcp_server._factor_df_for_validation(
            "rank(close)",
            "all_market",
            "2022-01-01",
            "2025-06-30",
            5,
            False,
            False,
        )
    )

    assert result is None
