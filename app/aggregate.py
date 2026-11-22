"""Pure aggregation logic: normalization, fingerprinting, histogram merging, quantile bounds.

All latency values are **microseconds**.  Bucket boundaries are the inclusive
upper bounds of histogram buckets.  For boundaries ``[100, 500]`` the buckets are
``(0,100]``, ``(100,500]`` and ``(500,+inf)``, reported as three counts
(``len(boundaries) + 1``).

Why not simply bucket-merge over the union of boundaries?
---------------------------------------------------------
Instances may use different boundaries.  If instance A reports ``(100,500]`` and
instance B reports a boundary at ``200``, the union boundary ``200`` cuts
*through* A's bucket: A's count cannot be distributed between ``(100,200]`` and
``(200,500]``.  Assigning it wholesale to one side would fabricate precision and
could make the reported p95 interval **exclude** the true value.

Instead this module keeps, for every candidate boundary ``x``, the guaranteed
lower and upper bounds on the merged CDF:

* ``cum_lower(x)`` — observations that are *certainly* ``<= x``: every source
  bucket lying entirely inside ``(0, x]``.
* ``cum_upper(x)`` — observations that *could* be ``<= x``: every source bucket
  whose lower edge is below ``x`` (it may partially extend past ``x``).

Then, for nearest-rank ``k = ceil(q*C)``:

* the true quantile ``v`` is ``<= x`` whenever ``cum_lower(x) >= k``;
* ``v`` is ``> x`` whenever ``cum_upper(x) < k``.

The resulting ``(lower_us, upper_us]`` is guaranteed to contain ``v`` and is as
tight as the reported buckets allow.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from fractions import Fraction
from typing import Any, Iterable, Sequence

QUANTILES: tuple[tuple[str, Fraction], ...] = (
    ("p50", Fraction(1, 2)),
    ("p95", Fraction(19, 20)),
    ("p99", Fraction(99, 100)),
)

MAX_BOUNDARIES = 2000
MAX_COUNT = 10**15


class ValidationProblem(ValueError):
    """A business-rule validation failure (mapped to HTTP 422)."""


def to_utc_iso(value: datetime) -> str:
    """Normalize an aware datetime to canonical UTC ISO-8601 with ``Z``.

    Always uses microsecond precision so the same instant written with different
    UTC offsets produces an identical storage/hash key.
    """
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValidationProblem("时间必须是带时区的时刻（例如 2026-01-01T00:00:00Z）")
    utc = value.astimezone(timezone.utc)
    return utc.replace(tzinfo=None).isoformat(timespec="microseconds") + "Z"


def canonical_number(value: float | int) -> str:
    """Canonical decimal text for a number, immune to int/float formatting drift."""
    if isinstance(value, bool):  # pragma: no cover - pydantic rejects bools for numbers
        raise ValidationProblem("布尔值不是合法数值")
    if isinstance(value, int):
        return str(value)
    if not math.isfinite(value):
        raise ValidationProblem("数值必须有限")
    normalized = Decimal(repr(float(value))).normalize()
    return format(normalized, "f")


def canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def compute_fingerprint(payload: dict[str, Any]) -> str:
    """SHA-256 over the canonical business payload.

    The fingerprint deliberately excludes server-generated fields (receive time,
    database id) but includes every value the reporter supplied, including the
    source batch, collection timestamp and the instance's own p95 target.  Field
    order never matters, and numbers are canonicalized so ``1`` and ``1.0`` hash
    identically.
    """
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def report_fingerprint_payload(
    *,
    service: str,
    instance: str,
    window_start: str,
    window_end: str,
    source_batch: str,
    boundaries: Sequence[float],
    counts: Sequence[int],
    total_count: int,
    total_sum_us: float,
    collected_at: str | None,
    p95_target_us: float | None = None,
) -> dict[str, Any]:
    return {
        "service": service,
        "instance": instance,
        "window_start": window_start,
        "window_end": window_end,
        "source_batch": source_batch,
        "bucket_boundaries": [canonical_number(b) for b in boundaries],
        "bucket_counts": [int(c) for c in counts],
        "total_count": int(total_count),
        "total_sum_us": canonical_number(total_sum_us),
        "collected_at": collected_at,
        "p95_target_us": None if p95_target_us is None else canonical_number(p95_target_us),
    }


def validate_histogram(
    boundaries: Sequence[float],
    counts: Sequence[int],
    total_count: int,
    total_sum_us: float,
) -> None:
    """Enforce a *complete* histogram: missing buckets are never treated as zero."""
    if len(boundaries) > MAX_BOUNDARIES:
        raise ValidationProblem(f"桶边界最多 {MAX_BOUNDARIES} 个")
    previous: float | None = None
    for boundary in boundaries:
        if not math.isfinite(boundary):
            raise ValidationProblem("桶边界必须是有限数值")
        if boundary < 0:
            raise ValidationProblem("桶边界不得为负")
        if previous is not None and boundary <= previous:
            raise ValidationProblem("桶边界必须严格递增且不得重复")
        previous = boundary
    if len(counts) != len(boundaries) + 1:
        raise ValidationProblem(
            f"bucket_counts 长度必须为 len(bucket_boundaries)+1"
            f"（应为 {len(boundaries) + 1}，实际 {len(counts)}）"
        )
    for count in counts:
        if count < 0:
            raise ValidationProblem("bucket_counts 不得为负")
        if count > MAX_COUNT:
            raise ValidationProblem("bucket_counts 超出允许范围")
    if total_count < 0 or total_count > MAX_COUNT:
        raise ValidationProblem("total_count 超出允许范围")
    if not math.isfinite(total_sum_us) or total_sum_us < 0:
        raise ValidationProblem("total_sum_us 必须是非负有限数值")
    if total_count != sum(counts):
        raise ValidationProblem(
            "直方图不完整：total_count 必须等于 bucket_counts 之和，缺桶不能按零处理"
        )
    if total_count == 0 and total_sum_us != 0:
        raise ValidationProblem("total_count 为 0 时 total_sum_us 必须为 0")
    if total_count > 0 and total_sum_us == 0:
        raise ValidationProblem("total_count 大于 0 时 total_sum_us 不应为 0")


@dataclass(frozen=True)
class HistogramInput:
    boundaries: tuple[float, ...]
    counts: tuple[int, ...]
    total_count: int
    total_sum_us: float


@dataclass(frozen=True)
class MergedHistogram:
    """Merged view keeping rigorous CDF bounds instead of invented exact counts."""

    boundaries: tuple[float, ...]
    # For each bucket j (0 = (0,b0], ..., last = overflow): guaranteed [min,max] count.
    bucket_count_bounds: tuple[tuple[int, int], ...]
    # cum_lower[j] / cum_upper[j] bound the number of observations in (0, boundaries[j]].
    cum_lower: tuple[int, ...]
    cum_upper: tuple[int, ...]
    total_count: int
    total_sum_us: float
    instance_count: int


def display_number(value: float | int) -> float | int:
    """Render integral floats as ints for readable JSON."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, float):
        return round(value, 6)
    return value


