from __future__ import annotations

import json
import os
import pickle
import re
import sys

from copy import deepcopy
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from lib.noise_suppress import suppress_all_known_noise
suppress_all_known_noise()

import mlflow
import pandas as pd
import qlib
from qlib.data import D
from qlib.data.dataset.handler import DataHandlerLP
from qlib.data.dataset.loader import NestedDataLoader, StaticDataLoader
from qlib.utils import init_instance_by_config
from qlib.workflow.online.update import PredUpdater, RMDLoader
from qlib.workflow.recorder import MLflowRecorder

from domain.factor_research.factor_compute import (
    _compute_factor_from_market_df,
    _load_market_data,
    _required_market_columns,
    _trim_factor_output,
    _warmup_start_date,
)
from domain.model.paths import MODEL_ACTIVE_PRODUCTION
from storage.model_registry import ModelRegistry
from storage.paths import MODEL_DEFAULT_START_DATE, MODEL_RUNS_ROOT, PREDICTION_FEATURE_RUNTIME_ROOT, QLIB_DATA_ROOT


RELATIVE_DATA_FILES = {
    'combined_factors_df.parquet',
    'labels_df.parquet',
}


def init_qlib() -> None:
    qlib.init(provider_uri=str(QLIB_DATA_ROOT), region='cn', expression_cache=None, dataset_cache=None)


def get_qlib_latest_calendar_date() -> object:
    cal = D.calendar(freq='day')
    return cal[-1]


def build_recorder_from_run_dir(run_dir: str | Path) -> MLflowRecorder:
    run_dir = Path(run_dir)
    exp_id = run_dir.parent.name
    run_id = run_dir.name
    mlruns_root = run_dir.parent.parent
    tracking_uri = f'file://{mlruns_root}'
    client = mlflow.tracking.MlflowClient(tracking_uri=tracking_uri)
    run = client.get_run(run_id)
    return MLflowRecorder(exp_id, tracking_uri, mlflow_run=run)


