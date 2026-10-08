.PHONY: dev test lint migrate image image-tar probe

IMAGE ?= kruidenier
TAG ?= local

dev:
	docker compose up --build

test:
	uv run pytest

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy app

migrate:
	uv run python -m app.db.migrate

# Multi-arch build (amd64 + arm64). Needs a buildx builder with QEMU.
image:
	docker buildx build --platform linux/amd64,linux/arm64 -t $(IMAGE):$(TAG) .

# Single-arch tar for import via Container Manager (no registry). ARCH=amd64 or arm64.
ARCH ?= amd64
image-tar:
	docker buildx build --platform linux/$(ARCH) -t $(IMAGE):$(TAG) --output type=docker,dest=kruidenier-$(TAG)-$(ARCH).tar .

# HANDMATIG: rooktest tegen de echte AH-API, neemt fixtures op. Alleen door een mens te draaien.
probe:
	uv run python scripts/probe.py $(ARGS)
