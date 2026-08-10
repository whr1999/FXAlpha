"""Auto-import adopted QuantGPT factors into FXalpha factor storage."""

from __future__ import annotations

import logging
import hashlib
import json
import re
import threading as _th
from typing import Any

from storage.paths import (
    FACTOR_ENABLE_WQ_SUBMIT,
    FACTOR_DEFAULT_UNIVERSE,
    get_live_factor_default_end_date,
    get_live_factor_default_start_date,
    get_live_factor_value_default_end_date,
    get_live_factor_value_default_start_date,
    get_live_st_exposure_guard_mode,
)


logger = logging.getLogger(__name__)

TAXONOMY_VERSION = "wq_data_category_v1"

STANDARD_FACTOR_CATEGORIES: tuple[str, ...] = (
    "Price Volume",
    "Fundamental",
    "Analyst",
    "Sentiment",
    "Options",
    "Model",
    "Insider Transactions",
    "Short Interest",
    "Ownership",
    "Composite",
    "Other",
)

FACTOR_CATEGORY_TAXONOMY: dict[str, dict[str, Any]] = {
    "Price Volume": {
        "wq_category": "Price Volume",
        "description": "Price, volume, turnover, volatility, liquidity, and daily microstructure proxies.",
        "fields": ["open", "high", "low", "close", "volume", "amount", "turnover_rate", "pct_change", "amp", "vwap", "total_mv", "float_mv"],
    },
    "Fundamental": {
        "wq_category": "Fundamental",
        "description": "Valuation, profitability, balance-sheet, and financial-quality variables.",
        "fields": ["pe", "pb", "roe", "roa", "eps", "net_profit", "net_asset_ps", "tot_equity", "total_assets"],
    },
    "Analyst": {"wq_category": "Analyst", "description": "Analyst estimate and rating data. Not enabled in the current A-share daily data layer.", "fields": [], "available": False},
    "Sentiment": {"wq_category": "Sentiment", "description": "News, text, and sentiment alternative data. Not enabled in the current data layer.", "fields": [], "available": False},
    "Options": {"wq_category": "Options", "description": "Option-implied volatility and derivatives data. Not enabled in the current stock daily data layer.", "fields": [], "available": False},
    "Model": {"wq_category": "Model", "description": "External model predictions, risk model exposures, or ensemble outputs.", "fields": [], "available": False},
    "Insider Transactions": {"wq_category": "Insider Transactions", "description": "Insider transaction data. Not enabled in the current A-share daily data layer.", "fields": [], "available": False},
    "Short Interest": {
        "wq_category": "Short Interest",
        "description": "Margin financing, securities lending, and short-interest proxies.",
        "fields": ["borrow_money_bal", "purch_borrow_money", "sec_lending_bal", "margin_trade_bal"],
    },
    "Ownership": {
        "wq_category": "Ownership",
        "description": "FXAlpha A-share extension for holder count and ownership concentration proxies.",
        "fields": ["holder_num"],
        "fxalpha_extension": True,
    },
    "Composite": {"wq_category": "Composite", "description": "A factor combining two or more data-source categories.", "fields": []},
    "Other": {"wq_category": "Other", "description": "A factor that cannot be mapped to a stable data-source category.", "fields": []},
}

_RUN_LABEL_MARKERS = ("codex", "mcp", "prompt", "round", "mining", "repair", "official", "session")

_GENERIC_FACTOR_NAMES = {
    "amount",
    "close",
    "open",
    "high",
    "low",
    "volume",
    "vol",
    "vwap",
    "turnover",
    "turnoverrate",
    "pctchange",
    "ret",
    "amp",
    "pb",
    "pe",
    "roe",
    "roa",
    "eps",
    "holder",
    "holdernum",
    "margin",
    "margintrade",
    "shortloan",
    "shortbalance",
    "floatcap",
    "mcap",
}

_LEGACY_NAME_SUFFIXES = (
    "_minimal_proxy",
    "_regime_switch_minimal",
    "_exact",
    "_rebuild",
)

_CATEGORY_ALIASES = {
    "price_volume": "Price Volume",
    "price-volume": "Price Volume",
    "price volume": "Price Volume",
    "microstructure": "Price Volume",
    "liquidity": "Price Volume",
    "量价": "Price Volume",
    "微观结构": "Price Volume",
    "fundamental": "Fundamental",
    "fundamentals": "Fundamental",
    "value": "Fundamental",
    "quality": "Fundamental",
    "基本面": "Fundamental",
    "估值": "Fundamental",
    "质量": "Fundamental",
    "analyst": "Analyst",
    "分析师": "Analyst",
    "sentiment": "Sentiment",
    "情绪": "Sentiment",
    "options": "Options",
    "option": "Options",
    "期权": "Options",
    "model": "Model",
    "模型": "Model",
    "insider": "Insider Transactions",
    "insider transactions": "Insider Transactions",
    "内部交易": "Insider Transactions",
    "short interest": "Short Interest",
    "margin": "Short Interest",
    "short": "Short Interest",
    "融资融券": "Short Interest",
    "holder": "Ownership",
    "shareholder": "Ownership",
    "ownership": "Ownership",
    "筹码": "Ownership",
    "股东": "Ownership",
    "composite": "Composite",
    "multi": "Composite",
    "复合": "Composite",
    "other": "Other",
    "其他": "Other",
}


