from __future__ import annotations

import json
import re

from storage.paths import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL


_SYSTEM_PROMPT = """You convert locally-researched A-share factor expressions into WorldQuant BRAIN FASTEXPR expressions.

Rules:
- Preserve the original market mechanism, not the exact local syntax.
- Output exactly one FASTEXPR expression on one line.
- Do not output markdown, explanation, or code fences.
- If the local expression cannot be expressed faithfully in FASTEXPR, return exactly: NOT_CONVERTIBLE
- Prefer simple, valid BRAIN operators over clever but unsupported local aliases.
- The output must be directly suitable for WorldQuant BRAIN simulation.
"""


def _clean_response(text: str) -> str:
    line = (text or "").strip().splitlines()[0].strip() if text else ""
    line = re.sub(r"^```[a-zA-Z]*", "", line).strip().strip("`").strip()
    return line


def _unwrap_function(expr: str, func_name: str, *, keep_first_arg: bool) -> str:
    token = f"{func_name}("
    while token in expr:
        start = expr.find(token)
        if start < 0:
            break
        i = start + len(token)
        depth = 1
        comma_idx = -1
        while i < len(expr) and depth > 0:
            ch = expr[i]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif ch == "," and depth == 1 and comma_idx < 0:
                comma_idx = i
            i += 1
        if depth != 0:
            break
        inner = expr[start + len(token):i - 1]
        if keep_first_arg and comma_idx >= 0:
            inner = expr[start + len(token):comma_idx]
        expr = expr[:start] + f"({inner.strip()})" + expr[i:]
    return expr


def normalize_wq_expression(local_expression: str) -> str:
    expr = (local_expression or "").strip()
    replacements = [
        (r"\bts_std\(", "ts_std_dev("),
        (r"\bts_decay_linear\(", "decay_linear("),
        (r"\bsign_power\(", "signed_power("),
        (r"\badv20\b", "ts_mean(volume,20)"),
    ]
    for pattern, repl in replacements:
        expr = re.sub(pattern, repl, expr)
    expr = _unwrap_function(expr, "tanh", keep_first_arg=False)
    expr = _unwrap_function(expr, "sigmoid", keep_first_arg=False)
    expr = _unwrap_function(expr, "decay_linear", keep_first_arg=True)
    expr = _unwrap_function(expr, "ts_decay_linear", keep_first_arg=True)
    return expr


def generate_wq_expression(
    local_expression: str,
    *,
    direction: str = "",
    context: dict | None = None,
) -> str | None:
    if not local_expression:
        return None

    heuristic = normalize_wq_expression(local_expression)
    if not LLM_API_KEY:
        return heuristic or None

    from openai import OpenAI

    client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)
    payload = {
        "local_expression": local_expression,
        "direction": direction,
        "context": context or {},
        "heuristic_fastexpr_hint": heuristic,
    }
    try:
        resp = client.chat.completions.create(
            model=LLM_MODEL or "deepseek-v4-flash",
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            temperature=0.1,
            max_tokens=300,
            timeout=60,
        )
        text = _clean_response(resp.choices[0].message.content or "")
        if text and text.upper() != "NOT_CONVERTIBLE":
            return normalize_wq_expression(text)
    except Exception:
        pass
    return heuristic or None
