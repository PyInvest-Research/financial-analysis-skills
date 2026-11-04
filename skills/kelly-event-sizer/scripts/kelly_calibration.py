"""Calibrate shrinkage on recorded predictions with nested held-out scoring."""

from datetime import datetime, timezone
from typing import Any

import numpy as np

from kelly import REMINDERS, number, probability


Json = dict[str, Any]
GRID = np.arange(101, dtype=float) / 100
CLIP = 1e-4
BOOTSTRAPS = 1000


def timestamp(value: Any) -> float:
    """Parse an ISO 8601 timestamp with an explicit UTC offset or raise."""
    if not isinstance(value, str):
        raise ValueError("timestamp must be an ISO 8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("Invalid ISO 8601 timestamp") from error
    if parsed.tzinfo is None:
        raise ValueError("timestamp requires Z or an explicit UTC offset")
    return parsed.timestamp()


def splits(times: np.ndarray | None, count: int,
           seed: int) -> list[tuple[np.ndarray, np.ndarray]]:
    """Return five shuffled folds or expanding windows over whole time groups.

    Identical timestamps never straddle a temporal train/test boundary.
    Inner selections with too few unique times use their training records;
    outer test records still remain strictly held out.
    """
    if times is None:
        indices = np.random.default_rng(seed).permutation(count)
        blocks = np.array_split(indices, min(5, count))
        return [(np.concatenate([b for j, b in enumerate(blocks) if j != i]),
                 test) for i, test in enumerate(blocks)] if count > 1 else []
    unique = np.unique(times)
    if len(unique) < 2:
        return []
    blocks = np.array_split(unique, min(6, len(unique)))
    return [(np.flatnonzero(times < block[0]),
             np.flatnonzero(np.isin(times, block))) for block in blocks[1:]]


def losses(p: np.ndarray, q: np.ndarray,
           y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Return per-record log loss and Brier score across the fixed grid."""
    predicted = np.clip(q[:, None] + (p - q)[:, None] * GRID,
                        CLIP, 1 - CLIP)
    log_loss = -(y[:, None] * np.log(predicted)
                 + (1 - y[:, None]) * np.log1p(-predicted))
    return log_loss, (predicted - y[:, None]) ** 2


def best_index(values: np.ndarray) -> int:
    """Resolve numerical ties within 1e-12 toward the smallest lambda."""
    mean_loss = np.mean(values, axis=0)
    return int(np.flatnonzero(mean_loss <= mean_loss.min() + 1e-12)[0])


def selection_rows(times: np.ndarray | None, count: int,
                   seed: int) -> np.ndarray:
    """Use held-out candidate losses; omit the temporal warmup period."""
    folds = splits(times, count, seed)
    if not folds:
        return np.arange(count)
    return np.sort(np.concatenate([test for _, test in folds]))


def resample(rng: np.random.Generator, count: int,
             temporal: bool) -> np.ndarray:
    """Draw IID rows or contiguous moving blocks for temporal dependence."""
    if not temporal:
        return rng.integers(0, count, size=count)
    length = max(1, int(np.ceil(np.sqrt(count))))
    starts = rng.integers(0, count - length + 1,
                          size=int(np.ceil(count / length)))
    return np.concatenate([np.arange(start, start + length)
                           for start in starts])[:count]


def calibrate_records(data: Json) -> Json:
    """Select lambda and report genuinely held-out adaptive-strategy scores.

    lambda_hat is selected for future use from historical candidate losses.
    The reported calibrated score uses a lambda selected separately inside
    each outer training fold. It never scores the final chosen lambda on the
    same outcomes that selected it. Bootstrap intervals are descriptive,
    conditional on the recorded forecasts and the documented resampling.
    """
    records = data.get("records")
    if not isinstance(records, list):
        raise ValueError("records must be an array of {p, q, y, timestamp?}")
    if any(not isinstance(row, dict) for row in records):
        raise ValueError("Each prediction record must be an object")
    p = np.array([probability(row.get("p"), "historical p")
                  for row in records])
    q = np.array([probability(row.get("q"), "historical q")
                  for row in records])
    y = np.array([number(row.get("y"), "y", 0, 1) for row in records])
    if not np.all((y == 0) | (y == 1)):
        raise ValueError("Historical y must be 0 or 1")
    has_times = ["timestamp" in row for row in records]
    if any(has_times) and not all(has_times):
        raise ValueError("Supply timestamps for every record or none")
    times = (np.array([timestamp(row["timestamp"]) for row in records])
             if any(has_times) else None)
    if times is not None:
        order = np.argsort(times, kind="stable")
        p, q, y, times = p[order], q[order], y[order], times[order]
    count = len(records)
    base: Json = {"schema_version": 1, "samples": count,
                  "reminders": REMINDERS}
    if count < 30:
        return {**base, "status": "insufficient_samples",
                "reason": "At least 30 settled predictions are required",
                "suggested_lambda_max": 0.5}
    seed_value = number(data.get("seed", 20261104), "seed", 0, 2 ** 32 - 1)
    if not seed_value.is_integer():
        raise ValueError("seed must be an integer")
    seed = int(seed_value)
    outer = splits(times, count, seed)
    if times is not None and len(outer) < 5:
        raise ValueError("Rolling calibration needs at least 6 distinct times")
    log_loss, brier = losses(p, q, y)
    selected_rows = selection_rows(times, count, seed)
    final_index = best_index(log_loss[selected_rows])
    all_log, all_brier, all_test = [], [], []
    folds = []
    for training, test in outer:
        training_times = times[training] if times is not None else None
        inner_rows = selection_rows(training_times, len(training), seed)
        chosen = best_index(log_loss[training[inner_rows]])
        indices = [0, chosen, 100]
        all_log.append(log_loss[test][:, indices])
        all_brier.append(brier[test][:, indices])
        all_test.extend(test.tolist())
        fold: Json = {"training_samples": len(training),
                      "test_samples": len(test),
                      "lambda_hat": float(GRID[chosen])}
        if times is not None:
            fold.update({
                "training_end": datetime.fromtimestamp(
                    float(times[training].max()), timezone.utc).isoformat(),
                "test_start": datetime.fromtimestamp(
                    float(times[test].min()), timezone.utc).isoformat(),
            })
        folds.append(fold)
    order = np.argsort(all_test)
    evaluated_log = np.concatenate(all_log)[order]
    evaluated_brier = np.concatenate(all_brier)[order]
    labels = ("baseline", "calibrated", "user")
    scores = {
        label: {"log_loss": float(evaluated_log[:, i].mean()),
                "brier": float(evaluated_brier[:, i].mean())}
        for i, label in enumerate(labels)
    }
    rng = np.random.default_rng(seed)
    sampled_lambdas = []
    sampled_scores = []
    for _ in range(BOOTSTRAPS):
        rows = resample(rng, len(selected_rows), times is not None)
        index = best_index(log_loss[selected_rows[rows]])
        sampled_lambdas.append(float(GRID[index]))
        evaluation_rows = resample(rng, len(evaluated_log), times is not None)
        sampled_scores.append(np.stack([
            evaluated_log[evaluation_rows].mean(axis=0),
            evaluated_brier[evaluation_rows].mean(axis=0),
        ]))
    interval = np.quantile(sampled_lambdas, [0.05, 0.95]).tolist()
    score_intervals = np.quantile(sampled_scores, [0.05, 0.95], axis=0)
    for i, label in enumerate(labels):
        scores[label]["log_loss_interval_90"] = (
            score_intervals[:, 0, i].tolist())
        scores[label]["brier_interval_90"] = score_intervals[:, 1, i].tolist()
    return {
        **base, "status": "ok", "lambda_hat": float(GRID[final_index]),
        "evaluation": "nested_rolling_origin" if times is not None
        else "nested_5_fold", "evaluated_samples": len(evaluated_log),
        "selection_samples": len(selected_rows), "folds": folds,
        "scores": scores,
        "score_interpretation": (
            "calibrated scores use fold-specific training-selected lambda; "
            "final lambda_hat is for future forecasts, not a held-out score"),
        "bootstrap": {
            "replicates": BOOTSTRAPS, "interval_90": interval,
            "method": "moving_block" if times is not None else "iid_rows",
            "seed": seed,
            "scope": "lambda selection and conditional score intervals",
        },
        "interpretation": {
            "near_zero": float(GRID[final_index]) <= 0.1,
            "wide_interval": interval[1] - interval[0] >= 0.5,
            "near_zero_meaning": (
                "No demonstrated predictive information beyond the baseline; "
                "sampling noise and calibration can also affect this result"),
            "wide_interval_meaning": "More records needed to assess skill",
        },
        "warnings": [
            "Use predictions recorded before outcomes were known",
            "Group repeated or related events before calibration",
            "Intervals do not guarantee future forecasting skill",
        ],
    }
