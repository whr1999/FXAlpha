from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd
from qlib.contrib.model.gbdt import LGBModel
from qlib.data.dataset.weight import Reweighter


FXALPHA_DEFAULT_SAMPLE_WEIGHT_POLICY = "top50_smooth2_bottom50_smooth1p5_mean_norm"


@dataclass(frozen=True)
class TopBottomRankWeightSpec:
    top_n: int = 50
    top_max: float = 2.0
    bottom_n: int = 50
    bottom_max: float = 1.5
    normalize_mean: bool = True
    label_col: str = "LABEL0"


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"false", "0", "no", "n", "off"}:
        return False
    if text in {"true", "1", "yes", "y", "on"}:
        return True
    return bool(value)


def _label_series(data: pd.DataFrame, label_col: str) -> pd.Series:
    if "label" not in data.columns.get_level_values(0):
        raise ValueError("FXAlphaTopBottomRankReweighter requires a Qlib label column group")
    label = data["label"]
    if isinstance(label, pd.DataFrame):
        if label_col in label.columns:
            return label[label_col].astype("float64")
        if label.shape[1] == 1:
            return label.iloc[:, 0].astype("float64")
        raise ValueError(f"label column {label_col!r} missing from label group")
    return label.astype("float64")


def _datetime_level(index: pd.Index) -> str | int:
    if isinstance(index, pd.MultiIndex):
        if "datetime" in index.names:
            return "datetime"
        return 0
    raise ValueError("FXAlphaTopBottomRankReweighter requires a MultiIndex with datetime")


def _weights_for_daily_label(label: pd.Series, spec: TopBottomRankWeightSpec) -> pd.Series:
    weight = pd.Series(1.0, index=label.index, dtype="float64")
    valid = label.replace([np.inf, -np.inf], np.nan).dropna()
    if valid.empty:
        return weight

    if spec.top_n > 0 and spec.top_max > 1.0:
        top = valid.sort_values(ascending=False).head(spec.top_n)
        top_steps = np.arange(len(top), 0, -1, dtype="float64") / max(float(spec.top_n), 1.0)
        weight.loc[top.index] = 1.0 + (float(spec.top_max) - 1.0) * top_steps

    if spec.bottom_n > 0 and spec.bottom_max > 1.0:
        bottom = valid.sort_values(ascending=True).head(spec.bottom_n)
        bottom_steps = np.arange(len(bottom), 0, -1, dtype="float64") / max(float(spec.bottom_n), 1.0)
        bottom_weight = 1.0 + (float(spec.bottom_max) - 1.0) * bottom_steps
        weight.loc[bottom.index] = np.maximum(weight.loc[bottom.index].to_numpy(), bottom_weight)

    if spec.normalize_mean:
        mean = float(weight.mean())
        if mean > 0 and np.isfinite(mean):
            weight = weight / mean
    return weight


class FXAlphaTopBottomRankReweighter(Reweighter):
    """Daily top/bottom label-rank sample weighting for FXAlpha LGBM training.

    The input label has already passed Qlib learn processors. Daily z-scoring is
    monotonic within each date, so using the processed label preserves the same
    cross-sectional rank as raw LABEL0 while keeping this reweighter inside the
    native Qlib DatasetH/DataHandlerLP/LGBModel workflow.
    """

    def __init__(
        self,
        top_n: int = 50,
        top_max: float = 2.0,
        bottom_n: int = 50,
        bottom_max: float = 1.5,
        normalize_mean: bool = True,
        label_col: str = "LABEL0",
    ):
        self.spec = TopBottomRankWeightSpec(
            top_n=int(top_n),
            top_max=float(top_max),
            bottom_n=int(bottom_n),
            bottom_max=float(bottom_max),
            normalize_mean=_coerce_bool(normalize_mean),
            label_col=str(label_col),
        )

    def reweight(self, data: pd.DataFrame) -> np.ndarray:
        label = _label_series(data, self.spec.label_col)
        level = _datetime_level(label.index)
        weights = [
            _weights_for_daily_label(group, self.spec)
            for _dt, group in label.groupby(level=level, sort=False)
        ]
        if not weights:
            return np.ones(len(data), dtype="float32")
        return pd.concat(weights).reindex(data.index).fillna(1.0).astype("float32").to_numpy()


