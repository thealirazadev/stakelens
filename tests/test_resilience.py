"""Runs the real indexer process against real failures, then proves the data is still right."""

import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest
from eth_account import Account
from sqlalchemy import delete, select

from app import queries
from app.chain import Web3Chain
from app.indexer import Indexer
from app.models import IndexedBlock, IndexerState, VaultEvent
from app.reconcile import reconcile
from scripts.deploy import ANVIL_DEPLOYER_KEY, E18, send
from scripts.simulate import STAKER_KEYS
from tests.conftest import RPC_URL, TEST_DATABASE_URL
from tests.proxy import RpcProxy

pytestmark = pytest.mark.chain

ROOT = Path(__file__).parent.parent
DEPLOYER = Account.from_key(ANVIL_DEPLOYER_KEY)
ALICE, BOB = (Account.from_key(key) for key in STAKER_KEYS[:2])
TIMEOUT_SECONDS = 20


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def start_indexer(deployed, tmp_path):
    started = []

    def start(*rpc_urls: str, metrics_port: int = 0) -> subprocess.Popen:
        log_file = tmp_path / f"indexer-{len(started)}.log"
        env = {
            **os.environ,
            "DATABASE_URL": TEST_DATABASE_URL,
            "RPC_URL": rpc_urls[0],
            "RPC_FALLBACK_URLS": ",".join(rpc_urls[1:]),
            "VAULT_ADDRESS": deployed.vault.address,
            "START_BLOCK": str(deployed.start_block),
            "POLL_SECONDS": "0.1",
            "RECONCILE_SECONDS": "0.5",
            "CONFIRMATIONS": "1",
            "INDEXER_METRICS_PORT": str(metrics_port),
        }
        process = subprocess.Popen(
            [sys.executable, "-m", "app.indexer"],
            cwd=ROOT,
            env=env,
            stdout=log_file.open("w"),
            stderr=subprocess.STDOUT,
        )
        process.log_file = log_file
        started.append(process)
        return process

    yield start
    for process in started:
        process.kill()
        process.wait()


def stake(w3, deployed, account, tokens: int) -> None:
    wei = tokens * E18
    send(w3, DEPLOYER, deployed.token.functions.transfer(account.address, wei))
    send(w3, account, deployed.token.functions.approve(deployed.vault.address, wei))
    send(w3, account, deployed.vault.functions.stake(wei))


def wait_until_caught_up(sessions, w3, deployed) -> None:
    target = w3.eth.block_number
    deadline = time.monotonic() + TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        with sessions() as session:
            last = session.scalar(select(IndexerState.last_block))
        if last is not None and last >= target:
            return
        time.sleep(0.1)
    raise AssertionError(f"indexer stuck at {last}, wanted {target}")


def assert_matches_contract(sessions, deployed) -> None:
    with sessions() as session:
        indexed = queries.staked_total_at(session, 10**12)
        rows = session.execute(select(VaultEvent.tx_hash, VaultEvent.log_index)).all()
        for account in (ALICE, BOB):
            position = queries.wallet_position(session, account.address, 1)
            on_chain = deployed.vault.functions.stakedOf(account.address).call()
            assert int(position["staked"]["wei"]) == on_chain
    assert indexed == deployed.vault.functions.totalStaked().call()
    assert len(rows) == len(set(rows)), "duplicate events"


def test_kill_dash_nine_mid_run_loses_and_duplicates_nothing(sessions, w3, deployed, start_indexer):
    indexer = start_indexer(RPC_URL)
    stake(w3, deployed, ALICE, 100)
    wait_until_caught_up(sessions, w3, deployed)

    indexer.send_signal(signal.SIGKILL)
    indexer.wait()
    stake(w3, deployed, BOB, 40)  # Happens while nothing is indexing.
    stake(w3, deployed, ALICE, 11)

    start_indexer(RPC_URL)
    wait_until_caught_up(sessions, w3, deployed)

    assert_matches_contract(sessions, deployed)


def test_full_rpc_outage_is_survived(sessions, w3, deployed, start_indexer):
    proxy = RpcProxy(RPC_URL)
    try:
        indexer = start_indexer(proxy.url)
        stake(w3, deployed, ALICE, 100)
        wait_until_caught_up(sessions, w3, deployed)

        proxy.down = True
        stake(w3, deployed, BOB, 40)
        time.sleep(1.5)
        assert indexer.poll() is None, "the indexer died during the outage"
        proxy.down = False

        wait_until_caught_up(sessions, w3, deployed)
        assert_matches_contract(sessions, deployed)
    finally:
        proxy.close()