def _decode_json_field(value: Any, default: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return default
    return value if value is not None else default


def _normalize_registry_row(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if not row:
        return None
    normalized = dict(row)
    normalized['factor_ids'] = _decode_json_field(normalized.get('factor_ids'), [])
    normalized['metadata'] = _decode_json_field(normalized.get('metadata'), {})
    return normalized


def _load_model_run_manifest(model_run_id: str) -> dict[str, Any]:
    manifest_path = MODEL_RUNS_ROOT / model_run_id / 'manifest.json'
    if not manifest_path.exists():
        raise FileNotFoundError(f'model_run manifest not found: {manifest_path}')
    return json.loads(manifest_path.read_text(encoding='utf-8'))


def _load_model_run_artifacts(model_run_id: str) -> dict[str, Any]:
    artifacts_path = MODEL_RUNS_ROOT / model_run_id / 'artifacts.json'
    if not artifacts_path.exists():
        return {}
    return json.loads(artifacts_path.read_text(encoding='utf-8'))


def _is_direct_model_run(run_dir: str | Path) -> bool:
    run_dir = Path(run_dir)
    manifest_path = run_dir / 'manifest.json'
    if not manifest_path.is_file():
        return False
    try:
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    except (OSError, ValueError, TypeError):
        return False
    runner = manifest.get('runner') if isinstance(manifest.get('runner'), dict) else {}
    return runner.get('main_chain') == 'direct_qlib0627_workflow' or (run_dir / 'direct_qlib_manifest.json').is_file()


def build_dataset_from_direct_run(
    run_dir: str | Path,
    combined_factors_override: str | None = None,
    *,
    test_start: str | None = None,
    test_end: str | None = None,
):
    from qlib.data.dataset import DatasetH
    from domain.model.qlib_direct import _load_feature_frame

    run_dir = Path(run_dir)
    manifest = json.loads((run_dir / 'manifest.json').read_text(encoding='utf-8'))
    feature_manifest = deepcopy(manifest.get('feature_set_manifest') or (manifest.get('direct_qlib') or {}).get('feature_set_manifest') or {})
    if combined_factors_override:
        feature_manifest['combined_factors_file'] = str(combined_factors_override)
        feature_manifest['feature_file'] = str(combined_factors_override)
    frame = _load_feature_frame(feature_manifest)
    segments = deepcopy(manifest.get('experiment', {}).get('segments') or manifest.get('direct_qlib', {}).get('segments') or {})
    if test_start or test_end:
        start = _normalize_date_like(test_start or test_end)
        end = _normalize_date_like(test_end or test_start)
        segments['test'] = [start, end]
    if not all(segments.get(key) for key in ('train', 'valid', 'test')):
        raise RuntimeError('direct production manifest is missing train/valid/test segments')
    processors = manifest.get('resolved_processors') if isinstance(manifest.get('resolved_processors'), dict) else {}
    infer_processors = deepcopy(processors.get('infer_processors') or [])
    learn_processors = deepcopy(processors.get('learn_processors') or [])
    handler = DataHandlerLP(
        instruments=None,
        start_time=segments['train'][0],
        end_time=segments['test'][1],
        data_loader=StaticDataLoader(frame),
        infer_processors=infer_processors,
        learn_processors=learn_processors,
        process_type=DataHandlerLP.PTYPE_A,
        drop_raw=False,
    )
    return DatasetH(handler=handler, segments=segments)


def _normalize_date_like(value) -> str | None:
    if value is None:
        return None
    return str(pd.Timestamp(value).date())


def _next_day(value: str) -> str:
    return (pd.Timestamp(value) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")


def _date_max_from_index(df: pd.DataFrame) -> str | None:
    if df.empty or not isinstance(df.index, pd.MultiIndex) or "datetime" not in df.index.names:
        return None
    return _normalize_date_like(df.index.get_level_values("datetime").max())


def _feature_missing_warnings(df: pd.DataFrame, feature_cols: list[Any]) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    total = int(len(df))
    if total <= 0:
        return warnings
    for col in feature_cols:
        series = df[col]
        missing = int(series.isna().sum())
        if missing == total:
            warnings.append({
                "feature_column": str(col[1] if isinstance(col, tuple) else col),
                "warning": "feature_all_missing_for_prediction_window",
                "missing_count": missing,
                "missing_ratio": 1.0,
            })
    return warnings


def _normal_feature_frame(path: str | Path, alias: str) -> pd.DataFrame:
    df = pd.read_parquet(path)
    if df.empty:
        return df
    if not isinstance(df.index, pd.MultiIndex):
        raise RuntimeError(f"factor parquet has non-MultiIndex index: {path}")
    if isinstance(df.columns, pd.MultiIndex):
        col = ("feature", alias) if ("feature", alias) in df.columns else df.columns[0]
        out = df[[col]].copy()
    else:
        col = alias if alias in df.columns else df.columns[0]
        out = df[[col]].copy()
    out.columns = pd.MultiIndex.from_product([["feature"], [alias]])
    return out.sort_index()


def _model_factor_records(model_context: dict[str, Any]) -> list[dict[str, Any]]:
    manifest_records = (
        model_context.get("manifest", {})
        .get("feature_set_manifest", {})
        .get("factor_records", [])
    )
    if manifest_records:
        return [dict(item) for item in manifest_records]

    row = model_context.get("registry_row") or {}
    records: list[dict[str, Any]] = []
    from storage.factor_registry import FactorRegistry

    registry = FactorRegistry()
    for factor_id in row.get("factor_ids") or []:
        factor = registry.get(factor_id)
        if not factor:
            continue
        metadata = factor.get("metadata") or {}
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except Exception:
                metadata = {}
        records.append({
            "factor_id": factor_id,
            "name": factor.get("name") or factor_id,
            "expression": factor.get("expression", ""),
            "data_path": metadata.get("data_path", ""),
            "data_column": metadata.get("data_column") or factor_id,
        })
    return records


def _prediction_feature_dir(model_run_id: str) -> Path:
    return PREDICTION_FEATURE_RUNTIME_ROOT / model_run_id


def _expression_warmup_days(expression: str, *, minimum: int = 90, buffer_days: int = 10) -> int:
    windows = [
        int(item)
        for item in re.findall(r"\bts_[a-zA-Z_]+\s*\([^)]*,\s*(\d+)\s*\)", expression or "")
    ]
    if not windows:
        return minimum
    return max(minimum, max(windows) + buffer_days)


def _prediction_warmup_start_date(stale_records: list[dict[str, Any]], start_date: str) -> str:
    warmup_days = max(
        (_expression_warmup_days(str(item.get("expression", ""))) for item in stale_records),
        default=90,
    )
    return _warmup_start_date(start_date, warmup_days=warmup_days)


def _cached_prediction_feature_file(model_context: dict[str, Any], target_date: str) -> dict[str, Any] | None:
    model_run_id = model_context.get("model_run_id", "")
    cache_dir = _prediction_feature_dir(model_run_id)
    manifest_file = cache_dir / f"manifest_{target_date}.json"
    if not manifest_file.exists():
        return None
    try:
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    except Exception:
        return None
    combined_file = Path(manifest.get("combined_factors_file", ""))
    if not combined_file.exists():
        return None
    if manifest.get("target_date") != target_date:
        return None
    if manifest.get("model_run_id") != model_run_id:
        return None
    try:
        cached_index = pd.read_parquet(combined_file, columns=[]).index
        cached_dates = pd.to_datetime(cached_index.get_level_values("datetime")).normalize()
        # Qlib processors used by model need a history window. A
        # single-day runtime cache can make every processed feature collapse to
        # zero, which then turns recommendations into instrument-order sorting.
        if cached_dates.nunique() < 30:
            return None
    except Exception:
        return None
    return manifest


def _latest_cached_prediction_feature_file_before(model_context: dict[str, Any], target_date: str) -> dict[str, Any] | None:
    model_run_id = model_context.get("model_run_id", "")
    cache_dir = _prediction_feature_dir(model_run_id)
    if not cache_dir.exists():
        return None
    best: dict[str, Any] | None = None
    best_date: str | None = None
    for manifest_file in cache_dir.glob("manifest_*.json"):
        try:
            manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        if manifest.get("model_run_id") != model_run_id:
            continue
        combined_file = Path(manifest.get("combined_factors_file", ""))
        if not combined_file.exists():
            continue
        try:
            cached_index = pd.read_parquet(combined_file, columns=[]).index
            cached_dates = pd.to_datetime(cached_index.get_level_values("datetime")).normalize()
            if cached_dates.nunique() < 30:
                continue
        except Exception:
            continue
        latest = _normalize_date_like(manifest.get("latest_date") or manifest.get("target_date"))
        if not latest or latest >= target_date:
            continue
        if best_date is None or latest > best_date:
            best = manifest
            best_date = latest
    return best


def build_prediction_feature_cache(
    model_context: dict[str, Any],
    *,
    target_date: str,
    start_date: str | None = None,
) -> dict[str, Any]:
    """Build a runtime-only feature file for daily prediction.

    This intentionally writes under runtime/trading and never updates the model
    training feature snapshot or factor registry.
    """
    target_date = _normalize_date_like(target_date) or str(target_date)
    cached = _cached_prediction_feature_file(model_context, target_date)
    if cached:
        return cached

    records = _model_factor_records(model_context)
    if not records:
        raise RuntimeError("model has no factor records for prediction feature rebuild")

    resolved_start = start_date or MODEL_DEFAULT_START_DATE
    previous_cache = _latest_cached_prediction_feature_file_before(model_context, target_date)
    previous_cache_df: pd.DataFrame | None = None
    previous_cache_latest = None
    if previous_cache:
        previous_cache_latest = _normalize_date_like(previous_cache.get("latest_date") or previous_cache.get("target_date"))
        previous_cache_df = pd.read_parquet(previous_cache["combined_factors_file"])

    feature_frames: list[pd.DataFrame] = []
    stale_records: list[dict[str, Any]] = []
    min_missing_start: str | None = None

    for record in records:
        alias = str(record.get("data_column") or record.get("factor_id") or record.get("name"))
        data_path = record.get("data_path")
        cache_col = ("feature", alias)
        if previous_cache_df is not None and cache_col in previous_cache_df.columns:
            old_frame = previous_cache_df[[cache_col]].copy()
            latest = _date_max_from_index(old_frame) or previous_cache_latest
            missing_start = resolved_start if latest is None else _next_day(latest)
        elif not data_path or not Path(data_path).exists():
            latest = None
            old_frame = pd.DataFrame()
            missing_start = resolved_start
        else:
            old_frame = _normal_feature_frame(data_path, alias)
            latest = _date_max_from_index(old_frame)
            missing_start = resolved_start if latest is None else _next_day(latest)

        if old_frame is not None and not old_frame.empty:
            dates = pd.to_datetime(old_frame.index.get_level_values("datetime")).normalize()
            old_frame = old_frame[(dates >= pd.Timestamp(resolved_start)) & (dates <= pd.Timestamp(target_date))]

        if latest is None or latest < target_date:
            expression = record.get("expression", "")
            if not expression:
                raise RuntimeError(f"factor {record.get('factor_id')} missing expression for prediction rebuild")
            stale_records.append({
                "factor_id": record.get("factor_id", ""),
                "alias": alias,
                "expression": expression,
                "existing_latest": latest,
                "missing_start": missing_start,
            })
            min_missing_start = missing_start if min_missing_start is None else min(min_missing_start, missing_start)

        feature_frames.append(old_frame)

    computed_by_alias: dict[str, pd.DataFrame] = {}
    if stale_records:
        load_start = _prediction_warmup_start_date(stale_records, min_missing_start or resolved_start)
        required_columns = _required_market_columns([item["expression"] for item in stale_records])
        market_df = _load_market_data(
            start_date=load_start,
            end_date=target_date,
            required_columns=required_columns,
            filter_non_st=False,
        )
        if market_df.empty:
            raise RuntimeError(f"no market data available for prediction feature rebuild through {target_date}")
        for item in stale_records:
            computed = _compute_factor_from_market_df(market_df, item["expression"])
            computed = _trim_factor_output(computed, item["missing_start"], target_date)
            if computed.empty:
                raise RuntimeError(f"no computed values for factor {item['factor_id']} through {target_date}")
            computed.columns = pd.MultiIndex.from_product([["feature"], [item["alias"]]])
            computed_by_alias[item["alias"]] = computed.sort_index()

    merged_frames: list[pd.DataFrame] = []
    for record, old_frame in zip(records, feature_frames):
        alias = str(record.get("data_column") or record.get("factor_id") or record.get("name"))
        computed = computed_by_alias.get(alias)
        if computed is not None:
            pieces = [df for df in (old_frame, computed) if df is not None and not df.empty]
            frame = pd.concat(pieces).sort_index()
            frame = frame[~frame.index.duplicated(keep="last")]
        else:
            frame = old_frame
        if frame is None or frame.empty:
            raise RuntimeError(f"empty feature frame for factor {record.get('factor_id')}")
        merged_frames.append(frame)

    combined = pd.concat(merged_frames, axis=1, join="outer").sort_index()
    combined = combined.loc[:, ~combined.columns.duplicated(keep="last")]
    feature_cols = [col for col in combined.columns if col[0] == "feature"]
    feature_missing_warnings = _feature_missing_warnings(combined, feature_cols)
    if combined.empty:
        raise RuntimeError("prediction feature cache is empty after joining features")
    base_factor_file = model_context.get("platform_combined_factors_file")
    if base_factor_file and Path(base_factor_file).exists():
        base_df = pd.read_parquet(base_factor_file)
        if isinstance(base_df.columns, pd.MultiIndex):
            label_cols = [col for col in base_df.columns if col[0] == "label"]
            if label_cols:
                labels = base_df[label_cols].copy()
                combined = combined.join(labels, how="left")
    latest_date = _date_max_from_index(combined)
    if latest_date is None or latest_date < target_date:
        raise RuntimeError(f"prediction feature cache stale after rebuild: {latest_date} < {target_date}")

    out_dir = _prediction_feature_dir(model_context["model_run_id"])
    out_dir.mkdir(parents=True, exist_ok=True)
    combined_file = out_dir / f"combined_factors_{target_date}.parquet"
    manifest_file = out_dir / f"manifest_{target_date}.json"
    combined.to_parquet(combined_file, engine="pyarrow")
    manifest = {
        "model_id": model_context.get("model_id", ""),
        "model_run_id": model_context.get("model_run_id", ""),
        "feature_set_id": model_context.get("feature_set_id", ""),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "target_date": target_date,
        "start_date": resolved_start,
        "latest_date": latest_date,
        "combined_factors_file": str(combined_file),
        "shape": list(combined.shape),
        "factor_count": len(records),
        "stale_factor_count": len(stale_records),
        "stale_factors": stale_records,
        "feature_universe_policy": "adopted_factor_static_universe_no_point_in_time_st_post_filter",
        "feature_missing_policy": "preserve_nan_for_qlib_processors",
        "feature_missing_warnings": feature_missing_warnings,
        "source": "runtime_prediction_feature_cache",
    }
    manifest_file.write_text(json.dumps(manifest, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return manifest


def _workspace_root_from_run_dir(run_dir: Path) -> Path:
    return run_dir.parents[2]


@lru_cache(maxsize=16)
def _feature_file_instruments(feature_file: str) -> tuple[str, ...]:
    df = pd.read_parquet(feature_file, columns=[])
    if not isinstance(df.index, pd.MultiIndex) or "instrument" not in df.index.names:
        raise RuntimeError(f"feature file has no instrument index: {feature_file}")
    return tuple(sorted(str(item) for item in df.index.get_level_values("instrument").unique()))


def _absolutize_task_paths(obj: Any, workspace_root: Path, combined_factors_override: str | None = None) -> Any:
    if isinstance(obj, dict):
        new_obj = {}
        for k, v in obj.items():
            if k == 'config' and isinstance(v, str) and Path(v).name in RELATIVE_DATA_FILES:
                if combined_factors_override and Path(v).name == 'combined_factors_df.parquet':
                    new_obj[k] = str(combined_factors_override)
                elif not Path(v).is_absolute():
                    new_obj[k] = str(workspace_root / v)
                else:
                    new_obj[k] = v
            elif (
                k == 'instruments'
                and isinstance(v, str)
                and v == 'fxalpha_model_all'
                and combined_factors_override
            ):
                # StaticDataLoader cannot resolve FXAlpha's training-universe alias.
                # For daily prediction, bind it to the instruments present in the runtime feature file.
                new_obj[k] = list(_feature_file_instruments(str(combined_factors_override)))
            else:
                new_obj[k] = _absolutize_task_paths(v, workspace_root, combined_factors_override)
        return new_obj
    if isinstance(obj, list):
        return [_absolutize_task_paths(v, workspace_root, combined_factors_override) for v in obj]
    return obj


def _heal_loader_state(obj: Any) -> None:
    if isinstance(obj, StaticDataLoader) and not hasattr(obj, '_data'):
        obj._data = None
    if isinstance(obj, NestedDataLoader):
        for dl in obj.data_loader_l:
            _heal_loader_state(dl)
    for attr in ('handler', 'data_loader', 'data_loader_l'):
        if hasattr(obj, attr):
            value = getattr(obj, attr)
            if isinstance(value, list):
                for item in value:
                    _heal_loader_state(item)
            else:
                _heal_loader_state(value)


def build_dataset_from_task_artifact(run_dir: str | Path, combined_factors_override: str | None = None):
    run_dir = Path(run_dir)
    workspace_root = _workspace_root_from_run_dir(run_dir)
    task_path = run_dir / 'artifacts' / 'task'
    task = pickle.loads(task_path.read_bytes())
    dataset_cfg = _absolutize_task_paths(deepcopy(task['dataset']), workspace_root, combined_factors_override)
    dataset = init_instance_by_config(dataset_cfg)
    _heal_loader_state(dataset)
    return dataset


class PlatformRMDLoader(RMDLoader):
    def __init__(self, rec: MLflowRecorder, run_dir: str | Path, combined_factors_override: str | None = None):
        super().__init__(rec)
        self.run_dir = Path(run_dir)
        self.workspace_root = _workspace_root_from_run_dir(self.run_dir)
        self.combined_factors_override = combined_factors_override

    def get_dataset(self, start_time, end_time, segments=None, unprepared_dataset=None):
        if unprepared_dataset is None:
            dataset = build_dataset_from_task_artifact(self.run_dir, self.combined_factors_override)
        else:
            dataset = unprepared_dataset
        if segments is None:
            segments = {'test': (start_time, end_time)}
        dataset.config(handler_kwargs={'start_time': start_time, 'end_time': end_time}, segments=segments)
        dataset.setup_data(handler_kwargs={'init_type': DataHandlerLP.IT_LS})
        return dataset

    def get_model(self):
        model_path = self.run_dir / 'artifacts' / 'params.pkl'
        added_path = False
        if str(self.workspace_root) not in sys.path:
            sys.path.insert(0, str(self.workspace_root))
            added_path = True
        old_cwd = Path.cwd()
        os.chdir(self.workspace_root)
        try:
            with model_path.open('rb') as f:
                return pickle.load(f)
        finally:
            os.chdir(old_cwd)
            if added_path and sys.path and sys.path[0] == str(self.workspace_root):
                sys.path.pop(0)


def list_model_rows() -> list[dict[str, Any]]:
    registry = ModelRegistry()
    return [_normalize_registry_row(row) for row in registry.list_models('all')]


def select_best_model_row() -> dict[str, Any] | None:
    rows = [row for row in list_model_rows() if row and row.get('status') in {'active', 'production'} and row.get('run_dir')]
    if not rows:
        return None

    def sort_key(row: dict[str, Any]):
        sharpe = row.get('sharpe')
        annualized_ret = row.get('annualized_ret')
        icir = row.get('icir')
        return (
            1 if row.get('status') == 'production' else 0,
            0 if sharpe is None else 1,
            float(sharpe or -1e18),
            float(annualized_ret or -1e18),
            float(icir or -1e18),
            row.get('created_at', ''),
        )

    return sorted(rows, key=sort_key, reverse=True)[0]


def resolve_prediction_model_context(
    model_id: str | None = None,
    model_run_id: str | None = None,
    require_production: bool = False,
) -> dict[str, Any]:
    registry = ModelRegistry()
    row = None

    if model_id:
        row = _normalize_registry_row(registry.get(model_id))
        if not row:
            raise FileNotFoundError(f'model_id not found: {model_id}')
    elif model_run_id:
        for candidate in list_model_rows():
            if candidate.get('model_run_id') == model_run_id:
                row = candidate
                break
        if row is None:
            raise FileNotFoundError(f'model_run_id not found in model_registry: {model_run_id}')
    else:
        pointer: dict[str, Any] = {}
        try:
            pointer = json.loads(MODEL_ACTIVE_PRODUCTION.read_text(encoding='utf-8')) if MODEL_ACTIVE_PRODUCTION.exists() else {}
        except (OSError, ValueError, TypeError):
            pointer = {}
        pointer_model_id = str(pointer.get('model_id') or '')
        row = _normalize_registry_row(registry.get(pointer_model_id)) if pointer_model_id else None
        if row is not None and row.get('status') != 'production':
            row = None
        if row is None and not pointer_model_id:
            row = _normalize_registry_row(registry.get_production())
        if row is None and not require_production:
            row = select_best_model_row()
        if row is None:
            raise RuntimeError('no production model available; promote a model first')

    manifest = _load_model_run_manifest(row['model_run_id'])
    artifacts = _load_model_run_artifacts(row['model_run_id'])
    run_dir = row.get('run_dir') or artifacts.get('run_dir') or manifest.get('summary', {}).get('run_dir')
    if not run_dir:
        raise FileNotFoundError(f"model_run {row['model_run_id']} has no recorder run_dir")
    feature_set_id = str(row.get('feature_set_id') or manifest.get('feature_set_id') or '')
    feature_set_manifest = manifest.get('feature_set_manifest') if isinstance(manifest.get('feature_set_manifest'), dict) else {}
    combined_factors_file = manifest.get('platform_combined_factors_file') or feature_set_manifest.get('combined_factors_file')
    latest_date = manifest.get('latest_date') or feature_set_manifest.get('latest_date') or feature_set_manifest.get('actual_end_date')
    if (not combined_factors_file or not latest_date) and feature_set_id:
        from domain.model.feature_set_builder import load_feature_set_manifest

        feature_set_manifest = load_feature_set_manifest(feature_set_id) or {}
        combined_factors_file = combined_factors_file or feature_set_manifest.get('combined_factors_file') or feature_set_manifest.get('feature_file')
        latest_date = latest_date or feature_set_manifest.get('latest_date') or feature_set_manifest.get('actual_end_date')
    return {
        'source': 'production_model' if row.get('status') == 'production' else 'model_registry',
        'model_id': row.get('model_id', ''),
        'model_run_id': row.get('model_run_id', ''),
        'model_family': row.get('model_family', ''),
        'status': row.get('status', ''),
        'feature_set_id': feature_set_id,
        'feature_set_fingerprint': row.get('feature_set_fingerprint', manifest.get('feature_set_fingerprint', '')),
        'platform_combined_factors_file': str(combined_factors_file) if combined_factors_file else '',
        'platform_factor_latest_date': _normalize_date_like(latest_date),
        'recorder_run_dir': str(run_dir),
        'artifact_dir': artifacts.get('artifact_dir') or str(Path(run_dir) / 'artifacts'),
        'manifest': manifest,
        'artifacts': artifacts,
        'registry_row': row,
    }


def ensure_factor_freshness(model_context: dict[str, Any], to_date=None, *, allow_rebuild: bool = True) -> dict[str, Any]:
    factor_file = model_context.get('platform_combined_factors_file')
    factor_latest = model_context.get('platform_factor_latest_date')
    if not factor_file or not Path(factor_file).exists():
        raise FileNotFoundError('platform_combined_factors_file is missing; build active feature set first')
    if not factor_latest:
        raise RuntimeError('platform_factor_latest_date is missing; rebuild active feature set first')
    factor_latest_norm = _normalize_date_like(factor_latest)
    target_norm = _normalize_date_like(to_date) if to_date is not None else None
    if target_norm and factor_latest_norm < target_norm:
        cached = _cached_prediction_feature_file(model_context, target_norm)
        if cached:
            model_context['platform_combined_factors_file'] = cached['combined_factors_file']
            model_context['platform_factor_latest_date'] = cached['latest_date']
            model_context['prediction_feature_cache'] = cached
            return {
                'status': 'fresh',
                'factor_file': str(cached['combined_factors_file']),
                'factor_latest_date': cached['latest_date'],
                'target_date': target_norm,
                'source': 'runtime_prediction_feature_cache',
                'base_factor_file': str(factor_file),
                'base_factor_latest_date': factor_latest_norm,
                'stale_factor_count': str(cached.get('stale_factor_count', 0)),
            }
        if not allow_rebuild:
            records = _model_factor_records(model_context)
            return {
                'status': 'feature_rebuild_required',
                'factor_file': str(factor_file),
                'factor_latest_date': factor_latest_norm,
                'target_date': target_norm,
                'source': 'platform_feature_snapshot',
                'required_runtime_cache_root': str(_prediction_feature_dir(model_context.get('model_run_id', ''))),
                'factor_count': len(records),
            }
        cache = build_prediction_feature_cache(model_context, target_date=target_norm)
        model_context['platform_combined_factors_file'] = cache['combined_factors_file']
        model_context['platform_factor_latest_date'] = cache['latest_date']
        model_context['prediction_feature_cache'] = cache
        return {
            'status': 'fresh',
            'factor_file': str(cache['combined_factors_file']),
            'factor_latest_date': cache['latest_date'],
            'target_date': target_norm,
            'source': 'runtime_prediction_feature_cache',
            'base_factor_file': str(factor_file),
            'base_factor_latest_date': factor_latest_norm,
            'stale_factor_count': str(cache.get('stale_factor_count', 0)),
        }
    return {
        'status': 'fresh',
        'factor_file': str(factor_file),
        'factor_latest_date': factor_latest_norm,
        'target_date': target_norm or factor_latest_norm,
        'source': 'platform_feature_snapshot',
    }


def validate_pred_inputs(model_context: dict[str, Any], to_date=None) -> dict[str, Any]:
    run_dir = Path(model_context['recorder_run_dir'])
    direct_layout = _is_direct_model_run(run_dir)
    artifact_dir = run_dir if direct_layout else run_dir / 'artifacts'
    required = (
        {
            'manifest.json': run_dir / 'manifest.json',
            'model.pkl': run_dir / 'model.pkl',
            'params.pkl': run_dir / 'params.pkl',
            'pred.pkl': run_dir / 'pred.pkl',
        }
        if direct_layout
        else {
            'task': artifact_dir / 'task',
            'params.pkl': artifact_dir / 'params.pkl',
            'pred.pkl': artifact_dir / 'pred.pkl',
        }
    )
    missing = [name for name, path in required.items() if not path.exists()]
    if missing:
        raise FileNotFoundError(f'missing recorder artifacts: {missing}')

    target_norm = _normalize_date_like(to_date) if to_date is not None else None
    if target_norm:
        dataset = (
            build_dataset_from_direct_run(
                run_dir,
                model_context.get('platform_combined_factors_file'),
                test_start=target_norm,
                test_end=target_norm,
            )
            if direct_layout
            else build_dataset_from_task_artifact(run_dir, model_context.get('platform_combined_factors_file'))
        )
        dataset.config(
            handler_kwargs={'start_time': target_norm, 'end_time': target_norm},
            segments={'test': (target_norm, target_norm)},
        )
        dataset.setup_data(handler_kwargs={'init_type': DataHandlerLP.IT_LS})
        test_sampler = dataset.prepare('test', col_set='feature')
        sample_count = len(test_sampler)
        if sample_count <= 0:
            raise RuntimeError(f'no test samples available for pred target date {target_norm}')
        feature_quality = _prepared_feature_quality(test_sampler)
        if (
            feature_quality.get("feature_count", 0) > 0
            and feature_quality.get("zero_var_feature_count") == feature_quality.get("feature_count")
        ):
            raise RuntimeError(
                "processed_prediction_features_degenerate: "
                f"all {feature_quality['feature_count']} processed features have zero variance "
                f"for {target_norm}; rebuild runtime feature cache with sufficient history"
            )
    else:
        sample_count = None
        feature_quality = None

    return {
        'artifact_dir': str(artifact_dir),
        'recorder_run_dir': str(run_dir),
        'model_id': model_context.get('model_id', ''),
        'model_run_id': model_context.get('model_run_id', ''),
        'run_context_source': model_context.get('source', ''),
        'required_artifacts_ok': True,
        'artifact_layout': 'direct_qlib' if direct_layout else 'mlflow_recorder',
        'target_date': target_norm,
        'test_sample_count': int(sample_count) if sample_count is not None else None,
        'feature_quality': feature_quality,
    }


def _prepared_feature_quality(test_sampler: Any) -> dict[str, Any]:
    frame = test_sampler if hasattr(test_sampler, "columns") else getattr(test_sampler, "data", None)
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        return {"feature_count": 0, "sample_count": 0, "zero_var_feature_count": 0}
    numeric = frame.apply(pd.to_numeric, errors="coerce")
    unique_counts = numeric.nunique(dropna=True)
    return {
        "sample_count": int(len(numeric)),
        "feature_count": int(numeric.shape[1]),
        "nan_ratio": float(numeric.isna().sum().sum() / max(numeric.shape[0] * numeric.shape[1], 1)),
        "zero_var_feature_count": int((unique_counts <= 1).sum()),
        "all_nan_feature_count": int(numeric.isna().all(axis=0).sum()),
    }


def update_pred_for_recorder(run_dir: str | Path, to_date=None, from_date=None, combined_factors_override: str | None = None) -> dict[str, Any]:
    run_dir = Path(run_dir)
    if _is_direct_model_run(run_dir):
        start = _normalize_date_like(from_date or to_date)
        end = _normalize_date_like(to_date or from_date)
        if not start or not end:
            raise ValueError('direct production prediction update requires to_date or from_date')
        dataset = build_dataset_from_direct_run(
            run_dir,
            combined_factors_override,
            test_start=start,
            test_end=end,
        )
        with (run_dir / 'model.pkl').open('rb') as fh:
            model = pickle.load(fh)
        updated = model.predict(dataset, segment='test')
        if isinstance(updated, pd.Series):
            updated = updated.rename('score').to_frame()
        elif 'score' not in updated.columns and len(updated.columns) == 1:
            updated = updated.rename(columns={updated.columns[0]: 'score'})
        updated = updated.sort_index()
        pred_path = run_dir / 'pred.pkl'
        existing = pd.read_pickle(pred_path) if pred_path.is_file() else pd.DataFrame()
        if isinstance(existing, pd.Series):
            existing = existing.rename('score').to_frame()
        if not existing.empty and isinstance(existing.index, pd.MultiIndex):
            existing_dates = pd.to_datetime(existing.index.get_level_values('datetime')).normalize()
            existing = existing[(existing_dates < pd.Timestamp(start)) | (existing_dates > pd.Timestamp(end))]
        merged = pd.concat([existing, updated]).sort_index()
        merged = merged[~merged.index.duplicated(keep='last')]
        merged.to_pickle(pred_path)
        return {
            'layout': 'direct_qlib',
            'pred_latest': str(merged.index.get_level_values('datetime').max()),
            'updated_start': start,
            'updated_end': end,
            'updated_rows': int(len(updated)),
            'pred_path': str(pred_path),
        }
    rec = build_recorder_from_run_dir(run_dir)
    old_cwd = Path.cwd()
    os.chdir(_workspace_root_from_run_dir(run_dir))
    try:
        updater = PredUpdater(
            rec,
            to_date=to_date,
            from_date=from_date,
            loader_cls=lambda rec: PlatformRMDLoader(rec, run_dir, combined_factors_override),
        )
        updater.update()
        pred = rec.load_object('pred.pkl')
        latest = pred.index.get_level_values('datetime').max()
        return {
            'recorder_id': rec.id,
            'recorder_name': rec.name,
            'pred_latest': str(latest),
            'tracking_uri': rec.uri,
            'artifact_uri': rec.artifact_uri,
        }
    finally:
        os.chdir(old_cwd)


def load_pred_dataframe(run_dir: str | Path) -> pd.DataFrame:
    run_dir = Path(run_dir)
    if _is_direct_model_run(run_dir):
        pred = pd.read_pickle(run_dir / 'pred.pkl')
        return pred.rename('score').to_frame() if isinstance(pred, pd.Series) else pred
    rec = build_recorder_from_run_dir(run_dir)
    pred: pd.DataFrame = rec.load_object('pred.pkl')
    return pred


def latest_pred_snapshot(
    model_context: dict[str, Any],
    as_of_date: str | None = None,
) -> tuple[pd.Timestamp, pd.DataFrame]:
    pred = load_pred_dataframe(model_context['recorder_run_dir'])
    dts = pd.to_datetime(pred.index.get_level_values('datetime')).normalize()
    latest_dt = pd.Timestamp(as_of_date).normalize() if as_of_date else pd.Timestamp(dts.max()).normalize()
    available = set(dts)
    if latest_dt not in available:
        if as_of_date:
            raise RuntimeError(f'pred.pkl has no predictions for requested date {latest_dt.date()}')
        latest_dt = pd.Timestamp(max(available)).normalize()
    latest = pred.xs(latest_dt, level='datetime').copy()
    score_col = 'score' if 'score' in latest.columns else latest.columns[0]
    latest = latest.reset_index().rename(columns={score_col: 'score'})
    latest['instrument'] = latest['instrument'].astype(str)
    latest['score'] = latest['score'].astype(float)
    latest = latest.sort_values(['score', 'instrument'], ascending=[False, True]).reset_index(drop=True)
    latest.insert(0, 'rank', range(1, len(latest) + 1))
    return latest_dt, latest