def _has_any(text: str, keywords: tuple[str, ...]) -> bool:
    return any(keyword in text for keyword in keywords)


def _extract_fields(expression: str | None) -> set[str]:
    expr = str(expression or "").lower()
    operators = {
        "rank", "zscore", "log", "abs", "scale", "where", "max", "min",
        "sign", "power", "ema", "sma", "wma", "rsi", "macd", "obv", "atr",
        "boll_upper", "boll_lower", "boll_mid", "ts_mean", "ts_std", "ts_max",
        "ts_min", "ts_sum", "ts_shift", "ts_delta", "ts_rank", "ts_argmax",
        "ts_argmin", "ts_corr", "ts_cov", "decay_linear", "product",
    }
    return {
        token
        for token in re.findall(r"\b[a-zA-Z_][A-Za-z0-9_]*\b", expr)
        if token not in operators and not token.startswith("adv")
    }


def _field_categories(expression: str | None) -> dict[str, list[str]]:
    expr = str(expression or "").lower().replace(" ", "")
    fields = _extract_fields(expr)
    categories: dict[str, list[str]] = {}

    def add(category: str, matched: list[str]) -> None:
        if matched:
            categories.setdefault(category, [])
            categories[category].extend(sorted(set(matched)))

    add("Price Volume", [field for field in fields if field in {"open", "high", "low", "close", "volume", "amount", "turnover_rate", "pct_change", "amp", "vwap", "total_mv", "float_mv", "pre_close"}])
    if _has_any(expr, ("ts_corr(close,volume", "ts_corr(close,amount", "obv", "atr", "boll", "macd", "rsi")):
        categories.setdefault("Price Volume", []).append("technical_indicator")
    add("Fundamental", [field for field in fields if field in {"pe", "pb", "roe", "roa", "eps", "bps", "net_profit", "net_asset_ps", "tot_equity", "total_assets", "tot_share", "float_a_share"}])
    add("Short Interest", [field for field in fields if field in {"borrow_money_bal", "purch_borrow_money", "sec_lending_bal", "margin_trade_bal"}])
    add("Ownership", [field for field in fields if field in {"holder_num"}])
    return {key: sorted(set(value)) for key, value in categories.items()}


def _normalize_category_alias(category: str) -> str:
    raw = str(category or "").strip()
    lower = raw.lower()
    if raw in STANDARD_FACTOR_CATEGORIES:
        return raw
    if not raw or any(marker in lower for marker in _RUN_LABEL_MARKERS):
        return ""
    return _CATEGORY_ALIASES.get(lower) or next(
        (target for key, target in _CATEGORY_ALIASES.items() if key in lower or key in raw),
        "",
    )


