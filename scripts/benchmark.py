"""Measures catch-up speed and API latency, and writes docs/benchmarks.md.

Needs Postgres (`make up`) and an instant-mining chain (`make chain-test`). It uses its own
database, so it never touches real data.
"""

import os
import platform
import random
import statistics
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
from alembic import command
from alembic.config import Config
from eth_account import Account
from sqlalchemy import create_engine, delete, text
from sqlalchemy.dialects.postgresql import insert
from web3 import Web3

from app.chain import Web3Chain
from app.db import make_session_factory
from app.indexer import Indexer
from app.models import IndexedBlock, IndexerState, VaultEvent
from scripts.deploy import ANVIL_DEPLOYER_KEY, E18, deploy, send
from scripts.simulate import STAKER_KEYS
from tests.proxy import RpcProxy

ROOT = Path(__file__).parent.parent
SERVER = os.environ.get(
    "TEST_DATABASE_SERVER", "postgresql+psycopg://stakelens:stakelens@localhost:55432"
)
DB_URL = f"{SERVER}/stakelens_bench"
RPC_URL = os.environ.get("TEST_RPC_URL", "http://127.0.0.1:8546")

CATCH_UP_BLOCKS = 5000
STAKES = 40
EVENT_ROWS = 200_000
WALLETS = 2_000
API_PORT = 8097
CONCURRENCY = 16
SECONDS_PER_ENDPOINT = 10


def fresh_database():
    admin = create_engine(f"{SERVER}/postgres", isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text("DROP DATABASE IF EXISTS stakelens_bench WITH (FORCE)"))
        connection.execute(text("CREATE DATABASE stakelens_bench"))
    admin.dispose()
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", DB_URL.replace("%", "%%"))
    command.upgrade(config, "head")
    return create_engine(DB_URL)


