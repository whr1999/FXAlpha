from __future__ import annotations

import json
import os
import re
import socket
import subprocess
from dataclasses import dataclass
from typing import Any

from storage.paths import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL, LLM_CROSS_REVIEW_MODEL, LLM_PROVIDER


class DeepSeekClientError(RuntimeError):
    """Raised when the Orchestrator LLM call cannot produce usable JSON."""

    def __init__(self, message: str, *, raw_preview: str | None = None, category: str | None = None):
        super().__init__(message)
        self.raw_preview = raw_preview
        self.category = category or _deepseek_error_category(message)


def _extract_json_object(text: str) -> dict[str, Any]:
    raw = str(text or "").strip()
    preview = raw[:1200]
    if not raw:
        raise DeepSeekClientError("empty_llm_response")
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.I).strip()
        raw = re.sub(r"\s*```$", "", raw).strip()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            raise DeepSeekClientError("llm_response_not_json", raw_preview=preview) from None
        try:
            payload = json.loads(raw[start : end + 1])
        except json.JSONDecodeError as exc:
            raise DeepSeekClientError(f"llm_response_not_valid_json:{exc.msg}", raw_preview=preview) from None
    if not isinstance(payload, dict):
        raise DeepSeekClientError("llm_json_must_be_object")
    return payload


def _deepseek_error_category(error: Any) -> str:
    """Classify failures so transport retries never become JSON-repair prompts."""

    message = str(error or "")
    lowered = message.lower()
    if message == "empty_llm_response":
        return "empty_content"
    if (
        message == "llm_response_not_json"
        or message == "llm_json_must_be_object"
        or message.startswith("llm_response_not_valid_json:")
        or "expecting value" in lowered
        or "unterminated string" in lowered
    ):
        return "json_parse_error"
    if "missing_required_fields" in message or "next_stage_not_allowed" in message or "candidate_expression_missing" in message:
        return "schema_contract_error"
    if "error code: 400" in lowered or "invalid_request" in lowered:
        return "provider_400"
    if (
        "timeout" in lowered
        or "timed out" in lowered
        or "connection" in lowered
        or "network" in lowered
        or "httpx" in lowered
        or "api connection" in lowered
    ):
        return "transport_error"
    return "unknown"


def _is_json_repair_category(category: str) -> bool:
    return category in {"empty_content", "json_parse_error", "schema_contract_error"}


def _is_transport_retry_category(category: str) -> bool:
    return category in {"transport_error", "unknown"}


def _deepseek_base_urls(base_url: str) -> tuple[str, ...]:
    """Prefer the official DeepSeek OpenAI-compatible base URL.

    DeepSeek's current OpenAI SDK examples use https://api.deepseek.com, while
    older local configs may still carry /v1.  Keep this normalization scoped to
    the DeepSeek provider so other OpenAI-compatible gateways remain untouched.
    """

    raw = str(base_url or "").rstrip("/")
    urls = [raw] if raw else []
    if str(LLM_PROVIDER or "").lower() == "deepseek" and raw == "https://api.deepseek.com/v1":
        urls.insert(0, "https://api.deepseek.com")
    return tuple(dict.fromkeys(url for url in urls if url))


def _llm_models(primary: str) -> tuple[str, ...]:
    models = [str(primary or "").strip(), str(LLM_CROSS_REVIEW_MODEL or "").strip()]
    if str(LLM_PROVIDER or "").lower() == "deepseek":
        allowed = {"deepseek-v4-flash", "deepseek-v4", "deepseek-v4-pro"}
        ordered = [model for model in models if model and model.lower() in allowed]
        if not ordered:
            ordered = ["deepseek-v4-flash"]
        return tuple(dict.fromkeys(ordered))
    return tuple(dict.fromkeys(model for model in models if model))


def _preferred_llm_model(primary: str) -> str:
    ordered = _llm_models(primary)
    if ordered:
        return ordered[0]
    fallback = str(primary or "").strip()
    return fallback or "deepseek-v4-flash"