def generate_factor_name(expression: str | None, category_info: dict | None = None) -> str:
    """Generate a compact but distinctive English factor name.

    Names should remain readable in the GUI while avoiding generic collisions.
    Prefer mechanism + operator + window tokens such as `HolderDown60`,
    `ROEUp60`, `CloseVolCorr10`, and `LowAmpStd10`.
    """
    expr = str(expression or "").lower().replace(" ", "")
    words: list[str] = []

    field_labels = {
        "holder_num": "Holder",
        "borrow_money_bal": "Margin",
        "purch_borrow_money": "MarginBuy",
        "sec_lending_bal": "ShortLoan",
        "margin_trade_bal": "MarginTrade",
        "turnover_rate": "Turnover",
        "pct_change": "Ret",
        "net_profit": "Profit",
        "total_assets": "Assets",
        "net_asset_ps": "BVPS",
        "tot_equity": "Equity",
        "total_mv": "MCap",
        "float_mv": "FloatCap",
        "volume": "Vol",
        "amount": "Amount",
        "close": "Close",
        "open": "Open",
        "high": "High",
        "low": "Low",
        "roe": "ROE",
        "roa": "ROA",
        "eps": "EPS",
        "pb": "PB",
        "pe": "PE",
        "amp": "Amp",
    }

    def label(field: str) -> str:
        return field_labels.get(field, "".join(part.capitalize() for part in field.split("_")))

    def add(word: str) -> None:
        clean = re.sub(r"[^A-Za-z0-9]", "", str(word or ""))
        if clean and clean not in words and len(words) < 4:
            words.append(clean)

    def is_negative(start: int) -> bool:
        prefix = expr[max(0, start - 8):start]
        return prefix.endswith("-") or prefix.endswith("(-") or "-1*" in prefix or "-1.0*" in prefix

    def window_suffix(needle: str, default: str = "") -> str:
        escaped = re.escape(needle)
        for op, suffix in (("ts_mean", "Mean"), ("ts_std", "Std"), ("ts_rank", "Rank"), ("ts_delta", "Delta")):
            m = re.search(rf"{op}\([^,]*{escaped}[^,]*,(\d+)\)", expr)
            if m:
                return f"{suffix}{m.group(1)}"
        return default

    pattern_tokens = (
        ("cost_85pct-cost_15pct", "CostSpread"),
        ("cost_15pct-cost_85pct", "CostSpread"),
        ("cost_85pct/cost_15pct", "CostRatio"),
        ("cost_15pct/cost_85pct", "CostRatio"),
        ("amount/free_share", "AmountFloat"),
        ("amount/float_a_share", "AmountFloat"),
        ("lg_net_amount-sm_net_amount", "LargeSmallFlow"),
        ("lg_net_vol-sm_net_vol", "LargeSmallVol"),
        ("net_mf_amount", "NetMfAmount"),
        ("low-0.9*close", "LowSupport"),
        ("close-low", "LowBounce"),
        ("high-close", "HighFade"),
        ("close-vwap", "VWAPGap"),
        ("float_mv", "SmallFloat" if "-float_mv" in expr or "rank(-float_mv" in expr else "FloatCap"),
        ("total_mv", "SmallCap" if "-total_mv" in expr or "rank(-total_mv" in expr else "MCap"),
    )
    for needle, token in pattern_tokens:
        if needle in expr:
            add(f"{token}{window_suffix(needle)}")

    matches: list[tuple[int, str]] = []
    for m in re.finditer(r"ts_corr\((?P<a>[a-z_]+),(?P<b>[a-z_]+),(?P<w>\d+)\)", expr):
        matches.append((m.start(), f"{label(m.group('a'))}{label(m.group('b'))}Corr{m.group('w')}"))
    for m in re.finditer(r"ts_delta\((?P<field>[a-z_]+),(?P<w>\d+)\)", expr):
        base = label(m.group("field"))
        if m.group("field") == "close":
            suffix = "Rev" if is_negative(m.start()) else "Mom"
        else:
            suffix = "Down" if is_negative(m.start()) else "Up"
        matches.append((m.start(), f"{base}{suffix}{m.group('w')}"))
    for m in re.finditer(r"ts_std\((?P<field>[a-z_]+),(?P<w>\d+)\)", expr):
        prefix = "Low" if is_negative(m.start()) else ""
        matches.append((m.start(), f"{prefix}{label(m.group('field'))}Std{m.group('w')}"))
    for m in re.finditer(r"ts_rank\((?P<field>[a-z_]+),(?P<w>\d+)\)", expr):
        prefix = "Low" if is_negative(m.start()) else ""
        matches.append((m.start(), f"{prefix}{label(m.group('field'))}Rank{m.group('w')}"))
    for m in re.finditer(r"ts_mean\((?P<field>[a-z_]+),(?P<w>\d+)\)", expr):
        matches.append((m.start(), f"{label(m.group('field'))}Mean{m.group('w')}"))

    for _, token in sorted(matches, key=lambda item: item[0]):
        add(token)

    raw_field_tokens = (
        ("-pb", "LowPB", "PB"),
        ("-pe", "LowPE", "PE"),
        ("roe", "ROE", "ROE"),
        ("roa", "ROA", "ROA"),
        ("eps", "EPS", "EPS"),
        ("holder_num", "Holder", "Holder"),
        ("turnover_rate", "Turnover", "Turnover"),
        ("volume", "Vol", "Vol"),
        ("amount", "Amount", "Amount"),
    )
    for needle, token, family in raw_field_tokens:
        if len(words) >= 4:
            break
        if needle in expr and not any(family in word for word in words):
            if token.lower() in _GENERIC_FACTOR_NAMES:
                if "rank(" in expr:
                    token = f"{token}Rank"
                elif "ts_mean(" in expr:
                    token = f"{token}Mean"
                elif "ts_delta(" in expr:
                    token = f"{token}Delta"
                else:
                    token = f"{token}Signal"
            add(token)

    if not words:
        category = str((category_info or {}).get("primary_category") or "")
        fallback = {
            "Price Volume": ["PriceVol"],
            "Fundamental": ["Fundamental"],
            "Short Interest": ["ShortInterest"],
            "Ownership": ["Ownership"],
            "Composite": ["Composite"],
        }.get(category, ["QuantFactor"])
        words.extend(fallback[:4])
    if len(words) == 1 and words[0].lower() in _GENERIC_FACTOR_NAMES:
        if "rank(" in expr:
            words[0] = f"{words[0]}Rank"
        elif "ts_mean(" in expr:
            words[0] = f"{words[0]}Mean"
        elif "ts_delta(" in expr:
            words[0] = f"{words[0]}Delta"
        else:
            words[0] = f"{words[0]}Signal"
    return " ".join(words[:4])


def factor_name_quality_reason(name: str | None, expression: str | None = None) -> str:
    """Return a rejection reason for names that are too generic for registry use."""
    raw = " ".join(str(name or "").strip().split())
    if not raw:
        return "missing"
    lowered = raw.lower()
    compact = re.sub(r"[^a-z0-9]", "", lowered)
    if compact in _GENERIC_FACTOR_NAMES:
        return "raw_field_name"
    generic_roots = "|".join(sorted(re.escape(name) for name in _GENERIC_FACTOR_NAMES))
    if re.fullmatch(rf"(?:{generic_roots})(?:mean|rank|delta|std|signal)?\d*", compact):
        return "raw_field_operator_name"
    for token in re.split(r"\s+", raw):
        if re.sub(r"[^a-z0-9]", "", token.lower()) in _GENERIC_FACTOR_NAMES:
            return "raw_field_component"
    if re.match(r"^r\d{3,5}[a-z_]*\d*[a-z]*$", lowered):
        return "run_label_name"
    if lowered.endswith(_LEGACY_NAME_SUFFIXES):
        return "legacy_suffix"
    if any(marker in lowered for marker in _RUN_LABEL_MARKERS):
        return "run_context_name"
    if len(compact) < 5:
        return "too_short"
    return ""


