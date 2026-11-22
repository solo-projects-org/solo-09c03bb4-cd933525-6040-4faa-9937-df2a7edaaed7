"""Business service layer: validation, idempotency, publication snapshots."""

from __future__ import annotations

import math
import sqlite3
from typing import Any

from . import clock
from .aggregate import (
    STATUS_LABELS,
    HistogramInput,
    ValidationProblem,
    compute_fingerprint,
    display_number,
    evaluate_p95,
    merge_histograms,
    percentiles,
    report_fingerprint_payload,
    to_utc_iso,
    validate_histogram,
)
from .models import PublishIn, ReportIn
from .store import ReportRow, Store


class ReportConflict(Exception):
    def __init__(self, existing: ReportRow):
        super().__init__("conflicting report")
        self.existing = existing


def _normalized_report(report: ReportIn) -> dict[str, Any]:
    window_start = to_utc_iso(report.window_start)
    window_end = to_utc_iso(report.window_end)
    if window_start >= window_end:
        raise ValidationProblem("window_start 必须早于 window_end")
    validate_histogram(
        report.bucket_boundaries,
        report.bucket_counts,
        report.total_count,
        report.total_sum_us,
    )
    collected_at = to_utc_iso(report.collected_at) if report.collected_at else None
    boundaries = [float(b) for b in report.bucket_boundaries]
    payload = report_fingerprint_payload(
        service=report.service,
        instance=report.instance,
        window_start=window_start,
        window_end=window_end,
        source_batch=report.source_batch,
        boundaries=boundaries,
        counts=report.bucket_counts,
        total_count=report.total_count,
        total_sum_us=report.total_sum_us,
        collected_at=collected_at,
        p95_target_us=report.p95_target_us,
    )
    return {
        "service": report.service,
        "instance": report.instance,
        "window_start": window_start,
        "window_end": window_end,
        "source_batch": report.source_batch,
        "fingerprint": compute_fingerprint(payload),
        "payload": payload,
        "boundaries": boundaries,
        "counts": [int(c) for c in report.bucket_counts],
        "total_count": int(report.total_count),
        "total_sum_us": float(report.total_sum_us),
        "collected_at": collected_at,
        "received_at": to_utc_iso(clock.now()),
    }


def ingest_report(store: Store, report: ReportIn) -> tuple[ReportRow, str]:
    normalized = _normalized_report(report)
    row, outcome = store.record_report(normalized)
    if outcome == "conflict":
        raise ReportConflict(row)
    return row, outcome


def _report_summary(row: ReportRow) -> dict[str, Any]:
    payload = row.payload
    return {
        "report_id": row.id,
        "instance": row.instance,
        "source_batch": row.source_batch,
        "received_at": row.received_at,
        "collected_at": row.collected_at,
        "fingerprint": row.fingerprint,
        "bucket_boundaries": [display_number(b) for b in row.boundaries],
        "bucket_counts": list(row.counts),
        "total_count": row.total_count,
        "total_sum_us": display_number(row.total_sum_us),
        "p95_target_us": payload.get("p95_target_us"),
    }


