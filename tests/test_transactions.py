"""Transaction-level tests: no publish ever reads a half-consistent report set."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from app.models import PublishIn
from app.models import ReportIn
from app.service import ingest_report, publish


def _report(instance: str):
    return ReportIn(
        service="svc",
        instance=instance,
        window_start="2026-11-22T00:00:00Z",
        window_end="2026-11-22T00:05:00Z",
        p95_target_us=250000,
        source_batch="b1",
        bucket_boundaries=[100000, 200000],
        bucket_counts=[5, 5, 0],
        total_count=10,
        total_sum_us=1000000.0,
        collected_at="2026-11-22T00:06:00Z",
    )


PUBLISH_REQUEST = dict(
    service="svc",
    window_start="2026-11-22T00:00:00Z",
    window_end="2026-11-22T00:05:00Z",
    p95_target_us=250000,
    expected_instances=["i1", "i2", "i3", "i4"],
    deadline="2026-11-30T00:00:00Z",
)


def test_publish_snapshot_is_internally_consistent_under_concurrent_ingest(store, fake_clock):
    """Every published total_count must equal 10 * instance_count.

    Each ingest inserts exactly one complete report (10 observations) in its own
    transaction.  If a publish could observe a partially written report, or a
    counts/total mismatch, this invariant would break.
    """
    request = PublishIn(**PUBLISH_REQUEST)
    errors: list[str] = []
    stop = threading.Event()

    def writer() -> None:
        for index in range(40):
            ingest_report(store, _report(f"w{index}"))

    def reader() -> None:
        while not stop.is_set():
            result = publish(store, request)
            if result["total_count"] != 10 * result["instance_count"]:
                errors.append(f"{result['total_count']} != 10*{result['instance_count']}")
            if result["merged_bucket_count_ranges"]:
                for lower_upper in result["merged_bucket_count_ranges"]:
                    if lower_upper["lower"] > lower_upper["upper"]:
                        errors.append(f"inverted range {lower_upper}")

    with ThreadPoolExecutor(max_workers=3) as pool:
        readers = [pool.submit(reader) for _ in range(2)]
        writer_future = pool.submit(writer)
        writer_future.result()
        stop.set()
        for future in readers:
            future.result()

    assert not errors, errors[:5]
    final = publish(store, request)
    assert final["total_count"] == 10 * final["instance_count"]


def test_repeated_publish_is_byte_identical_for_same_deadline(store, fake_clock):
    ingest_report(store, _report("i1"))
    ingest_report(store, _report("i2"))
    request = PublishIn(**{**PUBLISH_REQUEST, "expected_instances": ["i1", "i2"]})
    first = publish(store, request)
    second = publish(store, request)
    assert first["publish_id"] + 1 == second["publish_id"]
    for key in (
        "total_count",
        "total_sum_us",
        "common_bucket_boundaries",
        "merged_bucket_count_ranges",
        "p50",
        "p95",
        "p99",
        "status",
        "missing_instances",
        "reports_used",
        "created_at",
    ):
        assert first[key] == second[key], key