def canonical_factor_name(
    expression: str | None,
    category_info: dict | None = None,
    proposed_name: str | None = None,
) -> tuple[str, str]:
    """Choose a registry-safe display name and report whether it was repaired."""
    proposed = " ".join(str(proposed_name or "").strip().split())[:80]
    if proposed and not factor_name_quality_reason(proposed, expression):
        return proposed, "provided"
    generated = " ".join(generate_factor_name(expression, category_info).split())[:80]
    if factor_name_quality_reason(generated, expression):
        category = str((category_info or {}).get("primary_category") or "Quant")
        suffix = {
            "Price Volume": "PriceVolumeSignal",
            "Fundamental": "FundamentalSignal",
            "Short Interest": "ShortInterestSignal",
            "Ownership": "OwnershipSignal",
            "Composite": "CompositeSignal",
        }.get(category, "QuantSignal")
        generated = f"{generated} {suffix}" if generated else suffix
    status = "repaired" if proposed else "generated"
    return generated[:80], status


def classify_factor_expression(expression: str | None, category: str | None = None) -> dict[str, Any]:
    """Classify a factor using WorldQuant data-source categories."""
    raw = str(category or "").strip()
    alias = _normalize_category_alias(raw)
    matched = _field_categories(expression)
    data_categories = sorted(matched.keys())

    if raw in STANDARD_FACTOR_CATEGORIES:
        primary = raw
        rationale = "caller supplied a standard WorldQuant category"
    elif len(data_categories) >= 2:
        primary = "Composite"
        rationale = "expression uses multiple WorldQuant data-source categories"
    elif len(data_categories) == 1:
        primary = data_categories[0]
        rationale = "expression fields map to one WorldQuant data-source category"
    elif alias:
        primary = alias
        rationale = "caller category alias was normalized"
    else:
        primary = "Other"
        rationale = "no supported field mapping or stable category alias was found"

    category_tags = sorted(set([primary, *data_categories]))
    info = {
        "taxonomy_version": TAXONOMY_VERSION,
        "primary_category": primary,
        "category_tags": category_tags,
        "matched_fields": matched,
        "raw_category": raw,
        "rationale": rationale,
    }
    info["suggested_factor_name"] = generate_factor_name(expression, info)
    return info


def normalize_factor_category(category: str | None, expression: str | None = None) -> str:
    return str(classify_factor_expression(expression, category).get("primary_category") or "Other")


def _as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _first_number(*values: Any) -> float | None:
    for value in values:
        if value is None:
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number == number:
            return number
    return None


def _extract_backtest_summary(candidate: dict) -> dict:
    candidate = _as_dict(candidate)
    direct = _as_dict(candidate.get("backtest_summary"))
    if direct:
        return direct
    backtest = _as_dict(candidate.get("backtest"))
    backtest_summary = _as_dict(backtest.get("backtest_summary"))
    if backtest_summary:
        return backtest_summary
    backtest_result = _as_dict(candidate.get("backtest_result"))
    backtest_result_summary = _as_dict(backtest_result.get("backtest_summary"))
    if backtest_result_summary:
        return backtest_result_summary
    result = _as_dict(candidate.get("result"))
    result_summary = _as_dict(result.get("backtest_summary"))
    if result_summary:
        return result_summary
    metrics = _as_dict(candidate.get("metrics"))
    metrics_summary = _as_dict(metrics.get("backtest_summary"))
    if metrics_summary:
        return metrics_summary
    return metrics


def _selected_group_flipped_low_side(candidate: dict) -> bool | None:
    candidate = _as_dict(candidate)
    for value in (
        candidate.get("flipped"),
        _as_dict(candidate.get("key_metrics")).get("flipped"),
        _as_dict(candidate.get("best_long_only_group_metrics")).get("selected_group_is_flipped_low_side"),
        _as_dict(candidate.get("backtest_summary")).get("flipped"),
        _as_dict(candidate.get("params")).get("flipped"),
    ):
        if value is None:
            continue
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "y", "low", "low_factor_values"}
        return bool(value)
    return None


def _compact_long_only_direction_metadata(candidate: dict) -> dict:
    flipped = _selected_group_flipped_low_side(candidate)
    best_group = _as_dict(_as_dict(candidate).get("best_long_only_group_metrics"))
    if flipped is None and not best_group:
        return {}
    payload = {
        "selected_group_is_flipped_low_side": bool(flipped) if flipped is not None else None,
        "long_only_side": ("low_factor_values" if flipped else "high_factor_values") if flipped is not None else None,
        "source": "quantgpt_backtest_best_long_only_group",
    }
    for key in ("annual_return", "sharpe", "max_drawdown", "turnover"):
        if best_group.get(key) is not None:
            payload[key] = best_group.get(key)
    return {key: value for key, value in payload.items() if value is not None}


