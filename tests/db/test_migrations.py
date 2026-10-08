from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, inspect

from app.db.migrate import alembic_config
from app.db.models import Base


def _run(url: str, fn: str, target: str) -> None:
    engine = create_engine(url)
    cfg = alembic_config()
    cfg.attributes["configure_logger"] = False
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        getattr(command, fn)(cfg, target)
    engine.dispose()


def test_upgrade_matches_models_and_downgrade_is_clean(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'm.db').as_posix()}"
    _run(url, "upgrade", "head")

    engine = create_engine(url)
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == [], f"models and migrations differ: {diff}"
    tables = set(inspect(engine).get_table_names())
    assert {"household", "purchase", "price_observation", "action_log"} <= tables
    engine.dispose()

    _run(url, "downgrade", "base")
    engine = create_engine(url)
    assert set(inspect(engine).get_table_names()) <= {"alembic_version"}
    engine.dispose()

    _run(url, "upgrade", "head")  # and back up again
