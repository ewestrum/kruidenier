"""FastAPI app (`uvicorn app.web.main:app`)."""

import logging
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, text
from starlette.middleware.sessions import SessionMiddleware

from app.config import get_settings
from app.db.models import WorkerHeartbeat
from app.db.session import session_factory
from app.web.deps import Forbidden, LoginRequired, render
from app.web.ui import router

log = logging.getLogger(__name__)

HEARTBEAT_MAX_AGE = timedelta(minutes=10)
STATIC = Path(__file__).parent / "static"


def _session_secret() -> str:
    key = get_settings().secret_key
    if not key:
        log.warning("SECRET_KEY is empty: sessions will not survive a restart")
        return secrets.token_urlsafe(48)
    return key


app = FastAPI(title="Kruidenier", docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(
    SessionMiddleware,
    secret_key=_session_secret(),
    session_cookie="kruidenier",
    max_age=60 * 60 * 24 * 30,
    same_site="lax",
    https_only=False,  # LAN/Tailscale over plain http is supported (SPEC §15)
)
app.mount("/static", StaticFiles(directory=STATIC), name="static")
app.include_router(router)


@app.exception_handler(LoginRequired)
async def _login_required(request: Request, _: LoginRequired) -> Response:
    if request.headers.get("hx-request"):
        return Response(status_code=204, headers={"HX-Redirect": "/login"})
    return RedirectResponse("/login", status_code=303)


@app.exception_handler(Forbidden)
async def _forbidden(request: Request, exc: Forbidden) -> Response:
    if str(exc) == "csrf":
        message = "Deze pagina was verlopen. Laad hem opnieuw en probeer het nog eens."
    else:
        message = "Alleen een beheerder van het huishouden kan dit doen."
    response = render(request, "error.html", message=message)
    response.status_code = 403
    return response


@app.get("/manifest.webmanifest")
def manifest() -> FileResponse:
    return FileResponse(STATIC / "manifest.webmanifest", media_type="application/manifest+json")


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