def _build_result(
    *,
    service: str,
    window_start: str,
    window_end: str,
    deadline: str,
    expected: list[str],
    p95_target_us: float | None,
    eligible: list[ReportRow],
    all_in_window: list[ReportRow],
    publish_id: int,
    created_at: str,
) -> dict[str, Any]:
    expected_set = set(expected)
    by_instance = {row.instance: row for row in eligible}
    reported = sorted(by_instance)
    missing = sorted(expected_set - set(by_instance))
    unexpected = sorted(set(by_instance) - expected_set)
    excluded_late = sorted({row.instance for row in all_in_window} - set(by_instance))
    used = [by_instance[name] for name in expected if name in by_instance]

    notes: list[str] = []
    if missing:
        notes.append(
            "截止时刻前未收到以下应到实例的上报，结果待核；缺失实例未按零计入合并："
            + ", ".join(missing)
        )
    if unexpected:
        notes.append("以下实例不在应到清单内，未纳入本次发布：" + ", ".join(unexpected))
    if excluded_late:
        notes.append(
            "以下实例在截止时刻之后才到达，未纳入本次发布，将进入新的统计批次："
            + ", ".join(excluded_late)
        )
    if not used:
        notes.append("截止时刻前没有任何有效上报，无法合并统计。")

    merged = merge_histograms(
        HistogramInput(
            boundaries=tuple(row.boundaries),
            counts=tuple(row.counts),
            total_count=row.total_count,
            total_sum_us=row.total_sum_us,
        )
        for row in used
    )
    quantiles = percentiles(merged)
    p95_evaluation = evaluate_p95(quantiles["p95"], p95_target_us)
    interval_status = p95_evaluation["status"]

    if missing:
        if interval_status != "pending_review":
            notes.append("尽管 p95 区间可判定，但应到实例未收齐，整体状态仍为待核。")
        status = "pending_review"
        p95_evaluation = {
            **p95_evaluation,
            "status": "pending_review",
            "status_label": STATUS_LABELS["pending_review"],
            "reason": (
                f"应到实例未收齐（缺 {len(missing)} 个），待核；"
                f"已收实例区间判定为“{STATUS_LABELS[interval_status]}”：{p95_evaluation['reason']}"
            ),
            "interval_status": interval_status,
        }
    else:
        status = interval_status
        p95_evaluation = {**p95_evaluation, "interval_status": interval_status}

    return {
        "publish_id": publish_id,
        "service": service,
        "window_start": window_start,
        "window_end": window_end,
        "deadline": deadline,
        "created_at": created_at,
        "expected_instances": expected,
        "reported_instances": reported,
        "missing_instances": missing,
        "unexpected_instances": unexpected,
        "excluded_late_instances": excluded_late,
        "source_batches": sorted({row.source_batch for row in used}),
        "common_bucket_boundaries": [display_number(b) for b in merged.boundaries],
        "merged_bucket_count_ranges": [
            {"lower": lower, "upper": upper} for lower, upper in merged.bucket_count_bounds
        ],
        "total_count": merged.total_count,
        "total_sum_us": display_number(merged.total_sum_us),
        "instance_count": merged.instance_count,
        "p50": quantiles["p50"],
        "p95": quantiles["p95"],
        "p99": quantiles["p99"],
        "p95_target_us": (
            None if p95_target_us is None else display_number(float(p95_target_us))
        ),
        "p95_evaluation": p95_evaluation,
        "status": status,
        "status_label": STATUS_LABELS[status],
        "notes": notes,
        "reports_used": [_report_summary(row) for row in used],
    }


def publish(store: Store, request: PublishIn) -> dict[str, Any]:
    window_start = to_utc_iso(request.window_start)
    window_end = to_utc_iso(request.window_end)
    deadline = to_utc_iso(request.deadline)
    if window_start >= window_end:
        raise ValidationProblem("window_start 必须早于 window_end")
    if request.p95_target_us is not None and not math.isfinite(request.p95_target_us):
        raise ValidationProblem("p95_target_us 必须是有限数值")

    expected = list(request.expected_instances)
    created_at = to_utc_iso(clock.now())

    def build(publish_id: int, conn: sqlite3.Connection) -> dict[str, Any]:
        # Reads run inside the write transaction: the snapshot cannot interleave
        # with a concurrent report insert.
        eligible = store.reports_for_window(
            request.service, window_start, window_end, received_until=deadline, conn=conn
        )
        all_in_window = store.reports_for_window(
            request.service, window_start, window_end, conn=conn
        )
        return _build_result(
            service=request.service,
            window_start=window_start,
            window_end=window_end,
            deadline=deadline,
            expected=expected,
            p95_target_us=request.p95_target_us,
            eligible=eligible,
            all_in_window=all_in_window,
            publish_id=publish_id,
            created_at=created_at,
        )

    _, result = store.insert_publish(
        {
            "service": request.service,
            "window_start": window_start,
            "window_end": window_end,
            "deadline": deadline,
            "p95_target_us": request.p95_target_us,
            "created_at": created_at,
        },
        build_result=build,
    )
    return result
