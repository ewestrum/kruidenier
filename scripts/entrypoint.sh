#!/bin/sh
# Container entrypoint: `web`, `worker`, `web-dev`, or `cli <command>` (SPEC §15).
set -eu

case "${1:-web}" in
  web)
    # Migrations run on every start; an advisory lock prevents double runs.
    python -m app.db.migrate
    exec uvicorn app.web.main:app --host 0.0.0.0 --port 8000 \
      --proxy-headers --forwarded-allow-ips='*'
    ;;
  web-dev)
    python -m app.db.migrate
    exec uvicorn app.web.main:app --host 0.0.0.0 --port 8000 --reload --reload-dir /app/app
    ;;
  worker)
    # The worker may start before web; the shared advisory lock makes this safe.
    python -m app.db.migrate
    exec python -m app.worker
    ;;
  cli)
    shift
    exec python -m app.cli "$@"
    ;;
  *)
    exec "$@"
    ;;
esac
