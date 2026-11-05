FOUNDRY_IMAGE := ghcr.io/foundry-rs/foundry:stable
UID_GID := $(shell id -u):$(shell id -g)
FORGE := docker run --rm -u $(UID_GID) -e HOME=/work/.foundry-home -v $(CURDIR)/contracts:/work -w /work --entrypoint forge $(FOUNDRY_IMAGE)

.PHONY: up down chain chain-test build test-contracts test deploy indexer api simulate reorg check lint

up:
	docker compose up -d --wait db
	uv run alembic upgrade head

down:
	docker compose down

# Local dev chain with a 2 second block time, so confirmations visibly climb.
chain:
	docker run --rm --name stakelens-anvil -p 127.0.0.1:8545:8545 --entrypoint anvil $(FOUNDRY_IMAGE) --host 0.0.0.0 --block-time 2

# Instant-mining chain for the end-to-end tests.
chain-test:
	docker run --rm --name stakelens-anvil-test -p 127.0.0.1:8546:8545 --entrypoint anvil $(FOUNDRY_IMAGE) --host 0.0.0.0

build:
	$(FORGE) build

test-contracts:
	$(FORGE) test

test:
	uv run pytest -q

deploy: build
	uv run python -m scripts.deploy

indexer:
	uv run python -m app.indexer

api:
	uv run uvicorn app.main:app --port 8010

simulate:
	uv run python -m scripts.simulate activity

reorg:
	uv run python -m scripts.simulate reorg

check:
	uv run python -m scripts.simulate check

lint:
	uv run ruff check .
	uv run black --check .
