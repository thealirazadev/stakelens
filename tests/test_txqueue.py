"""The transaction queue against a real EVM node: stuck transactions, crashes, and reorgs."""

import pytest
from eth_account import Account
from sqlalchemy import func, select

from app import txqueue
from app.chain import Endpoints
from app.models import OutboundTx, TxAttempt
from app.txqueue import GWEI, LocalKeySigner, TxWorker, enqueue
from app.txworker import transfer_calldata
from scripts.deploy import ANVIL_DEPLOYER_KEY, E18
from tests.conftest import RPC_URL

pytestmark = pytest.mark.chain

CHAIN_ID = 31337
SIGNER = LocalKeySigner(ANVIL_DEPLOYER_KEY)
RECIPIENT = Account.create().address


def make_worker(sessions, **kwargs) -> TxWorker:
    kwargs.setdefault("confirmations", 1)
    return TxWorker(sessions, Endpoints([RPC_URL]), SIGNER, CHAIN_ID, **kwargs)


def request_transfer(sessions, deployed, key: str, tokens: int = 7) -> None:
    with sessions.begin() as session:
        enqueue(
            session,
            key,
            SIGNER.address,
            deployed.token.address,
            transfer_calldata(RECIPIENT, tokens * E18),
        )


def status(sessions, key: str) -> OutboundTx:
    with sessions() as session:
        return session.scalar(select(OutboundTx).where(OutboundTx.idempotency_key == key))


def attempts(sessions) -> list[TxAttempt]:
    with sessions() as session:
        return list(session.scalars(select(TxAttempt).order_by(TxAttempt.id)))


def balance(deployed) -> int:
    return deployed.token.functions.balanceOf(RECIPIENT).call()


def mine(w3, blocks: int = 1) -> None:
    for _ in range(blocks):
        w3.provider.make_request("evm_mine", [])


def run_until(worker, sessions, key: str, wanted: str, ticks: int = 8) -> None:
    for _ in range(ticks):
        worker.tick()
        if status(sessions, key).status == wanted:
            return
    raise AssertionError(f"{key} is {status(sessions, key).status}, wanted {wanted}")


def test_sends_and_confirms_once(sessions, w3, deployed):
    request_transfer(sessions, deployed, "a")

    run_until(make_worker(sessions), sessions, "a", "confirmed")

    assert balance(deployed) == 7 * E18
    tx = status(sessions, "a")
    assert tx.receipt_success is True
    assert tx.winning_hash == attempts(sessions)[0].tx_hash


def test_waits_for_the_required_confirmations(sessions, w3, deployed):
    request_transfer(sessions, deployed, "a")
    worker = make_worker(sessions, confirmations=3)

    run_until(worker, sessions, "a", "mined")
    worker.tick()
    assert status(sessions, "a").status == "mined"

    mine(w3, 2)
    worker.tick()
    assert status(sessions, "a").status == "confirmed"


def test_same_key_never_sends_twice(sessions, w3, deployed):
    request_transfer(sessions, deployed, "same")
    request_transfer(sessions, deployed, "same", tokens=999)

    worker = make_worker(sessions)
    run_until(worker, sessions, "same", "confirmed")
    worker.tick()

    assert balance(deployed) == 7 * E18
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(OutboundTx)) == 1


def test_stuck_transaction_is_replaced_and_takes_effect_once(sessions, w3, deployed):
    request_transfer(sessions, deployed, "stuck")
    w3.provider.make_request("evm_setAutomine", [False])
    worker = make_worker(sessions, stuck_seconds=0)

    worker.tick()  # First broadcast.
    worker.tick()  # Nothing mined, so it is stuck: replace at a higher fee.
    worker.tick()  # And again.

    versions = attempts(sessions)
    assert len(versions) == 3
    assert versions[0].max_fee_per_gas < versions[1].max_fee_per_gas < versions[2].max_fee_per_gas
    assert balance(deployed) == 0

    mine(w3)
    run_until(worker, sessions, "stuck", "confirmed")

    assert balance(deployed) == 7 * E18
    assert status(sessions, "stuck").winning_hash == versions[-1].tx_hash


