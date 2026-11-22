"""Tests for p95 target version registration and publish-time selection."""

from __future__ import annotations

from tests.conftest import WINDOW, register_target, report_payload, target_payload

PUBLISH = {
    **WINDOW,
    "expected_instances": ["checkout-1", "checkout-2"],
    "deadline": "2026-11-22T00:07:00Z",
}


def post_report(client, **overrides):
    return client.post("/api/v1/reports", json=report_payload(**overrides))


def test_register_target_creates_version(client):
    response = register_target(client, version="fresh-v1", p95_target_us=120000)
    assert response.status_code == 201
    body = response.json()
    assert body["duplicate"] is False
    assert body["target_id"] == 2  # id 1 belongs to the autouse default target
    assert body["service"] == "checkout-api"
    assert body["version"] == "fresh-v1"
    assert body["p95_target_us"] == 120000
    assert body["effective_at"] == "2026-11-22T00:00:00.000000Z"
    assert body["registered_at"].endswith("Z")
    assert len(body["fingerprint"]) == 64


def test_identical_resend_returns_original_version(client):
    first = register_target(client).json()
    second = register_target(client)
    assert second.status_code == 200
    body = second.json()
    assert body["duplicate"] is True
    assert body["target_id"] == first["target_id"]
    assert body["fingerprint"] == first["fingerprint"]
    assert body["registered_at"] == first["registered_at"]

    # Different field order and 250000 vs 250000.0 must still be identical.
    reordered = {
        "effective_at": "2026-11-22T00:00:00Z",
        "p95_target_us": 250000.0,
        "version": "target-v1",
        "service": "checkout-api",
    }
    again = client.post("/api/v1/p95-targets", json=reordered)
    assert again.status_code == 200
    assert again.json()["duplicate"] is True

    listed = client.get("/api/v1/p95-targets", params={"service": "checkout-api"}).json()
    assert len(listed) == 1


def test_same_version_with_changed_target_conflicts_and_remains_immutable(client):
    first = register_target(client).json()
    conflict = register_target(client, p95_target_us=200000)
    assert conflict.status_code == 409
    body = conflict.json()
    assert body["error"] == "conflicting_p95_target_version"
    assert body["existing_fingerprint"] == first["fingerprint"]

    listed = client.get("/api/v1/p95-targets", params={"service": "checkout-api"}).json()
    assert len(listed) == 1
    assert listed[0]["p95_target_us"] == 250000
    assert listed[0]["fingerprint"] == first["fingerprint"]


def test_same_version_with_changed_effective_time_conflicts(client):
    register_target(client)
    conflict = register_target(client, effective_at="2026-11-22T00:01:00Z")
    assert conflict.status_code == 409
    listed = client.get("/api/v1/p95-targets", params={"service": "checkout-api"}).json()
    assert listed[0]["effective_at"] == "2026-11-22T00:00:00.000000Z"


def test_target_validation_failures(client):
    naive = client.post(
        "/api/v1/p95-targets",
        json=target_payload(effective_at="2026-11-22T00:00:00"),
    )
    assert naive.status_code == 422

    negative = client.post(
        "/api/v1/p95-targets",
        json=target_payload(version="neg", p95_target_us=-1),
    )
    assert negative.status_code == 422

    blank = client.post(
        "/api/v1/p95-targets",
        json=target_payload(version="   "),
    )
    assert blank.status_code == 422

    extra = client.post(
        "/api/v1/p95-targets",
        json=target_payload(version="extra", unexpected=1),
    )
    assert extra.status_code == 422


def test_same_version_string_is_scoped_per_service(client):
    first = register_target(client, version="shared-label", p95_target_us=250000)
    second = register_target(
        client, service="billing-api", version="shared-label", p95_target_us=300000
    )
    assert first.status_code == 201 and second.status_code == 201
    rows = client.get("/api/v1/p95-targets").json()
    services = {row["service"] for row in rows}
    assert {"checkout-api", "billing-api"} <= services
    billing = [row for row in rows if row["service"] == "billing-api"]
    assert billing[0]["p95_target_us"] == 300000
    checkout = [row for row in rows if row["version"] == "shared-label" and row["service"] == "checkout-api"]
    assert checkout[0]["p95_target_us"] == 250000


