"""Unit tests for the pure aggregation rules."""

from __future__ import annotations

from fractions import Fraction

import pytest

from app.aggregate import (
    HistogramInput,
    ValidationProblem,
    compute_fingerprint,
    evaluate_p95,
    merge_histograms,
    percentiles,
    quantile_interval,
    report_fingerprint_payload,
    to_utc_iso,
    validate_histogram,
)
from datetime import datetime, timezone, timedelta


def test_histogram_requires_len_plus_one():
    with pytest.raises(ValidationProblem, match="len"):
        validate_histogram([100, 200], [1, 2], 3, 10.0)


def test_histogram_count_must_equal_sum_no_missing_as_zero():
    with pytest.raises(ValidationProblem, match="缺桶不能按零处理"):
        validate_histogram([100, 200], [1, 2, 0], 10, 10.0)


def test_histogram_boundaries_strictly_increasing():
    with pytest.raises(ValidationProblem, match="严格递增"):
        validate_histogram([100, 100], [1, 1, 0], 2, 1.0)


def test_histogram_rejects_negative_boundary():
    with pytest.raises(ValidationProblem, match="不得为负"):
        validate_histogram([-1], [0, 1], 1, 1.0)


def test_utc_normalization_equates_offsets():
    a = to_utc_iso(datetime(2026, 11, 22, 8, 0, tzinfo=timezone(timedelta(hours=8))))
    b = to_utc_iso(datetime(2026, 11, 22, 0, 0, tzinfo=timezone.utc))
    assert a == b == "2026-11-22T00:00:00.000000Z"


def test_fingerprint_field_order_and_number_format_invariant():
    base = report_fingerprint_payload(
        service="s",
        instance="i",
        window_start="w0",
        window_end="w1",
        source_batch="b",
        boundaries=[100, 200],
        counts=[1, 2],
        total_count=3,
        total_sum_us=1.0,
        collected_at=None,
    )
    assert compute_fingerprint(base) == compute_fingerprint(dict(reversed(list(base.items()))))
    float_form = report_fingerprint_payload(
        service="s",
        instance="i",
        window_start="w0",
        window_end="w1",
        source_batch="b",
        boundaries=[100.0, 200.0],
        counts=[1, 2],
        total_count=3,
        total_sum_us=1,
        collected_at=None,
    )
    assert compute_fingerprint(base) == compute_fingerprint(float_form)


def test_fingerprint_changes_with_source_batch():
    common = dict(
        service="s",
        instance="i",
        window_start="w0",
        window_end="w1",
        boundaries=[100],
        counts=[1, 1],
        total_count=2,
        total_sum_us=50.0,
        collected_at=None,
    )
    assert compute_fingerprint(
        report_fingerprint_payload(source_batch="a", **common)
    ) != compute_fingerprint(report_fingerprint_payload(source_batch="b", **common))


def test_identical_boundaries_merge_exactly():
    merged = merge_histograms(
        [
            HistogramInput((100.0, 200.0), (5, 5, 0), 10, 100.0),
            HistogramInput((100.0, 200.0), (1, 2, 3), 6, 300.0),
        ]
    )
    assert merged.boundaries == (100.0, 200.0)
    assert merged.bucket_count_bounds == ((6, 6), (7, 7), (3, 3))
    assert merged.total_count == 16
    assert merged.total_sum_us == 400.0
    # rank for p50 = ceil(0.5*16)=8 -> falls in (100,200]
    p50 = quantile_interval(merged, Fraction(1, 2))
    assert (p50["lower_us"], p50["upper_us"]) == (100.0, 200.0)


def test_misaligned_boundaries_keep_uncertainty_not_fake_precision():
    """A's (100,500] bucket straddles B's boundary at 200: must not be split."""
    merged = merge_histograms(
        [
            HistogramInput((100.0, 500.0), (10, 0, 0), 10, 0.0),
            HistogramInput((200.0,), (0, 10), 10, 0.0),
        ]
    )
    assert merged.boundaries == (100.0, 200.0, 500.0)
    # Only A's 10 observations are certainly <=100; <=200 can be at most 10.
    assert merged.cum_lower[0] == 10 and merged.cum_upper[0] == 10
    assert merged.cum_lower[1] == 10 and merged.cum_upper[1] == 10
    assert merged.cum_lower[2] == 10 and merged.cum_upper[2] == 20
    # Certain overflow bucket is 0, possible overflow is 10.
    assert merged.bucket_count_bounds[-1] == (0, 10)
    for lower, upper in merged.bucket_count_bounds:
        assert lower <= upper