def _bucket_edges(
    item: HistogramInput, index: int
) -> tuple[float, float | None]:
    """Return ``(lower_edge, upper_edge)`` of bucket ``index``; ``None`` means +inf."""
    lower = 0.0 if index == 0 else item.boundaries[index - 1]
    upper = item.boundaries[index] if index < len(item.boundaries) else None
    return lower, upper


def merge_histograms(reports: Iterable[HistogramInput]) -> MergedHistogram:
    """Merge per-instance histograms into rigorous CDF bounds.

    For every candidate boundary ``x`` (the sorted union of all instance
    boundaries) we compute:

    * ``cum_lower(x)``: sum of buckets whose *upper* edge is ``<= x`` — these
      observations are certainly at or below ``x``;
    * ``cum_upper(x)``: sum of buckets whose *lower* edge is ``< x`` — these
      observations *could* be at or below ``x``.

    A source bucket that straddles ``x`` therefore contributes to the uncertainty
    band rather than being attributed to one side.  Instances absent from the
    input are not zero-filled; the caller decides whether the set is complete.
    """
    items = list(reports)
    if not items:
        return MergedHistogram((), ((0, 0),), (), (), 0, 0.0, 0)

    total_count = sum(item.total_count for item in items)
    total_sum = sum(item.total_sum_us for item in items)
    common = sorted({b for item in items for b in item.boundaries})

    cum_lower: list[int] = []
    cum_upper: list[int] = []
    for x in common:
        certain = 0
        possible = 0
        for item in items:
            for index, count in enumerate(item.counts):
                if count == 0:
                    continue
                lower_edge, upper_edge = _bucket_edges(item, index)
                if upper_edge is not None and upper_edge <= x:
                    certain += count
                if lower_edge < x:
                    possible += count
        cum_lower.append(certain)
        cum_upper.append(possible)

    bounds: list[tuple[int, int]] = []
    for index in range(len(common)):
        previous_upper = cum_upper[index - 1] if index > 0 else 0
        previous_lower = cum_lower[index - 1] if index > 0 else 0
        bounds.append(
            (
                max(cum_lower[index] - previous_upper, 0),
                max(cum_upper[index] - previous_lower, 0),
            )
        )
    bounds.append(
        (
            max(total_count - cum_upper[-1], 0),
            max(total_count - cum_lower[-1], 0),
        )
    )

    return MergedHistogram(
        boundaries=tuple(common),
        bucket_count_bounds=tuple(bounds),
        cum_lower=tuple(cum_lower),
        cum_upper=tuple(cum_upper),
        total_count=total_count,
        total_sum_us=total_sum,
        instance_count=len(items),
    )


