"""End-to-end API tests: ingest, idempotency, conflicts, publish, late batches."""

from __future__ import annotations

from tests.conftest import WINDOW, report_payload

PUBLISH = {
    **WINDOW,
    "p95_target_us": 250000,
    "expected_instances": ["checkout-1", "checkout-2"],
    "deadline": "2026-11-22T00:07:00Z",
}


def post_report(client, **overrides):
    return client.post("/api/v1/reports", json=report_payload(**overrides))


def test_health(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_openapi_available(client):
    spec = client.get("/openapi.json").json()
    assert spec["openapi"].startswith("3.")
    for path in ("/api/v1/reports", "/api/v1/publish", "/api/v1/published/{publish_id}"):
        assert path in spec["paths"]
    assert client.get("/docs").status_code == 200


def test_create_then_identical_resend_is_deduplicated(client):
    first = post_report(client)
    assert first.status_code == 201
    assert first.json()["duplicate"] is False

    second = post_report(client)
    assert second.status_code == 200
    assert second.json()["duplicate"] is True
    assert second.json()["report_id"] == first.json()["report_id"]

    listed = client.get("/api/v1/reports", params={"service": "checkout-api"}).json()
    assert len(listed) == 1


def test_resend_with_different_number_formatting_is_still_identical(client):
    post_report(client)
    resend = client.post(
        "/api/v1/reports",
        json=report_payload(bucket_boundaries=[100000.0, 200000.0, 300000.0, 500000.0], total_sum_us=14250000),
    )
    assert resend.status_code == 200
    assert resend.json()["duplicate"] is True


def test_conflicting_resend_is_rejected_and_original_preserved(client):
    first = post_report(client).json()
    conflict = client.post(
        "/api/v1/reports",
        json=report_payload(bucket_counts=[12, 30, 20, 9, 2], total_count=73),
    )
    assert conflict.status_code == 409
    body = conflict.json()
    assert body["error"] == "conflicting_report"
    assert body["existing_fingerprint"] == first["fingerprint"]

    listed = client.get("/api/v1/reports", params={"service": "checkout-api"}).json()
    assert len(listed) == 1
    assert listed[0]["bucket_counts"] == [12, 30, 20, 8, 3]


def test_conflict_when_only_source_batch_changes(client):
    post_report(client)
    conflict = post_report(client, source_batch="batch-2")
    assert conflict.status_code == 409


def test_reject_incomplete_histogram_missing_bucket_as_zero(client):
    # One bucket short of len(boundaries)+1: the missing bucket must NOT be filled with 0.
    response = post_report(client, bucket_counts=[12, 30, 20, 8], total_count=73)
    assert response.status_code == 422
    assert "bucket_counts 长度必须为 len(bucket_boundaries)+1" in response.json()["detail"]

    # Full-length counts whose sum disagrees with total_count are also rejected.
    mismatch = post_report(client, bucket_counts=[12, 30, 20, 8, 0], total_count=73)
    assert mismatch.status_code == 422
    assert "缺桶不能按零处理" in mismatch.json()["detail"]


def test_reject_count_mismatch(client):
    response = post_report(client, total_count=70)
    assert response.status_code == 422


def test_reject_naive_datetime(client):
    response = post_report(client, window_start="2026-11-22T00:00:00")
    assert response.status_code == 422


def test_reject_non_increasing_boundaries(client):
    response = post_report(client, bucket_boundaries=[100000, 100000], bucket_counts=[1, 1, 0], total_count=2)
    assert response.status_code == 422


def test_reject_unknown_field(client):
    response = post_report(client, bucket_counts_typo=[1])
    assert response.status_code == 422


def test_publish_only_after_deadline_reports(client):
    post_report(client, instance="checkout-1")
    # checkout-2 has not reported yet.
    response = client.post("/api/v1/publish", json=PUBLISH)
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "pending_review"
    assert body["missing_instances"] == ["checkout-2"]
    assert body["total_count"] == 73  # missing instance NOT counted as zero
    assert body["instance_count"] == 1


def test_publish_meets_when_interval_below_target(client):
    post_report(client, instance="checkout-1", bucket_boundaries=[100000], bucket_counts=[10, 0], total_count=10)
    post_report(client, instance="checkout-2", bucket_boundaries=[100000], bucket_counts=[10, 0], total_count=10)
    body = client.post("/api/v1/publish", json={**PUBLISH, "p95_target_us": 150000}).json()
    assert body["p95"]["lower_us"] == 0
    assert body["p95"]["upper_us"] == 100000
    assert body["status"] == "meets"
    assert body["status_label"] == "达标"


def test_publish_misses_when_interval_above_target(client):
    for instance in ("checkout-1", "checkout-2"):
        post_report(
            client,
            instance=instance,
            bucket_boundaries=[100000, 300000],
            bucket_counts=[0, 10, 0],
            total_count=10,
            total_sum_us=2000000.0,
        )
    body = client.post("/api/v1/publish", json={**PUBLISH, "p95_target_us": 150000}).json()
    # All observations are in (100000,300000]; rank 19/20 still lands there for each instance.
    assert body["p95"]["lower_us"] == 100000
    assert body["p95"]["upper_us"] == 300000
    # Target lies inside the interval: cannot prove either outcome.
    assert body["status"] == "pending_review"

    below = client.post("/api/v1/publish", json={**PUBLISH, "p95_target_us": 50000}).json()
    assert below["status"] == "misses"
    assert below["status_label"] == "未达标"


def test_publish_misses_when_lower_bound_exceeds_target(client):
    for instance in ("checkout-1", "checkout-2"):
        post_report(client, instance=instance)
    # Default histogram: p95 rank 139/146 falls in (300000,500000].
    body = client.post("/api/v1/publish", json={**PUBLISH, "p95_target_us": 250000}).json()
    assert body["p95"]["lower_us"] == 300000
    assert body["p95"]["upper_us"] == 500000
    assert body["status"] == "misses"


def test_publish_pending_when_target_inside_interval(client):
    for instance in ("checkout-1", "checkout-2"):
        post_report(client, instance=instance)
    body = client.post("/api/v1/publish", json={**PUBLISH, "p95_target_us": 400000}).json()
    assert body["p95"]["lower_us"] == 300000
    assert body["p95"]["upper_us"] == 500000
    assert body["status"] == "pending_review"
    assert body["p95_evaluation"]["interval_status"] == "pending_review"


def test_publish_pending_when_p95_in_overflow_bucket(client):
    for instance in ("checkout-1", "checkout-2"):
        post_report(
            client,
            instance=instance,
            bucket_boundaries=[100000],
            bucket_counts=[0, 10],
            total_count=10,
            total_sum_us=5000000.0,
        )
    body = client.post("/api/v1/publish", json={**PUBLISH, "p95_target_us": 10**9}).json()
    assert body["p95"]["upper_us"] is None
    assert body["status"] == "pending_review"


def test_missing_instances_force_pending_even_if_interval_meets(client):
    post_report(
        client,
        instance="checkout-1",
        bucket_boundaries=[100000],
        bucket_counts=[10, 0],
        total_count=10,
    )
    body = client.post("/api/v1/publish", json={**PUBLISH, "p95_target_us": 150000}).json()
    assert body["status"] == "pending_review"
    assert body["p95_evaluation"]["interval_status"] == "meets"
    assert "checkout-2" in body["missing_instances"]


def test_late_instance_goes_to_new_batch_and_old_snapshot_is_queryable(client, fake_clock):
    fake_clock.set(fake_clock.current.replace(hour=0, minute=5, second=0))
    post_report(client, instance="checkout-1")
    first = client.post("/api/v1/publish", json=PUBLISH).json()
    assert first["publish_id"] == 1
    assert first["missing_instances"] == ["checkout-2"]
    assert first["excluded_late_instances"] == []

    # checkout-2 reports after the first deadline.
    late = post_report(client, instance="checkout-2", source_batch="batch-late")
    assert late.status_code == 201

    second = client.post("/api/v1/publish", json={**PUBLISH, "deadline": "2026-11-22T00:09:00Z"}).json()
    assert second["publish_id"] == 2
    assert second["missing_instances"] == []
    assert second["instance_count"] == 2
    assert second["status"] != "pending_review" or second["p95"] is not None
    assert "batch-late" in second["source_batches"]

    # The earlier snapshot must be unchanged and retrievable by id.
    old = client.get("/api/v1/published/1").json()
    assert old["publish_id"] == 1
    assert old["missing_instances"] == ["checkout-2"]
    assert old["total_count"] == 73
    assert old["created_at"] == first["created_at"]
    assert old["reports_used"] == first["reports_used"]

    history = client.get("/api/v1/published").json()
    assert [row["publish_id"] for row in history] == [2, 1]


def test_deadline_excludes_reports_received_after_it(client, fake_clock):
    fake_clock.set(fake_clock.current.replace(hour=0, minute=10, second=0))
    post_report(client, instance="checkout-1")
    body = client.post(
        "/api/v1/publish",
        json={**PUBLISH, "expected_instances": ["checkout-1"], "deadline": "2026-11-22T00:05:00Z"},
    ).json()
    assert body["missing_instances"] == ["checkout-1"]
    assert body["excluded_late_instances"] == ["checkout-1"]
    assert body["total_count"] == 0


def test_unexpected_instance_is_reported_not_merged(client):
    post_report(client, instance="checkout-1")
    post_report(client, instance="checkout-2")
    post_report(client, instance="stray-9")
    body = client.post("/api/v1/publish", json=PUBLISH).json()
    assert body["unexpected_instances"] == ["stray-9"]
    assert body["instance_count"] == 2
    assert body["total_count"] == 146


def test_different_instances_can_use_different_boundaries(client):
    post_report(client, instance="checkout-1", bucket_boundaries=[100000, 200000], bucket_counts=[5, 5, 0], total_count=10)
    post_report(client, instance="checkout-2", bucket_boundaries=[150000, 250000], bucket_counts=[3, 4, 3], total_count=10)
    body = client.post(
        "/api/v1/publish",
        json={**PUBLISH, "expected_instances": ["checkout-1", "checkout-2"], "p95_target_us": 100000},
    ).json()
    assert body["common_bucket_boundaries"] == [100000, 150000, 200000, 250000]
    # rank 10: at most 8 observations are provably <=100000, so p50 is above it.
    assert body["p50"]["lower_us"] == 100000
    assert body["p50"]["upper_us"] == 200000
    # rank 19 exceeds every finite boundary, so p95 is only lower-bounded.
    assert body["p95"]["lower_us"] == 250000
    assert body["p95"]["upper_us"] is None
    assert body["status"] == "misses"


def test_publish_rejects_invalid_window_and_duplicates(client):
    bad_window = client.post(
        "/api/v1/publish",
        json={**PUBLISH, "window_start": "2026-11-22T00:05:00Z", "window_end": "2026-11-22T00:00:00Z"},
    )
    assert bad_window.status_code == 422
    duplicate_expected = client.post(
        "/api/v1/publish", json={**PUBLISH, "expected_instances": ["a", "a"]}
    )
    assert duplicate_expected.status_code == 422
    empty_expected = client.post("/api/v1/publish", json={**PUBLISH, "expected_instances": []})
    assert empty_expected.status_code == 422


def test_no_target_yields_pending(client):
    post_report(client, instance="checkout-1")
    post_report(client, instance="checkout-2")
    body = client.post("/api/v1/publish", json={**PUBLISH, "p95_target_us": None}).json()
    assert body["p95"] is not None
    assert body["status"] == "pending_review"
    assert body["p95_evaluation"]["reason"] == "未提供 p95 目标"


def test_published_not_found(client):
    assert client.get("/api/v1/published/999").status_code == 404


def test_report_queries_filter_by_batch(client):
    post_report(client, instance="checkout-1", source_batch="b1")
    post_report(client, instance="checkout-2", source_batch="b2")
    assert len(client.get("/api/v1/reports", params={"source_batch": "b2"}).json()) == 1