def test_crash_between_saving_and_broadcasting_loses_nothing(sessions, w3, deployed, monkeypatch):
    request_transfer(sessions, deployed, "crash")
    crashing = make_worker(sessions)

    def die(self, attempt):
        raise KeyboardInterrupt

    monkeypatch.setattr(TxWorker, "_broadcast", die)
    with pytest.raises(KeyboardInterrupt):
        crashing.tick()
    monkeypatch.undo()

    assert status(sessions, "crash").status == "pending"
    assert balance(deployed) == 0

    run_until(make_worker(sessions), sessions, "crash", "confirmed")

    assert balance(deployed) == 7 * E18
    assert len(attempts(sessions)) == 1


def test_crash_after_broadcast_does_not_double_send(sessions, w3, deployed):
    request_transfer(sessions, deployed, "a")
    make_worker(sessions).tick()  # Sent and mined. This worker now "crashes".

    run_until(make_worker(sessions), sessions, "a", "confirmed")

    assert balance(deployed) == 7 * E18
    assert len(attempts(sessions)) == 1


def test_reorged_transaction_is_sent_again_and_lands_once(sessions, w3, deployed):
    fork_point = w3.provider.make_request("evm_snapshot", [])["result"]
    request_transfer(sessions, deployed, "reorg")
    worker = make_worker(sessions, confirmations=3)
    run_until(worker, sessions, "reorg", "mined")
    assert balance(deployed) == 7 * E18

    w3.provider.make_request("evm_revert", [fork_point])
    assert balance(deployed) == 0

    worker.tick()  # No receipt any more, so it goes back to pending and is rebroadcast.
    assert status(sessions, "reorg").status in ("pending", "mined")
    run_until(worker, sessions, "reorg", "mined")
    mine(w3, 2)
    run_until(worker, sessions, "reorg", "confirmed")

    assert balance(deployed) == 7 * E18


def test_reverting_request_fails_without_using_a_nonce(sessions, w3, deployed):
    far_too_many = 10**30
    request_transfer(sessions, deployed, "bad", tokens=far_too_many)
    request_transfer(sessions, deployed, "good")
    worker = make_worker(sessions)

    run_until(worker, sessions, "bad", "failed")
    run_until(worker, sessions, "good", "confirmed")

    bad = status(sessions, "bad")
    assert bad.nonce is None
    assert "would revert" in bad.error
    assert balance(deployed) == 7 * E18


def test_requests_go_out_in_order_one_at_a_time(sessions, w3, deployed):
    request_transfer(sessions, deployed, "first")
    request_transfer(sessions, deployed, "second")
    worker = make_worker(sessions, confirmations=2)

    worker.tick()
    assert status(sessions, "first").status != "queued"
    worker.tick()
    assert status(sessions, "second").status == "queued"  # First is not confirmed yet.

    mine(w3)
    run_until(worker, sessions, "second", "mined")

    assert status(sessions, "first").status == "confirmed"
    assert status(sessions, "second").nonce == status(sessions, "first").nonce + 1


def test_fee_cap_holds_the_request_without_reserving_a_nonce(sessions, w3, deployed):
    request_transfer(sessions, deployed, "capped")
    worker = make_worker(sessions, max_fee_per_gas=1)

    worker.tick()
    worker.tick()

    tx = status(sessions, "capped")
    assert (tx.status, tx.nonce) == ("queued", None)
    assert attempts(sessions) == []

    run_until(make_worker(sessions, max_fee_per_gas=100 * GWEI), sessions, "capped", "confirmed")
    assert balance(deployed) == 7 * E18


def test_nonce_taken_by_an_outside_transaction_is_reported_as_dropped(
    sessions, w3, deployed, monkeypatch
):
    request_transfer(sessions, deployed, "a")
    w3.provider.make_request("evm_setAutomine", [False])
    worker = make_worker(sessions, stuck_seconds=3600)
    worker.tick()
    ours = status(sessions, "a")

    # The same key sends something else with the same nonce, at a much higher fee.
    outside = SIGNER.sign(
        {
            "chainId": CHAIN_ID,
            "nonce": ours.nonce,
            "to": RECIPIENT,
            "value": 1,
            "gas": 21000,
            "maxFeePerGas": 500 * GWEI,
            "maxPriorityFeePerGas": 400 * GWEI,
            "type": 2,
        }
    )[0]
    w3.eth.send_raw_transaction(outside)
    mine(w3)
    monkeypatch.setattr(txqueue, "DROP_GRACE_SECONDS", 0)

    run_until(worker, sessions, "a", "dropped")

    assert balance(deployed) == 0
    assert "did not send" in status(sessions, "a").error
