from __future__ import annotations

import anyio
import json
import sys

from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client


async def main() -> None:
    async with streamablehttp_client(
        "http://127.0.0.1:8004/",
        timeout=60,
        sse_read_timeout=120,
    ) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            if "--schemas" in sys.argv:
                for tool in tools.tools:
                    if tool.name.startswith("fxalpha_model") or tool.name == "fxalpha_record_model_step":
                        print(f"--- {tool.name}")
                        print(json.dumps(tool.inputSchema, ensure_ascii=False, indent=2))
            else:
                print(json.dumps([tool.name for tool in tools.tools], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    anyio.run(main)