def _complete_metrics(candidate: dict) -> dict:
    backtest_summary = _extract_backtest_summary(candidate)
    gate = _as_dict(candidate.get("gate_result"))
    deep_validation = _as_dict(candidate.get("deep_validation"))
    metrics = {
        "ic_mean": _first_number(gate.get("ic"), backtest_summary.get("rank_ic_mean"), backtest_summary.get("ic_mean")),
        "icir": _first_number(gate.get("ir"), backtest_summary.get("rank_ic_ir"), backtest_summary.get("ic_ir"), backtest_summary.get("icir")),
        "rank_ic": _first_number(gate.get("rank_ic"), backtest_summary.get("rank_ic_mean"), backtest_summary.get("rank_ic")),
        "rank_icir": _first_number(gate.get("rank_ir"), backtest_summary.get("rank_ic_ir"), backtest_summary.get("rank_icir")),
        "annual_return": _first_number(backtest_summary.get("annual_return"), backtest_summary.get("annualized_return")),
        "sharpe": _first_number(gate.get("sharpe"), backtest_summary.get("sharpe")),
        "max_drawdown": _first_number(backtest_summary.get("max_drawdown")),
        "turnover": _first_number(backtest_summary.get("turnover")),
        "quick_score": _first_number(candidate.get("quick_score"), candidate.get("score"), gate.get("quick_score")),
        "deep_score": _first_number(candidate.get("deep_score"), deep_validation.get("deep_score"), gate.get("deep_score")),
    }
    return {key: value for key, value in metrics.items() if value is not None}


def _compact_quality_metadata(candidate: dict, metrics: dict, wq_expression: str | None) -> dict:
    backtest_summary = _extract_backtest_summary(candidate)
    long_only_direction = _compact_long_only_direction_metadata(candidate)
    screening = _as_dict(candidate.get("screening"))
    deep_validation = _as_dict(candidate.get("deep_validation"))
    novelty_guard = (
        _as_dict(candidate.get("novelty_guard"))
        or _as_dict(screening.get("novelty_guard"))
        or _as_dict(deep_validation.get("novelty_correlation"))
    )
    st_exposure_guard = (
        _as_dict(candidate.get("st_exposure_guard"))
        or _as_dict(screening.get("st_exposure_guard"))
        or _as_dict(deep_validation.get("st_exposure_guard"))
    )
    combined_guard = (
        _as_dict(candidate.get("combined_guard"))
        or _as_dict(screening.get("combined_guard"))
        or _as_dict(deep_validation.get("combined_guard"))
    )
    anti_overfit = (
        _as_dict(candidate.get("anti_overfit"))
        or _as_dict(candidate.get("anti_overfit_summary"))
        or _as_dict(deep_validation.get("anti_overfit"))
        or _as_dict(deep_validation.get("anti_overfit_summary"))
        or _as_dict(_as_dict(candidate.get("backtest")).get("anti_overfit"))
        or _as_dict(_as_dict(candidate.get("backtest_result")).get("anti_overfit"))
    )
    adversarial = (
        _as_dict(candidate.get("adversarial_validation"))
        or _as_dict(candidate.get("adversarial"))
        or _as_dict(deep_validation.get("adversarial_validation"))
    )
    rolling_validation = (
        _as_dict(candidate.get("rolling_validation"))
        or _as_dict(candidate.get("rolling_validation_summary"))
        or _as_dict(deep_validation.get("rolling_validation"))
    )
    economic_thesis = _as_dict(candidate.get("economic_thesis"))
    hypothesis = candidate.get("hypothesis")
    metadata = {
        "evidence_schema_version": "fxalpha_evidence_v1",
        "research_provenance": {
            key: candidate.get(key)
            for key in (
                "run_id",
                "round_id",
                "candidate_id",
                "trajectory_id",
                "factor_map_id",
                "factor_map_audit_id",
                "matched_region_uid",
            )
            if candidate.get(key) not in (None, "")
        },
        "wq_expression": wq_expression,
        "economic_thesis": economic_thesis,
        "hypothesis": hypothesis,
        "metrics": metrics,
        "backtest_summary": backtest_summary,
        "long_only_direction": long_only_direction,
        "selected_group_is_flipped_low_side": long_only_direction.get("selected_group_is_flipped_low_side"),
        "gate_result": _as_dict(candidate.get("gate_result")),
        "deep_validation": deep_validation,
        "novelty_guard": novelty_guard,
        "st_exposure_guard": st_exposure_guard,
        "combined_guard": combined_guard,
        "risk_tags": list(candidate.get("risk_tags") or []),
        "anti_overfit": anti_overfit,
        "anti_overfit_summary": anti_overfit,
        "adversarial_validation": adversarial,
        "rolling_validation": rolling_validation,
        "persistence_diagnostic": _as_dict(candidate.get("persistence_diagnostic")) or _as_dict(candidate.get("autocorrelation")),
        "screening": screening,
        "holding_period_days": _first_number(
            candidate.get("holding_period_days"),
            candidate.get("holding_period"),
            _as_dict(candidate.get("gate_result")).get("holding_period_days"),
        ),
    }
    return {key: value for key, value in metadata.items() if value not in ({}, None, "")}


