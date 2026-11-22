from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import clock  # noqa: E402
from app.main import create_app  # noqa: E402
from app.store import Store  # noqa: E402


class FakeClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 11, 22, 0, 6, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self.current

    def set(self, value: datetime) -> None:
        self.current = value


@pytest.fixture
def fake_clock(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr(clock, "now", fake.now)
    return fake


@pytest.fixture
def store(tmp_path):
    db = Store(str(tmp_path / "test.db"))
    yield db
    db.close()


@pytest.fixture
def client(tmp_path, fake_clock):
    from fastapi.testclient import TestClient

    from app.config import Settings

    app = create_app(Settings(database_path=str(tmp_path / "api.db")))
    with TestClient(app) as test_client:
        test_client.fake_clock = fake_clock  # type: ignore[attr-defined]
        yield test_client


WINDOW = {
    "service": "checkout-api",
    "window_start": "2026-11-22T00:00:00Z",
    "window_end": "2026-11-22T00:05:00Z",
}


def report_payload(instance: str = "checkout-1", **overrides):
    payload = {
        **WINDOW,
        "instance": instance,
        "source_batch": "batch-1",
        "bucket_boundaries": [100000, 200000, 300000, 500000],
        "bucket_counts": [12, 30, 20, 8, 3],
        "total_count": 73,
        "total_sum_us": 14250000.0,
        "collected_at": "2026-11-22T00:06:10Z",
        "p95_target_us": 250000,
    }
    payload.update(overrides)
    return payload
