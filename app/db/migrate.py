"""Run `alembic upgrade head` at container start (SPEC §15).

On Postgres a session-level advisory lock makes sure two starting containers never
migrate at the same time.
"""

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import text

from app.db.session import get_engine

MIGRATIONS = Path(__file__).resolve().parent / "migrations"
LOCK_ID = 0x4B52_5549  # "KRUI"

log = logging.getLogger(__name__)


def alembic_config() -> Config:
    """Built in code so it works wherever the package is installed (no alembic.ini needed)."""
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS))
    cfg.set_main_option("path_separator", "os")
    return cfg


def upgrade_head() -> None:
    engine = get_engine()
    cfg = alembic_config()
    cfg.attributes["configure_logger"] = False
    with engine.connect() as conn:
        is_pg = conn.dialect.name == "postgresql"
        if is_pg:
            conn.execute(text("SELECT pg_advisory_lock(:id)"), {"id": LOCK_ID})
        try:
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, "head")
            conn.commit()
        finally:
            if is_pg:
                conn.execute(text("SELECT pg_advisory_unlock(:id)"), {"id": LOCK_ID})
                conn.commit()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    upgrade_head()
    log.info("migrations up to date")