def _provider_model_name(model: str) -> str:
    raw = str(model or "").strip()
    if str(LLM_PROVIDER or "").lower() == "deepseek" and raw.lower() == "deepseek-v4":
        return "deepseek-v4-pro"
    return raw


def _deepseek_json_mode_enabled() -> bool:
    raw = str(os.environ.get("FXALPHA_DEEPSEEK_JSON_MODE") or "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return False


def _message_text(message: Any) -> str:
    content = getattr(message, "content", None)
    if isinstance(content, str) and content.strip():
        return content
    if hasattr(message, "model_dump"):
        dumped = message.model_dump()
        for key in ("content", "parsed"):
            value = dumped.get(key)
            if isinstance(value, str) and value.strip():
                return value
            if isinstance(value, dict):
                return json.dumps(value, ensure_ascii=False)
    return ""


def _message_reasoning_text(message: Any) -> str:
    reasoning = getattr(message, "reasoning_content", None)
    if isinstance(reasoning, str) and reasoning.strip():
        return reasoning
    if hasattr(message, "model_dump"):
        dumped = message.model_dump()
        for key in ("reasoning_content", "reasoning"):
            value = dumped.get(key)
            if isinstance(value, str) and value.strip():
                return value
            if isinstance(value, list):
                parts: list[str] = []
                for item in value[:8]:
                    if isinstance(item, dict):
                        text = item.get("text") or item.get("content")
                        if isinstance(text, str) and text.strip():
                            parts.append(text.strip())
                    elif isinstance(item, str) and item.strip():
                        parts.append(item.strip())
                if parts:
                    return "\n".join(parts)
    return ""


def _message_debug_preview(message: Any) -> str:
    """Return a bounded provider-message preview for trace/debug only.

    The Orchestrator must not treat provider-specific reasoning fields as the
    final JSON answer, but keeping a redacted preview makes empty-content
    failures diagnosable.
    """

    try:
        if hasattr(message, "model_dump"):
            return json.dumps(message.model_dump(), ensure_ascii=False, default=str)[:1200]
    except Exception:
        pass
    return str(message)[:1200]


def _repair_payload_scaffold(payload: dict[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {
        "task": payload.get("task"),
        "stage_briefing": payload.get("stage_briefing"),
        "system_contract": payload.get("system_contract"),
        "output_contract": payload.get("output_contract"),
    }
    if payload.get("output_schema") is not None:
        compact["output_schema"] = payload.get("output_schema")
    return {key: value for key, value in compact.items() if value not in (None, "", [], {})}


def _build_reasoning_repair_messages(
    *,
    system: str,
    payload: dict[str, Any],
    reasoning_text: str,
) -> list[dict[str, str]]:
    reasoning_excerpt = str(reasoning_text or "").strip()
    if len(reasoning_excerpt) > 8000:
        reasoning_excerpt = reasoning_excerpt[:8000]
    repair_instruction = (
        "你上一轮已经完成内部分析，但没有输出最终 JSON，导致最终 content 为空。\n"
        "现在不要继续展开分析，不要复述背景，不要输出 Markdown。\n"
        "请基于下面的任务骨架和你上一轮内部分析摘录，只输出一个严格 JSON object。\n"
        "如果内部分析与任务骨架冲突，以任务骨架为准；缺字段时按任务骨架补全。\n"
        f"任务骨架:\n{json.dumps(_repair_payload_scaffold(payload), ensure_ascii=False, default=str)}\n"
        f"内部分析摘录:\n{reasoning_excerpt}"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": repair_instruction},
    ]


def _build_json_repair_messages(
    *,
    system: str,
    payload: dict[str, Any],
    raw_response: str,
    reasoning_text: str = "",
) -> list[dict[str, str]]:
    raw_excerpt = str(raw_response or "").strip()
    if len(raw_excerpt) > 8000:
        raw_excerpt = raw_excerpt[:8000]
    reasoning_excerpt = str(reasoning_text or "").strip()
    if len(reasoning_excerpt) > 4000:
        reasoning_excerpt = reasoning_excerpt[:4000]
    repair_instruction = (
        "你上一轮的最终输出没有通过 FXAlpha Orchestrator 的严格 JSON 校验。\n"
        "现在不要继续分析，不要补充解释，不要输出 Markdown，只输出一个合法 JSON object。\n"
        "必须遵守下面的任务骨架、required_fields、allowed_next_stages 和 schema_example。\n"
        "如果上一轮内容里已有接近正确的 JSON，请修正后直接输出；如果与任务骨架冲突，以任务骨架为准。\n"
        f"任务骨架:\n{json.dumps(_repair_payload_scaffold(payload), ensure_ascii=False, default=str)}\n"
        f"上一轮输出摘录:\n{raw_excerpt}"
    )
    if reasoning_excerpt:
        repair_instruction += f"\n内部分析摘录:\n{reasoning_excerpt}"
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": repair_instruction},
    ]


def _chat_completion_message(
    *,
    client: Any,
    provider_model: str,
    messages: list[dict[str, str]],
    temperature: float,
    max_tokens: int,
) -> Any:
    completion_kwargs = {
        "model": provider_model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "extra_body": {"thinking": {"type": "disabled"}},
    }
    if _deepseek_json_mode_enabled():
        completion_kwargs["response_format"] = {"type": "json_object"}
    resp = client.chat.completions.create(**completion_kwargs)
    try:
        message = resp.choices[0].message
    except Exception as exc:
        raise DeepSeekClientError("llm_response_missing_choice", raw_preview=str(resp)[:1200]) from exc
    usage = getattr(resp, "usage", None)
    if usage is not None:
        usage_payload: dict[str, Any] = {}
        try:
            dumped = usage.model_dump()
            if isinstance(dumped, dict):
                usage_payload = dumped
        except Exception:
            if isinstance(usage, dict):
                usage_payload = usage
        try:
            setattr(message, "_fxalpha_usage", usage_payload)
        except Exception:
            pass
    return message


def _auto_proxy_url() -> str | None:
    if any(os.environ.get(name) for name in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy")):
        return None
    if str(LLM_PROVIDER or "").lower() != "deepseek":
        return None
    try:
        version = open("/proc/version", "r", encoding="utf-8", errors="ignore").read().lower()
    except Exception:
        version = ""
    if "microsoft" not in version and "wsl" not in version:
        return None
    try:
        route = subprocess.check_output(["ip", "route"], text=True, timeout=1)
        gateway = next((line.split()[2] for line in route.splitlines() if line.startswith("default ")), "")
    except Exception:
        gateway = ""
    if not gateway:
        return None
    for port in (7890,):
        sock = socket.socket()
        sock.settimeout(0.25)
        try:
            sock.connect((gateway, port))
            return f"http://{gateway}:{port}"
        except Exception:
            continue
        finally:
            sock.close()
    return None


@dataclass(frozen=True)
class DeepSeekJSONClient:
    model: str = _preferred_llm_model(LLM_MODEL)
    api_key: str = LLM_API_KEY or ""
    base_url: str = LLM_BASE_URL or "https://api.deepseek.com"
    timeout: int = 600

    def available(self) -> bool:
        return bool(self.api_key)

    def model_order(self) -> tuple[str, ...]:
        return _llm_models(self.model)

    def preferred_model(self) -> str:
        ordered = self.model_order()
        if ordered:
            return ordered[0]
        return self.model or "deepseek-v4-flash"

    def complete_json(
        self,
        *,
        system: str,
        payload: dict[str, Any],
        temperature: float = 0.15,
        max_tokens: int = 1800,
    ) -> dict[str, Any]:
        if not self.api_key:
            raise DeepSeekClientError("llm_api_key_missing")
        try:
            from openai import OpenAI
        except Exception as exc:  # pragma: no cover - depends on runtime env
            raise DeepSeekClientError(f"openai_sdk_unavailable:{exc}") from exc

        original_messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)},
        ]
        last_error: Exception | None = None
        for base_url in _deepseek_base_urls(self.base_url):
            proxy_url = _auto_proxy_url()
            http_client = None
            if proxy_url:
                try:
                    import httpx

                    http_client = httpx.Client(proxy=proxy_url, timeout=self.timeout)
                except Exception:
                    http_client = None
            client = OpenAI(
                api_key=self.api_key,
                base_url=base_url,
                timeout=self.timeout,
                max_retries=0,
                http_client=http_client,
            )
            model_order = self.model_order()
            for model in model_order:
                provider_model = _provider_model_name(model)
                for attempt in range(2):
                    try:
                        message = _chat_completion_message(
                            client=client,
                            provider_model=provider_model,
                            messages=original_messages,
                            temperature=temperature if attempt == 0 else 0.0,
                            max_tokens=max_tokens,
                        )
                        usage_payload = getattr(message, "_fxalpha_usage", None)
                        text = _message_text(message)
                        reasoning_text = _message_reasoning_text(message)
                        try:
                            result = _extract_json_object(text)
                        except DeepSeekClientError as exc:
                            category = exc.category or _deepseek_error_category(exc)
                            if _is_json_repair_category(category):
                                repair_messages = (
                                    _build_reasoning_repair_messages(
                                        system=system,
                                        payload=payload,
                                        reasoning_text=reasoning_text,
                                    )
                                    if str(exc) == "empty_llm_response" and reasoning_text
                                    else _build_json_repair_messages(
                                        system=system,
                                        payload=payload,
                                        raw_response=exc.raw_preview or text or _message_debug_preview(message),
                                        reasoning_text=reasoning_text,
                                    )
                                )
                                repair_message = _chat_completion_message(
                                    client=client,
                                    provider_model=provider_model,
                                    messages=repair_messages,
                                    temperature=0.0,
                                    max_tokens=max_tokens,
                                )
                                repair_usage_payload = getattr(repair_message, "_fxalpha_usage", None)
                                repair_text = _message_text(repair_message)
                                result = _extract_json_object(repair_text)
                                if repair_usage_payload:
                                    usage_payload = repair_usage_payload
                                result["_orchestrator_llm_repaired_from_reasoning"] = bool(reasoning_text)
                                if reasoning_text:
                                    result["_orchestrator_llm_reasoning_preview"] = reasoning_text[:600]
                                result["_orchestrator_llm_repaired_from_raw"] = True
                                result["_orchestrator_llm_raw_response"] = repair_text
                                result["_orchestrator_llm_original_raw_response"] = text
                            else:
                                if not exc.raw_preview:
                                    exc.raw_preview = _message_debug_preview(message)
                                raise
                        else:
                            result["_orchestrator_llm_raw_response"] = text
                        result["_orchestrator_llm_model"] = model
                        result["_orchestrator_llm_provider_model"] = provider_model
                        result["_orchestrator_llm_model_order"] = list(model_order)
                        if isinstance(usage_payload, dict) and usage_payload:
                            result["_orchestrator_llm_usage"] = usage_payload
                        return result
                    except Exception as exc:  # pragma: no cover - network and provider dependent
                        last_error = exc
                        category = _deepseek_error_category(exc)
                        if isinstance(exc, DeepSeekClientError):
                            category = exc.category or category
                        try:
                            setattr(exc, "category", category)
                        except Exception:
                            pass
                        if not _is_transport_retry_category(category):
                            break
                        if attempt == 1:
                            break
        if isinstance(last_error, DeepSeekClientError):
            raise DeepSeekClientError(str(last_error), raw_preview=last_error.raw_preview, category=last_error.category) from last_error
        raise DeepSeekClientError(
            str(last_error or "llm_json_failed"),
            category=_deepseek_error_category(last_error or "llm_json_failed"),
        )