def measure_catch_up(engine) -> list[dict]:
    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    snapshot = w3.provider.make_request("evm_snapshot", [])["result"]
    deployer = Account.from_key(ANVIL_DEPLOYER_KEY)
    staker = Account.from_key(STAKER_KEYS[0])
    deployed = deploy(w3, deployer)
    send(w3, deployer, deployed.token.functions.transfer(staker.address, STAKES * E18))
    send(w3, staker, deployed.token.functions.approve(deployed.vault.address, STAKES * E18))
    for _ in range(STAKES):
        send(w3, staker, deployed.vault.functions.stake(E18))
        w3.provider.make_request("anvil_mine", [hex(CATCH_UP_BLOCKS // STAKES)])

    sessions = make_session_factory(engine)
    results = []
    for label, reorg_depth in (("Block by block", 10**9), ("Log ranges", 64)):
        with sessions.begin() as session:
            for model in (VaultEvent, IndexedBlock, IndexerState):
                session.execute(delete(model))
        proxy = RpcProxy(RPC_URL)
        indexer = Indexer(
            sessions,
            Web3Chain(proxy.url, deployed.vault.address),
            start_block=deployed.start_block,
            batch_size=1_000_000,
            max_reorg_depth=reorg_depth,
        )
        started = time.perf_counter()
        blocks = 0
        while (done := indexer.sync_once().blocks) > 0:
            blocks += done
        elapsed = time.perf_counter() - started
        results.append(
            {
                "label": label,
                "blocks": blocks,
                "seconds": elapsed,
                "rpc_calls": len(proxy.calls),
            }
        )
        proxy.close()
    w3.provider.make_request("evm_revert", [snapshot])
    return results


def seed_events(engine) -> list[str]:
    rng = random.Random(7)
    wallets = [Web3.to_checksum_address(f"0x{rng.getrandbits(160):040x}") for _ in range(WALLETS)]
    now = datetime.now(UTC)
    with engine.begin() as connection:
        for start in range(0, EVENT_ROWS, 5000):
            connection.execute(
                insert(VaultEvent),
                [
                    {
                        "block_number": index // 5,
                        "block_hash": f"0x{index // 5:064x}",
                        "block_time": now,
                        "tx_hash": f"0x{index:064x}",
                        "log_index": index % 5,
                        "name": rng.choice(("Staked", "Staked", "Unstaked", "RewardClaimed")),
                        "wallet": rng.choice(wallets),
                        "amount": Decimal(rng.randrange(1, 1000) * E18),
                    }
                    for index in range(start, start + 5000)
                ],
            )
        connection.execute(
            insert(IndexerState),
            [
                {
                    "id": 1,
                    "last_block": EVENT_ROWS // 5,
                    "chain_head": EVENT_ROWS // 5,
                }
            ],
        )
        connection.execute(text("ANALYZE"))
    return wallets


def load(url: str, wallets: list[str]) -> dict:
    latencies: list[float] = []
    errors = [0]
    lock = threading.Lock()
    deadline = time.perf_counter() + SECONDS_PER_ENDPOINT

    def worker(seed: int) -> None:
        rng = random.Random(seed)
        with httpx.Client(base_url=f"http://127.0.0.1:{API_PORT}", timeout=30) as client:
            while time.perf_counter() < deadline:
                path = url.format(wallet=rng.choice(wallets))
                started = time.perf_counter()
                status = client.get(path).status_code
                elapsed = time.perf_counter() - started
                with lock:
                    latencies.append(elapsed)
                    errors[0] += status != 200

    threads = [threading.Thread(target=worker, args=(seed,)) for seed in range(CONCURRENCY)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    latencies.sort()
    return {
        "url": url,
        "rps": len(latencies) / SECONDS_PER_ENDPOINT,
        "p50": statistics.median(latencies) * 1000,
        "p95": latencies[int(len(latencies) * 0.95)] * 1000,
        "errors": errors[0],
    }


def measure_api(wallets: list[str]) -> list[dict]:
    env = {**os.environ, "DATABASE_URL": DB_URL, "ANTHROPIC_API_KEY": ""}
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(API_PORT)],
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(50):
            try:
                httpx.get(f"http://127.0.0.1:{API_PORT}/health", timeout=2)
                break
            except httpx.HTTPError:
                time.sleep(0.2)
        return [
            load(url, wallets)
            for url in (
                "/wallets/{wallet}/position",
                "/vault/events?limit=25",
                "/vault/stakers",
                "/vault/stats",
            )
        ]
    finally:
        server.terminate()
        server.wait()


def main() -> None:
    engine = fresh_database()
    print("measuring catch-up...", flush=True)
    catch_up = measure_catch_up(engine)
    with make_session_factory(engine).begin() as session:
        for model in (VaultEvent, IndexedBlock, IndexerState):
            session.execute(delete(model))
    print("seeding events...", flush=True)
    wallets = seed_events(engine)
    print("measuring api...", flush=True)
    api = measure_api(wallets)

    cpu = (
        subprocess.run(["lscpu"], capture_output=True, text=True)
        .stdout.split("Model name:")[1]
        .split("\n")[0]
        .strip()
    )
    lines = [
        "# Benchmarks",
        "",
        f"Measured with `make benchmark` on {datetime.now(UTC).date()}: {cpu}, "
        f"{os.cpu_count()} threads, {platform.system()}.",
        "",
        "Everything ran on one laptop: a local chain, Postgres in Docker, and one API worker, "
        "with the load generator on the same machine. A public RPC adds network latency, so "
        "read these numbers as a comparison and a ceiling, not a production forecast.",
        "",
        "## Catching up on history",
        "",
        f"About {CATCH_UP_BLOCKS} blocks holding {STAKES} vault events.",
        "",
        "| Mode | Blocks | Seconds | Blocks per second | RPC calls |",
        "|---|---:|---:|---:|---:|",
    ]
    for row in catch_up:
        rate = row["blocks"] / row["seconds"]
        lines.append(
            f"| {row['label']} | {row['blocks']} | {row['seconds']:.1f} | {rate:.0f} "
            f"| {row['rpc_calls']} |"
        )
    lines += [
        "",
        "RPC calls are what free provider plans limit, so that column matters most.",
        "",
        "## API under load",
        "",
        f"{EVENT_ROWS:,} events across {WALLETS:,} wallets, {CONCURRENCY} concurrent clients, "
        f"{SECONDS_PER_ENDPOINT} seconds per endpoint.",
        "",
        "| Endpoint | Requests per second | p50 ms | p95 ms | Errors |",
        "|---|---:|---:|---:|---:|",
    ]
    for row in api:
        lines.append(
            f"| `{row['url']}` | {row['rps']:.0f} | {row['p50']:.0f} | {row['p95']:.0f} "
            f"| {row['errors']} |"
        )
    (ROOT / "docs" / "benchmarks.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
