from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo


MODEL_DISPLAY_NAMING_VERSION = "model_display_v1"
MODEL_DISPLAY_TIMEZONE = ZoneInfo("Asia/Shanghai")


def normalize_model_identifier(value: Any) -> str:
    text = str(value or "").strip()
    text = re.sub(r"model0703", "model", text, flags=re.IGNORECASE)
    text = re.sub(r"^m0703_", "mrun_", text, flags=re.IGNORECASE)
    text = re.sub(r"^mr0703_", "mround_", text, flags=re.IGNORECASE)
    text = re.sub(r"^ms0703_", "msession_", text, flags=re.IGNORECASE)
    text = re.sub(r"^roll0703_", "model_roll_", text, flags=re.IGNORECASE)
    return text


def feature_set_display_label(feature_set_id: Any) -> str:
    value = normalize_model_identifier(feature_set_id)
    value = re.sub(r"^fs-", "", value, flags=re.IGNORECASE)
    value = re.sub(r"^model-", "", value, flags=re.IGNORECASE)
    value = re.sub(r"[-_](?:19|20)\d{6}(?:[-_]\d{4,6})?$", "", value)
    value = re.sub(r"[-_]+", "-", value).strip("-")
    return value.upper() or "UNSPECIFIED"


def _parse_identifier_timestamp(value: Any) -> datetime | None:
    raw = normalize_model_identifier(value)
    patterns = (
        r"model_prod_.*_(\d{8})T(\d{6})",
        r"mround_(\d{8})_(\d{6})",
        r"model_roll_(\d{8})T(\d{6})",
        r"(?:^|_)(\d{8})_(\d{6})(?:_|$)",
    )
    for pattern in patterns:
        match = re.search(pattern, raw, flags=re.IGNORECASE)
        if not match:
            continue
        try:
            return datetime.strptime("".join(match.groups()), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _parse_record_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def model_display_timestamp(row: dict[str, Any]) -> str:
    parsed = (
        _parse_identifier_timestamp(row.get("model_run_id"))
        or _parse_identifier_timestamp(row.get("campaign_id"))
        or _parse_record_timestamp(row.get("completed_at"))
        or _parse_record_timestamp(row.get("created_at"))
        or _parse_record_timestamp(row.get("started_at"))
        or _parse_record_timestamp(row.get("updated_at"))
    )
    return parsed.astimezone(MODEL_DISPLAY_TIMEZONE).strftime("%Y-%m-%d %H:%M") if parsed else "时间未知"


def model_display_role(row: dict[str, Any], *, kind: str = "model") -> str:
    if kind == "rolling" or str(row.get("role") or "").lower() == "rolling_campaign":
        return "ROLLING"
    status = str(row.get("status") or "research").lower()
    return {
        "research": "研究",
        "candidate": "候选",
        "production": "生产",
        "archived": "归档",
    }.get(status, "模型")


def model_display_projection(
    row: dict[str, Any],
    *,
    kind: str = "model",
    round_no: int | None = None,
) -> dict[str, Any]:
    feature_label = feature_set_display_label(row.get("feature_set_id"))
    role_label = model_display_role(row, kind=kind)
    timestamp = model_display_timestamp(row)
    parts = [role_label, feature_label, timestamp]
    if round_no is not None:
        parts.append(f"R{int(round_no)}")
    seed = row.get("seed")
    seed_text = f"正式 Seed{int(seed)}" if seed is not None else "正式 Seed42"
    return {
        "display_name": " · ".join(parts),
        "display_subtitle": f"{seed_text} · {MODEL_DISPLAY_NAMING_VERSION}",
        "display_role": role_label,
        "display_feature_set": feature_label,
        "display_timestamp": timestamp,
        "display_round": f"R{int(round_no)}" if round_no is not None else "",
        "display_naming_version": MODEL_DISPLAY_NAMING_VERSION,
    }


def rolling_display_projection(campaign: dict[str, Any]) -> dict[str, Any]:
    row = {
        **campaign,
        "model_run_id": campaign.get("campaign_id"),
        "role": "rolling_campaign",
        "seed": 42,
    }
    return model_display_projection(row, kind="rolling")