def make_sample_reweighter(policy: str | None, kwargs: dict[str, Any] | None = None) -> Reweighter | None:
    normalized = str(policy or "").strip()
    if not normalized or normalized.lower() in {"none", "null", "false"}:
        return None
    kwargs = dict(kwargs or {})
    if normalized == "sticky":
        normalized = FXALPHA_DEFAULT_SAMPLE_WEIGHT_POLICY
    if normalized == FXALPHA_DEFAULT_SAMPLE_WEIGHT_POLICY:
        kwargs.setdefault("top_n", 50)
        kwargs.setdefault("top_max", 2.0)
        kwargs.setdefault("bottom_n", 50)
        kwargs.setdefault("bottom_max", 1.5)
        kwargs.setdefault("normalize_mean", True)
        return FXAlphaTopBottomRankReweighter(**kwargs)
    if normalized == "top50_smooth2_bottom50_smooth1p5":
        kwargs.setdefault("top_n", 50)
        kwargs.setdefault("top_max", 2.0)
        kwargs.setdefault("bottom_n", 50)
        kwargs.setdefault("bottom_max", 1.5)
        kwargs.setdefault("normalize_mean", False)
        return FXAlphaTopBottomRankReweighter(**kwargs)
    raise ValueError(f"unsupported FXAlpha sample_weight_policy={policy!r}")


class FXAlphaWeightedLGBModel(LGBModel):
    """Qlib LGBModel with FXAlpha's production sample-weight policy.

    Qlib's YAML task runner passes ``task.reweighter`` through without
    instantiating config dictionaries.  Keeping the weighting policy inside this
    thin subclass lets formal qrun stay on the canonical
    DatasetH/DataHandlerLP/LGBModel path without patching the installed qlib
    package.
    """

    def __init__(
        self,
        *args: Any,
        sample_weight_policy: str = FXALPHA_DEFAULT_SAMPLE_WEIGHT_POLICY,
        sample_weight_kwargs: dict[str, Any] | None = None,
        **kwargs: Any,
    ):
        self.sample_weight_policy = sample_weight_policy
        self.sample_weight_kwargs = dict(sample_weight_kwargs or {})
        super().__init__(*args, **kwargs)

    def fit(self, dataset, *args: Any, reweighter: Reweighter | None = None, **kwargs: Any):
        active_reweighter = reweighter
        if active_reweighter is None:
            active_reweighter = make_sample_reweighter(self.sample_weight_policy, self.sample_weight_kwargs)
        return super().fit(dataset, *args, reweighter=active_reweighter, **kwargs)


def validate_sample_weight_policy(policy: str | None, kwargs: dict | None = None) -> dict[str, Any]:
    from .contracts import DEFAULT_SAMPLE_WEIGHT_KWARGS, DEFAULT_SAMPLE_WEIGHT_POLICY, SAMPLE_WEIGHT_POLICIES

    normalized = policy or DEFAULT_SAMPLE_WEIGHT_POLICY
    if normalized not in SAMPLE_WEIGHT_POLICIES:
        return {"passed": False, "errors": [f"unsupported_sample_weight_policy:{normalized}"]}
    if normalized == DEFAULT_SAMPLE_WEIGHT_POLICY:
        merged = dict(DEFAULT_SAMPLE_WEIGHT_KWARGS)
        merged.update(kwargs or {})
        errors = [
            f"{key}_mismatch:{merged.get(key)}!=expected:{expected}"
            for key, expected in DEFAULT_SAMPLE_WEIGHT_KWARGS.items()
            if merged.get(key) != expected
        ]
        return {"passed": not errors, "errors": errors, "normalized_kwargs": merged}
    return {"passed": True, "errors": [], "normalized_kwargs": kwargs or {}}
