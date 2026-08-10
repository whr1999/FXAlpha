from __future__ import annotations


# Single prompt-facing operator palette for the production Orchestrator. This
# is deliberately narrower than every parser capability:
# only operators approved for autonomous factor design belong here.
PRODUCTION_OPERATOR_PALETTE = (
    "rank", "zscore", "group_rank", "group_zscore", "scale", "sign",
    "sign_power", "tanh", "sigmoid", "clip", "log", "abs", "sqrt",
    "exp", "power", "max", "min", "where", "and", "or", "ts_mean",
    "ts_std", "ts_max", "ts_min", "ts_sum", "ts_shift", "ts_delta",
    "ts_rank", "ts_argmax", "ts_argmin", "ts_corr", "ts_cov",
    "ts_av_diff", "ts_zscore", "decay_linear", "indneutralize",
    "trade_when", "ema", "sma", "wma",
)


# Prompt-facing signatures are the executable local parser contract, not
# illustrative pseudocode.  Keep these beside the palette so every LLM stage
# that may create or recommend an expression sees the same arity rules.
PRODUCTION_OPERATOR_SIGNATURES = {
    "rank": "rank(x)",
    "zscore": "zscore(x)",
    "group_rank": "group_rank(x, group_column)",
    "group_zscore": "group_zscore(x, group_column)",
    "scale": "scale(x)",
    "sign": "sign(x)",
    "sign_power": "sign_power(x, exponent)",
    "tanh": "tanh(x)",
    "sigmoid": "sigmoid(x)",
    "clip": "clip(x, lower, upper)",
    "log": "log(x)",
    "abs": "abs(x)",
    "sqrt": "sqrt(x)",
    "exp": "exp(x)",
    "power": "power(x, exponent)",
    "max": "max(x, y)",
    "min": "min(x, y)",
    "where": "where(condition, true_value, false_value)",
    "and": "condition_a and condition_b",
    "or": "condition_a or condition_b",
    "ts_mean": "ts_mean(x, window)",
    "ts_std": "ts_std(x, window)",
    "ts_max": "ts_max(x, window)",
    "ts_min": "ts_min(x, window)",
    "ts_sum": "ts_sum(x, window)",
    "ts_shift": "ts_shift(x, window)",
    "ts_delta": "ts_delta(x, window)",
    "ts_rank": "ts_rank(x, window)",
    "ts_argmax": "ts_argmax(x, window)",
    "ts_argmin": "ts_argmin(x, window)",
    "ts_corr": "ts_corr(x, y, window)",
    "ts_cov": "ts_cov(x, y, window)",
    "ts_av_diff": "ts_av_diff(x, window)",
    "ts_zscore": "ts_zscore(x, window)",
    "decay_linear": "decay_linear(x, window)",
    "indneutralize": "indneutralize(x, group_column)",
    "trade_when": "trade_when(condition, alpha, hold_value)",
    "ema": "ema(x, window)",
    "sma": "sma(x, window)",
    "wma": "wma(x, window)",
}


def production_operator_palette() -> list[str]:
    return list(PRODUCTION_OPERATOR_PALETTE)


def production_operator_signatures() -> dict[str, str]:
    return {
        operator: PRODUCTION_OPERATOR_SIGNATURES[operator]
        for operator in PRODUCTION_OPERATOR_PALETTE
        if operator in PRODUCTION_OPERATOR_SIGNATURES
    }