def _sample_consistent(hist: HistogramInput, rng) -> list[float]:
    """Random exact latencies consistent with a reported histogram."""
    values: list[float] = []
    for index, count in enumerate(hist.counts):
        lower = 0.0 if index == 0 else hist.boundaries[index - 1]
        upper = hist.boundaries[index] if index < len(hist.boundaries) else None
        for _ in range(count):
            if upper is None:
                values.append(lower + rng.uniform(0.0, 1e6))
            else:
                # Include both endpoints so the enclosure must be closed correctly.
                pick = rng.random()
                if pick < 0.1:
                    values.append(lower)
                elif pick < 0.2:
                    values.append(upper)
                else:
                    values.append(rng.uniform(lower, upper))
    return values


def test_quantile_interval_always_contains_true_quantile():
    """Property: over random exact data consistent with both reports,
    the reported interval must enclose the exact nearest-rank quantile."""
    import random

    rng = random.Random(20261122)
    left = HistogramInput((100.0, 300.0), (2, 3, 0), 5, 0.0)
    right = HistogramInput((200.0,), (1, 4), 5, 0.0)
    merged = merge_histograms([left, right])
    intervals = percentiles(merged)
    assert merged.total_count == 10

    for _ in range(2000):
        values = sorted(_sample_consistent(left, rng) + _sample_consistent(right, rng))
        total = len(values)
        for name, q in (("p50", Fraction(1, 2)), ("p95", Fraction(19, 20)), ("p99", Fraction(99, 100))):
            rank = max(1, -(-q.numerator * total // q.denominator))
            true_value = values[rank - 1]
            interval = intervals[name]
            # Enclosure is closed [lower, upper] and guaranteed, not merely plausible.
            assert float(interval["lower_us"]) <= true_value, (name, true_value, interval)
            if interval["upper_us"] is not None:
                assert true_value <= float(interval["upper_us"]), (name, true_value, interval)


def test_quantile_null_upper_when_overflow_proven():
    merged = merge_histograms([HistogramInput((100.0,), (0, 10), 10, 0.0)])
    p95 = quantile_interval(merged, Fraction(19, 20))
    assert p95["upper_us"] is None
    assert p95["lower_us"] == 100.0


def test_empty_merge_has_no_quantiles():
    merged = merge_histograms([])
    assert merged.total_count == 0
    assert percentiles(merged) == {"p50": None, "p95": None, "p99": None}


def test_merged_count_is_exact_when_one_instance():
    merged = merge_histograms([HistogramInput((10.0, 20.0), (1, 2, 3), 6, 90.0)])
    assert merged.bucket_count_bounds == ((1, 1), (2, 2), (3, 3))


@pytest.mark.parametrize(
    "interval,target,expected",
    [
        ({"lower_us": 100, "upper_us": 200}, 250, "meets"),
        ({"lower_us": 100, "upper_us": 200}, 200, "meets"),
        ({"lower_us": 300, "upper_us": 400}, 250, "misses"),
        ({"lower_us": 250, "upper_us": 400}, 250, "pending_review"),
        ({"lower_us": 200, "upper_us": 300}, 250, "pending_review"),
        ({"lower_us": 200, "upper_us": None}, 250, "pending_review"),
        ({"lower_us": 300, "upper_us": None}, 250, "misses"),
        (None, 250, "pending_review"),
    ],
)
def test_evaluate_p95_matrix(interval, target, expected):
    assert evaluate_p95(interval, target)["status"] == expected


def test_evaluate_p95_without_target_is_pending():
    assert evaluate_p95({"lower_us": 1, "upper_us": 2}, None)["status"] == "pending_review"
