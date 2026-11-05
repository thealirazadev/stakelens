FOUNDRY_IMAGE := ghcr.io/foundry-rs/foundry:stable
UID_GID := $(shell id -u):$(shell id -g)
FORGE := docker run --rm -u $(UID_GID) -e HOME=/work/.foundry-home -v $(CURDIR)/contracts:/work -w /work --entrypoint forge $(FOUNDRY_IMAGE)

.PHONY: txworker verify verify-check benchmark mutation alerts-check up down chain chain-test build test-contracts test deploy indexer api simulate reorg check lint

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

txworker:
	uv run python -m app.txworker run

verify:
	uv run python -m scripts.verify

verify-check:
	uv run python -m scripts.verify --check

benchmark:
	uv run python -m scripts.benchmark

mutation:
	uv run python -m scripts.mutation_check

alerts-check:
	docker run --rm -v $(CURDIR)/ops:/ops:ro --entrypoint promtool prom/prometheus:latest check rules /ops/alerts.yml

lint:
	uv run ruff check .
	uv run black --check .