def _quality_block_reason(candidate: dict, *, force_import: bool = False) -> str:
    if force_import:
        return ""
    screening = candidate.get("screening") or {}
    gate = _as_dict(candidate.get("gate_result"))
    if not gate:
        return "missing_quality_gate"
    if gate.get("passed") is not True:
        return f"quality_gate_blocked:{gate.get('reason') or 'rejected'}"
    deep_validation = _as_dict(candidate.get("deep_validation"))
    deep_score = _first_number(
        deep_validation.get("deep_score"),
        gate.get("deep_score"),
        candidate.get("deep_score"),
    )
    gate_score = _first_number(gate.get("score"))
    if gate_score is None or deep_score is None:
        return "missing_deep_score"
    if abs(float(gate_score) - float(deep_score)) > 1e-6:
        return "inconsistent_gate_score"
    novelty = candidate.get("novelty_guard") or screening.get("novelty_guard") or deep_validation.get("novelty_correlation") or {}
    if not novelty:
        return "missing_novelty_guard"
    if novelty.get("allowed") is not True:
        return f"novelty_blocked:{novelty.get('reason') or 'low_information_gain'}"
    st_guard = candidate.get("st_exposure_guard") or screening.get("st_exposure_guard") or deep_validation.get("st_exposure_guard") or {}
    st_mode = _candidate_st_exposure_mode(st_guard)
    if not st_guard and st_mode == "hard":
        return "missing_st_exposure_guard"
    if st_guard and st_guard.get("passed") is not True and st_mode == "hard":
        return f"st_exposure_blocked:{st_guard.get('reason') or 'st_exposure_veto'}"
    combined_guard = candidate.get("combined_guard") or screening.get("combined_guard") or deep_validation.get("combined_guard") or {}
    if combined_guard and combined_guard.get("allowed") is not True and st_mode == "hard":
        return f"combined_gate_blocked:{combined_guard.get('reason') or 'not_allowed'}"
    holding_period_days = _first_number(
        candidate.get("holding_period_days"),
        candidate.get("holding_period"),
        gate.get("holding_period_days"),
    )
    if holding_period_days is None or int(holding_period_days) <= 0:
        return "missing_holding_period_days"
    backtest_summary = _extract_backtest_summary(candidate)
    if not backtest_summary:
        return "missing_backtest_summary"
    deep_validation = _as_dict(candidate.get("deep_validation"))
    if not deep_validation:
        return "missing_deep_validation"
    score_parts = _as_dict(deep_validation.get("score_parts"))
    component_scores = _as_dict(score_parts.get("component_scores"))
    missing_components = [
        key for key in ("quick_core", "anti_overfit", "rolling", "adversarial")
        if component_scores.get(key) is None
    ]
    if missing_components:
        return "missing_deep_component_scores:" + ",".join(missing_components)
    rolling_validation = (
        _as_dict(candidate.get("rolling_validation"))
        or _as_dict(screening.get("rolling_validation"))
        or _as_dict(deep_validation.get("rolling_validation"))
    )
    if not rolling_validation:
        return "missing_rolling_validation"
    rolling_score = _first_number(
        rolling_validation.get("score"),
        _as_dict(rolling_validation.get("summary")).get("score"),
    )
    if rolling_score is None:
        return "missing_rolling_score"
    metrics = _complete_metrics(candidate)
    required_metrics = ("ic_mean", "icir", "rank_ic", "sharpe", "max_drawdown", "turnover")
    missing_metrics = [key for key in required_metrics if metrics.get(key) is None]
    if missing_metrics:
        return "missing_registry_metrics:" + ",".join(missing_metrics)
    return ""


def _candidate_st_exposure_mode(st_guard: dict) -> str:
    mode = str((st_guard or {}).get("mode") or "").strip().lower()
    if mode in {"advisory", "diagnostic", "tag", "tag_only", "label"}:
        return "advisory"
    if mode in {"hard", "strict", "block", "blocking"}:
        return "hard"
    return get_live_st_exposure_guard_mode()


def _active_data_columns(registry: Any) -> set[str]:
    """Return data columns currently used by active factors."""
    columns: set[str] = set()
    try:
        active = registry.list_active(min_icir=-1e9)
    except Exception:
        active = []
    for item in active or []:
        metadata = item.get("metadata") if isinstance(item, dict) else {}
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except Exception:
                metadata = {}
        if not isinstance(metadata, dict):
            continue
        data_column = str(metadata.get("data_column") or "").strip()
        if data_column:
            columns.add(data_column)
    return columns


def _unique_safe_factor_column(base_prefix: str, expression: str, idx: int, used_columns: set[str]) -> str:
    """Create a <=40 char factor column name without colliding with active factors."""
    clean_prefix = "".join(ch if ch.isalnum() else "_" for ch in str(base_prefix or "QGF_Factor")).strip("_") or "QGF_Factor"
    candidate = f"{clean_prefix[:37]}_{idx:02d}"[:40]
    if candidate not in used_columns:
        used_columns.add(candidate)
        return candidate
    digest = hashlib.sha1(str(expression or "").encode("utf-8")).hexdigest()[:8]
    for attempt in range(100):
        suffix = f"_{digest}_{idx:02d}" if attempt == 0 else f"_{digest}_{idx:02d}_{attempt:02d}"
        prefix_len = max(1, 40 - len(suffix))
        candidate = f"{clean_prefix[:prefix_len]}{suffix}"[:40]
        if candidate not in used_columns:
            used_columns.add(candidate)
            return candidate
    raise RuntimeError(f"unable to create unique factor data column for {clean_prefix[:40]}")


