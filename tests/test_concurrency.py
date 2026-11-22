"""Concurrency, timezone-normalization and persistence tests."""

from __future__ import annotations

import threading

from tests.conftest import report_payload


def test_equivalent_timezone_offsets_hit_same_idempotency_key(client):
    first = client.post("/api/v1/reports", json=report_payload())
    assert first.status_code == 201
    # Same instant expressed as +08:00 must dedupe, not conflict.
    shifted = report_payload(window_start="2026-11-22T08:00:00+08:00", window_end="2026-11-22T08:05:00+08:00")
    second = client.post("/api/v1/reports", json=shifted)
    assert second.status_code == 200
    assert second.json()["duplicate"] is True
    assert second.json()["window_start"] == "2026-11-22T00:00:00.000000Z"


def test_concurrent_identical_posts_insert_once(client):
    payload = report_payload(instance="race-1")
    statuses: list[int] = []
    lock = threading.Lock()

    def send() -> None:
        response = client.post("/api/v1/reports", json=payload)
        with lock:
            statuses.append(response.status_code)

    threads = [threading.Thread(target=send) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(statuses) == [200] * 7 + [201], statuses
    listed = client.get("/api/v1/reports", params={"service": "checkout-api"}).json()
    assert len(listed) == 1


def test_concurrent_publishes_get_unique_increasing_ids(client):
    register = client.post(
        "/api/v1/p95-targets",
        json={
            "service": "checkout-api",
            "version": "target-v1",
            "p95_target_us": 250000,
            "effective_at": "2026-11-22T00:00:00Z",
        },
    )
    # The autouse default fixture already registered the same version.
    assert register.status_code in (200, 201)
    for instance in ("checkout-1", "checkout-2"):
        client.post("/api/v1/reports", json=report_payload(instance=instance))
    body = {
        "service": "checkout-api",
        "window_start": "2026-11-22T00:00:00Z",
        "window_end": "2026-11-22T00:05:00Z",
        "expected_instances": ["checkout-1", "checkout-2"],
        "deadline": "2026-11-22T00:07:00Z",
    }
    ids: list[int] = []
    lock = threading.Lock()

    def publish() -> None:
        response = client.post("/api/v1/publish", json=body)
        assert response.status_code == 201
        with lock:
            ids.append(response.json()["publish_id"])

    threads = [threading.Thread(target=publish) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(set(ids)) == 6
    assert sorted(ids) == list(range(1, 7))


def test_state_survives_store_reopen(tmp_path):
    from fastapi.testclient import TestClient

    from app.config import Settings
    from app.main import create_app

    db_path = str(tmp_path / "persist.db")
    settings = Settings(database_path=db_path)
    with TestClient(create_app(settings)) as first_client:
        first_client.post("/api/v1/reports", json=report_payload())
        target = first_client.post(
            "/api/v1/p95-targets",
            json={
                "service": "checkout-api",
                "version": "target-v1",
                "p95_target_us": 250000,
                "effective_at": "2026-11-22T00:00:00Z",
            },
        )
        assert target.status_code == 201
        publish = first_client.post(
            "/api/v1/publish",
            json={
                "service": "checkout-api",
                "window_start": "2026-11-22T00:00:00Z",
                "window_end": "2026-11-22T00:05:00Z",
                "expected_instances": ["checkout-1"],
                "deadline": "2026-11-22T00:07:00Z",
            },
        )
        assert publish.status_code == 201
        snapshot = publish.json()
        assert snapshot["p95_target_version"] == "target-v1"

    with TestClient(create_app(settings)) as second_client:
        reloaded = second_client.get(f"/api/v1/published/{snapshot['publish_id']}")
        assert reloaded.status_code == 200
        assert reloaded.json()["created_at"] == snapshot["created_at"]
        assert reloaded.json()["total_count"] == snapshot["total_count"]
        assert reloaded.json()["p95_target_version"] == "target-v1"
        # Registered targets also survive a restart, and re-sending dedupes.
        again_target = second_client.post(
            "/api/v1/p95-targets",
            json={
                "service": "checkout-api",
                "version": "target-v1",
                "p95_target_us": 250000,
                "effective_at": "2026-11-22T00:00:00Z",
            },
        )
        assert again_target.status_code == 200
        assert again_target.json()["duplicate"] is True
        # Duplicate detection still works after restart.
        again = second_client.post("/api/v1/reports", json=report_payload())
        assert again.status_code == 200 and again.json()["duplicate"] is True
