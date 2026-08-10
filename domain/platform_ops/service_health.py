from __future__ import annotations

import json
from urllib.error import URLError
from urllib.request import Request, urlopen


def http_json_health(url: str, timeout: float = 2.0) -> dict:
    try:
        request = Request(url, headers={"Accept": "application/json"})
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"raw": raw[:500]}
        return {"ok": True, "url": url, "payload": payload}
    except (OSError, URLError) as exc:
        return {"ok": False, "url": url, "error": str(exc)}