def test_fails_over_to_backup_and_never_logs_the_api_key(sessions, w3, deployed, start_indexer):
    primary, backup = RpcProxy(RPC_URL), RpcProxy(RPC_URL)
    metrics_port = free_port()
    try:
        indexer = start_indexer(primary.url, backup.url, metrics_port=metrics_port)
        stake(w3, deployed, ALICE, 100)
        wait_until_caught_up(sessions, w3, deployed)
        assert backup.calls == []

        primary.down = True
        stake(w3, deployed, BOB, 40)
        wait_until_caught_up(sessions, w3, deployed)

        assert backup.calls, "backup was never used"
        assert_matches_contract(sessions, deployed)

        scraped = urllib.request.urlopen(f"http://127.0.0.1:{metrics_port}/metrics").read().decode()
        assert 'stakelens_rpc_errors_total{endpoint="0"}' in scraped
        assert "SECRET-API-KEY" not in indexer.log_file.read_text()
    finally:
        primary.close()
        backup.close()


def test_rate_limited_primary_fails_over(sessions, w3, deployed, start_indexer):
    primary, backup = RpcProxy(RPC_URL), RpcProxy(RPC_URL)
    try:
        start_indexer(primary.url, backup.url)
        primary.rate_limited = True
        stake(w3, deployed, ALICE, 100)
        wait_until_caught_up(sessions, w3, deployed)

        assert backup.calls
        assert_matches_contract(sessions, deployed)
    finally:
        primary.close()
        backup.close()


def test_second_indexer_refuses_to_start(sessions, w3, deployed, start_indexer):
    start_indexer(RPC_URL)
    stake(w3, deployed, ALICE, 1)
    wait_until_caught_up(sessions, w3, deployed)

    second = start_indexer(RPC_URL)

    assert second.wait(timeout=TIMEOUT_SECONDS) != 0
    assert "already running" in second.log_file.read_text()


def test_rebuild_from_an_empty_database_gives_identical_events(sessions, w3, deployed):
    stake(w3, deployed, ALICE, 100)
    stake(w3, deployed, BOB, 40)
    send(w3, BOB, deployed.vault.functions.unstake(15 * E18))
    chain = Web3Chain(RPC_URL, deployed.vault.address)

    def index_and_read():
        indexer = Indexer(sessions, chain, start_block=deployed.start_block)
        while indexer.sync_once().blocks:
            pass
        with sessions() as session:
            return session.execute(
                select(
                    VaultEvent.block_hash,
                    VaultEvent.tx_hash,
                    VaultEvent.log_index,
                    VaultEvent.name,
                    VaultEvent.wallet,
                    VaultEvent.amount,
                ).order_by(VaultEvent.block_number, VaultEvent.log_index)
            ).all()

    first = index_and_read()
    with sessions.begin() as session:
        for model in (VaultEvent, IndexedBlock, IndexerState):
            session.execute(delete(model))

    assert index_and_read() == first
    assert len(first) == 3


def test_catches_up_through_a_provider_with_a_small_log_limit(sessions, w3, deployed):
    proxy = RpcProxy(RPC_URL)
    proxy.max_log_range = 10
    try:
        stake(w3, deployed, ALICE, 100)
        w3.provider.make_request("anvil_mine", [hex(300)])
        stake(w3, deployed, BOB, 40)
        w3.provider.make_request("anvil_mine", [hex(100)])

        indexer = Indexer(
            sessions,
            Web3Chain(proxy.url, deployed.vault.address),
            start_block=deployed.start_block,
            batch_size=10_000,
            log_range_size=256,
        )
        while indexer.sync_once().blocks:
            pass

        assert_matches_contract(sessions, deployed)
        assert proxy.calls.count("eth_getLogs") < 120  # Not one call for each of 400+ blocks.
    finally:
        proxy.close()


def test_reconciliation_passes_then_catches_tampered_data(sessions, w3, deployed):
    stake(w3, deployed, ALICE, 100)
    stake(w3, deployed, BOB, 40)
    chain = Web3Chain(RPC_URL, deployed.vault.address)
    indexer = Indexer(sessions, chain, start_block=deployed.start_block)
    while indexer.sync_once().blocks:
        pass

    clean = reconcile(sessions, chain, 1, deployed.start_block)
    assert clean.ok is True
    assert clean.wallets_checked == 2

    with sessions.begin() as session:
        victim = session.scalar(select(VaultEvent).where(VaultEvent.wallet == BOB.address))
        victim.amount = 41 * E18

    tampered = reconcile(sessions, chain, 1, deployed.start_block)
    assert tampered.ok is False
    assert tampered.wallets_mismatched == 1


def test_metrics_endpoint_reports_indexer_and_reconcile_state(client, sessions, w3, deployed):
    stake(w3, deployed, ALICE, 100)
    chain = Web3Chain(RPC_URL, deployed.vault.address)
    indexer = Indexer(sessions, chain, start_block=deployed.start_block)
    while indexer.sync_once().blocks:
        pass
    reconcile(sessions, chain, 1, deployed.start_block)

    body = client.get("/metrics").text

    assert "stakelens_blocks_behind 0.0" in body
    assert "stakelens_reconcile_ok 1.0" in body
    assert "stakelens_events 1.0" in body
    assert "stakelens_indexer_heartbeat_age_seconds" in body
    assert client.get("/vault/reconciliation").json()["ok"] is True