def quantile_interval(merged: MergedHistogram, q: Fraction) -> dict[str, Any] | None:
    """Return the guaranteed closed enclosure ``[lower_us, upper_us]`` of the q-quantile.

    Nearest-rank rule: ``k = ceil(q * C)``.  ``upper_us`` is ``None`` only when
    even the largest common boundary cannot be proven to reach rank ``k`` (the
    quantile lies in the unbounded overflow bucket).
    """
    total = merged.total_count
    if total <= 0:
        return None
    rank = max(math.ceil(q * total), 1)

    lower_us: float = 0.0
    for index, boundary in enumerate(merged.boundaries):
        if merged.cum_upper[index] < rank:
            lower_us = boundary
        else:
            break

    upper_us: float | None = None
    upper_index: int | None = None
    for index, boundary in enumerate(merged.boundaries):
        if merged.cum_lower[index] >= rank:
            upper_us = boundary
            upper_index = index
            break

    if upper_us is None:
        label = f"[{display_number(lower_us)}, +inf)"
        note = "分位数落在无上界的溢出桶，只能给出下界"
        bucket_index = len(merged.boundaries)
    else:
        label = f"[{display_number(lower_us)}, {display_number(upper_us)}]"
        note = "真实分位数必含于闭区间 [lower_us, upper_us]；由桶 CDF 保证"
        bucket_index = upper_index if upper_index is not None else 0

    return {
        "lower_us": display_number(lower_us),
        "upper_us": None if upper_us is None else display_number(upper_us),
        "bucket_index": bucket_index,
        "bucket_label": label,
        "rank": rank,
        "interval_note": note,
    }


def percentiles(merged: MergedHistogram) -> dict[str, dict[str, Any] | None]:
    return {name: quantile_interval(merged, q) for name, q in QUANTILES}


STATUS_LABELS = {
    "meets": "达标",
    "misses": "未达标",
    "pending_review": "待核",
}


def evaluate_p95(
    p95: dict[str, Any] | None,
    target_us: float | None,
) -> dict[str, Any]:
    """Decide meets / misses / pending_review from the guaranteed interval.

    The true p95 ``v`` satisfies ``lower_us <= v <= upper_us`` (a closed,
    guaranteed enclosure).

    * meets  ⇔ ``upper_us`` is finite and ``upper_us <= target`` (so ``v <= target``);
    * misses ⇔ ``lower_us > target`` (so ``v > target``);
    * otherwise pending_review: the target falls inside the interval, or the
      interval is unbounded, so the data cannot prove either outcome.
    """
    if target_us is None:
        return {
            "status": "pending_review",
            "status_label": STATUS_LABELS["pending_review"],
            "reason": "未提供 p95 目标",
        }
    if p95 is None:
        return {
            "status": "pending_review",
            "status_label": STATUS_LABELS["pending_review"],
            "reason": "窗口内没有观测数据，无法计算 p95",
            "target_us": display_number(target_us),
        }
    lower = float(p95["lower_us"])
    upper = p95["upper_us"]
    if lower > target_us:
        status, reason = (
            "misses",
            f"p95 必含区间下界 {display_number(lower)}µs 已高于目标 {display_number(target_us)}µs，判定未达标",
        )
    elif upper is not None and float(upper) <= target_us:
        status, reason = (
            "meets",
            f"p95 必含区间上界 {display_number(upper)}µs 不高于目标 {display_number(target_us)}µs，判定达标",
        )
    elif upper is None:
        status, reason = "pending_review", "p95 落在无上界的溢出桶，无法证明达标"
    else:
        status, reason = (
            "pending_review",
            f"目标 {display_number(target_us)}µs 落在 p95 必含区间 "
            f"[{display_number(lower)}, {display_number(upper)}] 内，现有桶粒度无法判定",
        )
    return {
        "status": status,
        "status_label": STATUS_LABELS[status],
        "reason": reason,
        "target_us": display_number(target_us),
    }
