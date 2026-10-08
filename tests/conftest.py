import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import respx
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.models import Base

FIXTURES = Path(__file__).parent / "fixtures" / "ah"


@pytest.fixture
def sessions() -> Iterator[sessionmaker[Session]]:
    """In-memory SQLite with the full schema and foreign keys enforced."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    @event.listens_for(engine, "connect")
    def _fk(conn: Any, _: Any) -> None:
        conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    yield sessionmaker(engine, expire_on_commit=False)
    engine.dispose()


def load_fixture(name: str, *, synthetic: bool = True) -> Any:
    folder = FIXTURES / "synthetic" if synthetic else FIXTURES
    return json.loads((folder / f"{name}.json").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def ah_api() -> Iterator[respx.MockRouter]:
    """CLAUDE.md rule 2: any unmocked request to AH fails the test instead of going out."""
    with respx.mock(
        base_url="https://api.ah.nl", assert_all_mocked=True, assert_all_called=False
    ) as router:
        yield router