def import_factors(
    adopted: list[dict],
    *,
    universe: str = FACTOR_DEFAULT_UNIVERSE,
    category: str = "",
    start_date: str | None = None,
    end_date: str | None = None,
    selection_start_date: str | None = None,
    selection_end_date: str | None = None,
    **kwargs,
) -> dict:
    from domain.factor_research.factor_compute import (
        FACTOR_COMPUTE_SEMANTICS_VERSION,
        audit_factor_value_coverage,
        compute_factor,
        save_factor_frame,
    )
    from storage.factor_registry import FactorRegistry

    registry = FactorRegistry()
    used_data_columns = _active_data_columns(registry)
    errors: list[str] = []
    details: list[dict] = []
    imported = 0
    skipped = 0
    start_date = start_date or get_live_factor_value_default_start_date()
    end_date = end_date or get_live_factor_value_default_end_date()
    selection_start_date = selection_start_date or get_live_factor_default_start_date()
    selection_end_date = selection_end_date or get_live_factor_default_end_date()
    submit_wq = bool(kwargs.get("submit_wq", FACTOR_ENABLE_WQ_SUBMIT))
    force_import = bool(kwargs.get("force_import") or kwargs.get("force"))

    if not adopted:
        return {"imported": 0, "skipped": 0, "errors": [], "details": [], "submit_wq": submit_wq}

    for idx, candidate in enumerate(adopted):
        expression = candidate.get("expression", "")
        if not expression:
            errors.append("empty expression in candidate")
            skipped += 1
            continue

        quality_block = _quality_block_reason(candidate, force_import=force_import)
        if quality_block:
            logger.warning("[auto-import] Quality guard blocked %s: %s", expression[:60], quality_block)
            errors.append(f"quality guard blocked {expression[:30]}: {quality_block}")
            skipped += 1
            details.append(
                {
                    "expression": expression[:80],
                    "status": "skipped_quality_guard",
                    "reason": quality_block,
                    "gate_result": candidate.get("gate_result", {}),
                    "screening": candidate.get("screening", {}),
                    "novelty_guard": candidate.get("novelty_guard") or (candidate.get("screening") or {}).get("novelty_guard") or {},
                }
            )
            continue

        existing = registry.get_active_by_expression(expression)
        if existing:
            factor_id = existing.get("factor_id", "")
            logger.warning("[auto-import] Duplicate active expression skipped: %s -> %s", expression[:60], factor_id)
            errors.append(f"duplicate active expression skipped for {expression[:30]}: {factor_id}")
            skipped += 1
            details.append(
                {
                    "factor_id": factor_id,
                    "expression": expression[:60],
                    "status": "skipped_duplicate_active",
                }
            )
            continue

        backtest_summary = _extract_backtest_summary(candidate)
        gate = candidate.get("gate_result", {})
        holding_period_days = int(
            _first_number(
                candidate.get("holding_period_days"),
                candidate.get("holding_period"),
                gate.get("holding_period_days"),
            ) or 5
        )
        category_info = classify_factor_expression(expression, category)
        normalized_category = str(category_info.get("primary_category") or "Other")
        proposed_factor_name = (
            candidate.get("factor_name")
            or candidate.get("name")
            or candidate.get("factor_name_hint")
            or (candidate.get("metadata") or {}).get("factor_name")
            or (candidate.get("metadata") or {}).get("factor_name_hint")
            or category_info.get("suggested_factor_name")
            or generate_factor_name(expression, category_info)
        )
        factor_name, factor_name_status = canonical_factor_name(
            expression,
            category_info,
            proposed_name=str(proposed_factor_name or ""),
        )
        proposed_factor_name = " ".join(str(proposed_factor_name or "").strip().split())[:80]
        factor_name_repair_reason = factor_name_quality_reason(proposed_factor_name, expression) if proposed_factor_name else ""
        factor_name_repaired = bool(proposed_factor_name and proposed_factor_name != factor_name)
        safe_prefix = ("QGF_" + "".join(ch if ch.isalnum() else "_" for ch in factor_name)).strip("_")
        # compute_and_save stores the DataFrame column using at most 40 chars.
        # Keep the suffix inside that limit so registry metadata matches parquet.
        safe_name = _unique_safe_factor_column(safe_prefix, expression, idx, used_data_columns)

        wq_expression = (
            candidate.get("wq_expression")
            or (candidate.get("metadata") or {}).get("wq_expression")
            or (candidate.get("result") or {}).get("wq_expression")
        )
        if not wq_expression and submit_wq:
            try:
                from domain.factor_research.wq_expression import generate_wq_expression

                wq_expression = generate_wq_expression(
                    expression,
                    direction=str(kwargs.get("direction", "") or ""),
                    context={
                        "gate_result": gate,
                        "backtest_summary": backtest_summary,
                        "autocorrelation": candidate.get("autocorrelation") or {},
                    },
                )
            except Exception as exc:
                logger.warning("[auto-import] WQ expression generation failed for %s: %s", safe_name[:40], exc)
                wq_expression = None
        metrics = _complete_metrics(candidate)

        logger.info("[auto-import] Computing %s", safe_name)
        try:
            factor_values = compute_factor(
                expression,
                start_date=start_date,
                end_date=end_date,
            )
        except Exception as exc:
            logger.error("[auto-import] Compute failed for %s: %s", safe_name[:40], exc)
            errors.append(f"compute failed for {expression[:30]}: {exc}")
            skipped += 1
            continue

        if factor_values.empty:
            logger.warning("[auto-import] No values for %s", safe_name[:40])
            errors.append(f"no values computed for {expression[:30]}")
            skipped += 1
            details.append(
                {
                    "name": factor_name,
                    "data_column": safe_name,
                    "expression": expression[:80],
                    "status": "skipped_value_coverage",
                    "reason": "no_factor_values",
                    "coverage_audit": audit_factor_value_coverage(factor_values, start_date, end_date),
                }
            )
            continue

        coverage_audit = audit_factor_value_coverage(factor_values, start_date, end_date)
        if coverage_audit.get("passed") is not True:
            reason = coverage_audit.get("reason") or "value_coverage_failed"
            logger.warning("[auto-import] Value coverage blocked %s: %s", safe_name[:40], reason)
            errors.append(f"value coverage blocked {expression[:30]}: {reason}")
            skipped += 1
            details.append(
                {
                    "name": factor_name,
                    "data_column": safe_name,
                    "expression": expression[:80],
                    "status": "skipped_value_coverage",
                    "reason": reason,
                    "missing_dates": coverage_audit.get("missing_dates", []),
                    "min_daily_valid": coverage_audit.get("min_daily_valid"),
                    "coverage_audit": coverage_audit,
                }
            )
            continue

        try:
            parquet_path = save_factor_frame(expression, safe_name, factor_values)
        except Exception as exc:
            logger.error("[auto-import] Save failed for %s: %s", safe_name[:40], exc)
            errors.append(f"save failed for {expression[:30]}: {exc}")
            skipped += 1
            continue

        if not parquet_path:
            logger.warning("[auto-import] No parquet written for %s", safe_name[:40])
            errors.append(f"no parquet written for {expression[:30]}")
            skipped += 1
            continue

        category_info = classify_factor_expression(expression, category)
        extra_metadata = _compact_quality_metadata(candidate, metrics, wq_expression)
        extra_metadata["selection_start_date"] = selection_start_date
        extra_metadata["selection_end_date"] = selection_end_date
        extra_metadata["value_start_date"] = start_date
        extra_metadata["value_end_date"] = end_date
        extra_metadata["universe"] = universe
        if universe in {"tradable_non_st", "all_market_non_st"}:
            extra_metadata["static_non_st_universe_filter"] = {
                "mode": "fixed_baseline",
                "baseline_date": "2026-06-01",
                "fields": ["list_status", "st_status"],
                "note": "Factor cross-sections use the non-ST membership fixed at the baseline date; security_name is not used for ST membership.",
            }
        extra_metadata["value_coverage_audit"] = coverage_audit
        extra_metadata["selection_bias_control"] = {
            "rule": "production_factor_selection_uses_latest_completed_as_of_window",
            "note": (
                "Production admission prioritizes current factor efficacy through the governed as-of date. "
                "Rolling v2 is recency/stability evidence, not a clean out-of-sample model backtest; "
                "model research must label clean-window and production-latest evaluations separately."
            ),
            "rolling_score_policy_version": "rolling_ic_recency_robust_v1",
        }
        if category and category != normalized_category:
            extra_metadata["raw_category"] = category
        extra_metadata["category_info"] = category_info
        extra_metadata["category_tags"] = category_info.get("category_tags", [])
        extra_metadata["factor_name"] = factor_name
        extra_metadata["factor_name_status"] = factor_name_status
        extra_metadata["proposed_factor_name"] = proposed_factor_name
        extra_metadata["factor_name_repaired"] = factor_name_repaired
        extra_metadata["factor_name_repair_reason"] = factor_name_repair_reason
        extra_metadata["holding_period_days"] = holding_period_days
        extra_metadata["compute_semantics_version"] = FACTOR_COMPUTE_SEMANTICS_VERSION
        extra_metadata["factor_value_compute_rules"] = {
            "time_series_history_mode": "full_stock_history_before_output_filter",
            "cross_section_universe_mode": "static_non_st_baseline_20260601",
            "output_universe_mode": "static_non_st_baseline_20260601",
        }
        extra_metadata["adopted_value_sync"] = {
            "mode": "deferred_active_only_refresh",
            "historical_wide_store": "not_rewritten_during_import",
            "quantgpt_active_store": "refreshed_by_active_values_worker_after_registry_commit",
        }

        factor_id = registry.register(
            name=factor_name,
            expression=expression,
            source="quantgpt",
            status="active",
            category=normalized_category,
            metrics=metrics,
            universe=universe,
            holding_period_days=holding_period_days,
            metadata={
                "data_path": parquet_path,
                "data_column": safe_name,
                **extra_metadata,
            },
        )

        imported += 1
        details.append(
            {
                "factor_id": factor_id,
                "name": factor_name,
                "proposed_factor_name": proposed_factor_name,
                "factor_name_repaired": factor_name_repaired,
                "factor_name_repair_reason": factor_name_repair_reason,
                "factor_name_status": factor_name_status,
                "data_column": safe_name,
                "expression": expression[:60],
                "category": normalized_category,
                "holding_period_days": holding_period_days,
                "parquet_path": parquet_path,
                "adopted_value_sync": "deferred_active_only_refresh",
                "ic_mean": metrics.get("ic_mean"),
                "icir": metrics.get("icir"),
                "rank_ic": metrics.get("rank_ic"),
                "rank_icir": metrics.get("rank_icir"),
                "annual_return": metrics.get("annual_return"),
                "deep_score": metrics.get("deep_score"),
            }
        )

        if submit_wq:
            from domain.factor_research.wq_submitter import submit_and_record_factor as _wq_submit

            _th.Thread(
                target=_wq_submit,
                args=(factor_id, wq_expression or "", safe_name, universe),
                daemon=True,
            ).start()

    logger.info(
        "[auto-import] %d imported + %d skipped / %d total",
        imported,
        skipped,
        len(adopted),
    )
    return {
        "imported": imported,
        "skipped": skipped,
        "errors": errors,
        "details": details,
        "submit_wq": submit_wq,
    }
