from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import WorkerHeartbeat
from app.web import main


@pytest.fixture
def client(sessions: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(main, "session_factory", lambda: sessions)
    return TestClient(main.app)


def beat(sessions: sessionmaker[Session], age: timedelta) -> None:
    with sessions.begin() as s:
        s.merge(WorkerHeartbeat(name="worker", beat_at=datetime.now(UTC) - age, detail="x"))


def test_no_heartbeat_is_unhealthy(client: TestClient) -> None:
    r = client.get("/healthz")
    assert r.status_code == 503
    assert r.json()["db"] == "ok"


def test_fresh_heartbeat_is_healthy(client: TestClient, sessions: sessionmaker[Session]) -> None:
    beat(sessions, timedelta(minutes=1))
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_stale_heartbeat_is_unhealthy(client: TestClient, sessions: sessionmaker[Session]) -> None:
    beat(sessions, timedelta(minutes=11))
    assert client.get("/healthz").status_code == 503


def test_db_down_is_unhealthy(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken() -> None:
        raise RuntimeError("db down")

    monkeypatch.setattr(main, "session_factory", broken)
    r = TestClient(main.app).get("/healthz")
    assert r.status_code == 503 and "unreachable" in r.json()["db"]