def test_publish_picks_latest_effective_version_before_window_end(client, fake_clock):
    """Interleaved registration/publish: every publish snapshots the then-last version."""
    register_target(
        client,
        version="v1",
        p95_target_us=500000,
        effective_at="2026-11-21T23:00:00Z",
    )
    register_target(
        client,
        version="v2",
        p95_target_us=400000,
        effective_at="2026-11-22T00:02:00Z",
    )
    register_target(
        client,
        version="v3",
        p95_target_us=300000,
        effective_at="2026-11-22T00:04:00Z",
    )

    post_report(client, instance="checkout-1")
    post_report(client, instance="checkout-2")

    first = client.post("/api/v1/publish", json=PUBLISH).json()
    assert first["p95_target_version"] == "v3"
    assert first["p95_target_us"] == 300000
    assert first["p95_target"]["effective_at"] == "2026-11-22T00:04:00.000000Z"

    # Register a newer version after the first review; the next publish must use it.
    register_target(
        client,
        version="v4",
        p95_target_us=250000,
        effective_at="2026-11-22T00:04:30Z",
    )
    second = client.post(
        "/api/v1/publish",
        json={**PUBLISH, "deadline": "2026-11-22T00:09:00Z"},
    ).json()
    assert second["p95_target_version"] == "v4"
    assert second["p95_target_us"] == 250000

    # The earlier snapshot still points at the version used at that time.
    old = client.get(f"/api/v1/published/{first['publish_id']}").json()
    assert old["p95_target_version"] == "v3"
    assert old["p95_target_us"] == 300000
    assert old["p95_evaluation"]["target_us"] == 300000


def test_version_effective_exactly_at_window_end_is_eligible(client):
    register_target(
        client,
        version="at-end",
        p95_target_us=123000,
        effective_at="2026-11-22T00:05:00Z",
    )
    post_report(client, instance="checkout-1")
    post_report(client, instance="checkout-2")
    body = client.post("/api/v1/publish", json=PUBLISH)
    assert body.status_code == 201
    assert body.json()["p95_target_version"] == "at-end"


def test_only_versions_effective_before_window_end_are_candidates(client):
    # Default target-v1 is effective at the window start; future versions lose.
    register_target(
        client,
        version="future-a",
        p95_target_us=100000,
        effective_at="2026-11-22T00:06:00Z",
    )
    register_target(
        client,
        version="future-b",
        p95_target_us=90000,
        effective_at="2026-11-23T00:00:00Z",
    )
    post_report(client, instance="checkout-1")
    post_report(client, instance="checkout-2")
    body = client.post("/api/v1/publish", json=PUBLISH)
    assert body.status_code == 201
    assert body.json()["p95_target_version"] == "target-v1"


def test_same_effective_time_resolves_to_most_recent_registration(client):
    register_target(
        client,
        version="same-time-a",
        p95_target_us=500000,
        effective_at="2026-11-22T00:03:00Z",
    )
    register_target(
        client,
        version="same-time-b",
        p95_target_us=250000,
        effective_at="2026-11-22T00:03:00Z",
    )
    post_report(client, instance="checkout-1")
    post_report(client, instance="checkout-2")
    body = client.post("/api/v1/publish", json=PUBLISH).json()
    assert body["p95_target_version"] == "same-time-b"
    assert body["p95_target_us"] == 250000


def test_publish_without_any_registered_target_is_rejected(client):
    response = client.post(
        "/api/v1/publish",
        json={
            "service": "brand-new-api",
            "window_start": WINDOW["window_start"],
            "window_end": WINDOW["window_end"],
            "expected_instances": ["x-1"],
            "deadline": PUBLISH["deadline"],
        },
    )
    assert response.status_code == 409
    assert response.json()["error"] == "no_eligible_p95_target_version"
    assert client.get("/api/v1/published").json() == []


def test_targets_are_listed_newest_first_and_filterable(client):
    register_target(client, version="list-v1", p95_target_us=1)
    register_target(client, version="list-v2", p95_target_us=2)
    register_target(client, service="other-api", version="other-v1", p95_target_us=3)

    rows = client.get("/api/v1/p95-targets", params={"service": "checkout-api"}).json()
    assert [row["version"] for row in rows] == ["list-v2", "list-v1", "target-v1"]
    all_rows = client.get("/api/v1/p95-targets").json()
    assert len(all_rows) == 4
    assert all(row["duplicate"] is False for row in all_rows)


def test_timezone_offsets_normalize_to_same_target_key(client):
    first = register_target(
        client,
        version="tz-v1",
        effective_at="2026-11-22T08:00:00+08:00",
    )
    assert first.status_code == 201
    # Same instant written in UTC must dedupe against the +08:00 registration.
    again = register_target(
        client,
        version="tz-v1",
        effective_at="2026-11-22T00:00:00Z",
    )
    assert again.status_code == 200
    assert again.json()["duplicate"] is True
    assert again.json()["effective_at"] == "2026-11-22T00:00:00.000000Z"
