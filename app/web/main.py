"""FastAPI app (`uvicorn app.web.main:app`). The UI routes follow in the next fase-1 step."""

from datetime import UTC, datetime, timedelta

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import select, text

from app.db.models import WorkerHeartbeat
from app.db.session import session_factory

HEARTBEAT_MAX_AGE = timedelta(minutes=10)

app = FastAPI(title="Kruidenier")


@app.get("/healthz")
def healthz() -> JSONResponse:
    """SPEC §15: DB reachable and last worker heartbeat < 10 min."""
    try:
        with session_factory()() as s:
            s.execute(text("SELECT 1"))
            beat = s.scalar(select(WorkerHeartbeat).where(WorkerHeartbeat.name == "worker"))
    except Exception as e:  # health endpoint must answer, whatever is broken
        return JSONResponse({"status": "error", "db": f"unreachable: {type(e).__name__}"}, 503)

    if beat is None:
        return JSONResponse({"status": "error", "db": "ok", "worker": "no heartbeat yet"}, 503)
    beat_at = beat.beat_at if beat.beat_at.tzinfo else beat.beat_at.replace(tzinfo=UTC)
    age = datetime.now(UTC) - beat_at
    worker_ok = age < HEARTBEAT_MAX_AGE
    body = {
        "status": "ok" if worker_ok else "error",
        "db": "ok",
        "worker": f"last heartbeat {int(age.total_seconds())}s ago",
        "worker_detail": beat.detail,
    }
    return JSONResponse(body, 200 if worker_ok else 503)
