# syntax=docker/dockerfile:1.7
# One image for `web`, `worker` and `cli` (SPEC §15). Multi-arch: linux/amd64 + linux/arm64.

FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project
COPY alembic.ini ./
COPY app ./app
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev


FROM python:3.12-slim
ARG VERSION=dev
RUN groupadd --system --gid 1000 kruidenier \
 && useradd --system --uid 1000 --gid kruidenier --home-dir /app --shell /usr/sbin/nologin kruidenier
WORKDIR /app
COPY --from=build --chown=kruidenier:kruidenier /app /app
COPY --chmod=755 scripts/entrypoint.sh /usr/local/bin/entrypoint.sh
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Europe/Amsterdam \
    KRUIDENIER_VERSION=${VERSION}
USER kruidenier
EXPOSE 8000
ENTRYPOINT ["entrypoint.sh"]
CMD ["web"]
