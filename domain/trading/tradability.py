from __future__ import annotations

from typing import Any

import pandas as pd


def apply_target_tradability_policy(target_df: pd.DataFrame, validation: dict[str, Any]) -> tuple[pd.DataFrame, dict[str, Any]]:
    filtered_targets = set(validation.get('filtered_target_instruments', []))
    frozen_current = sorted(validation.get('frozen_current_instruments', []))
    retry_required = sorted(validation.get('retry_needed_missing_target_invalid', [])) + sorted(validation.get('retry_needed_missing_current_only_invalid', []))

    if validation.get('policy_decision') in {'fail_fast', 'retry_required'}:
        raise RuntimeError(
            'tradability policy blocks execution: '
            f"policy={validation.get('policy_decision')}, "
            f"error_kind={validation.get('error_kind')}, "
            f"retry_required={retry_required}, "
            f"non_positive_examples={validation.get('non_positive_examples')}"
        )

    sanitized = target_df.copy()
    if filtered_targets:
        sanitized = sanitized[~sanitized['instrument'].astype(str).isin(filtered_targets)].copy()
    if sanitized.empty:
        raise RuntimeError(f'all target instruments were filtered out by tradability policy: {sorted(filtered_targets)}')

    policy_summary = {
        'policy_decision': validation.get('policy_decision'),
        'policy_reason': validation.get('policy_reason'),
        'filtered_target_instruments': sorted(filtered_targets),
        'filtered_target_count': len(filtered_targets),
        'frozen_current_instruments': frozen_current,
        'frozen_current_count': len(frozen_current),
        'kept_target_count': int(len(sanitized)),
        'retry_required_instruments': retry_required,
        'buy_limit_up_target_instruments': validation.get('buy_limit_up_target_instruments', []),
        'sell_limit_down_current_only_instruments': validation.get('sell_limit_down_current_only_instruments', []),
        'st_target_instruments': validation.get('st_target_instruments', []),
        'st_current_only_instruments': validation.get('st_current_only_instruments', []),
        'suspended_missing_target_invalid': validation.get('suspended_missing_target_invalid', []),
        'suspended_missing_current_only_invalid': validation.get('suspended_missing_current_only_invalid', []),
    }
    return sanitized, policy_summary
