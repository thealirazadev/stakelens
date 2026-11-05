"""Sends transactions reliably: at most one takes effect per request, and crashes lose nothing.

The rules that make this safe:

- Every signed transaction is saved before it is broadcast. After a crash the worker rebroadcasts
  the same bytes, which the network treats as the same transaction.
- A retry or fee bump always reuses the nonce, so only one version can ever be mined.
- One transaction per sender is in flight until it has enough confirmations. That gives up
  throughput for much simpler ordering: nonce gaps cannot happen, and a reorged transaction can be
  sent again without disturbing later ones.
- The signing key lives only in this worker. The API process never loads it.
"""

import logging
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Protocol

from eth_account import Account
from eth_account.signers.local import LocalAccount
from hexbytes import HexBytes
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from web3 import Web3
from web3.exceptions import ContractLogicError, TransactionNotFound, Web3RPCError

from app.chain import Endpoints, RpcUnavailable, with_gas_buffer
from app.metrics import TX_BROADCASTS, TX_FEE_BUMPS, TX_REORGED
from app.models import OutboundTx, TxAttempt

log = logging.getLogger("stakelens.txqueue")

GWEI = 10**9
# Nodes reject a replacement unless both fees rise by at least 10%. 12.5% leaves a margin.
FEE_BUMP_PERCENT = 112.5
ADVISORY_LOCK_ID = 7_201_456
# A node may report a used nonce slightly before our receipt lookup succeeds, so a transaction is
# only called dropped after this long with no receipt for any version of it.
DROP_GRACE_SECONDS = 120.0


class Signer(Protocol):
    """Everything the queue needs from a key. A KMS-backed signer can implement this too."""

    @property
    def address(self) -> str: ...

    def sign(self, transaction: dict) -> tuple[str, str]:
        """Returns (raw transaction hex, transaction hash hex)."""
        ...


class LocalKeySigner:
    def __init__(self, private_key: str) -> None:
        self._account: LocalAccount = Account.from_key(private_key)

    @property
    def address(self) -> str:
        return self._account.address

    def sign(self, transaction: dict) -> tuple[str, str]:
        signed = self._account.sign_transaction(transaction)
        return signed.raw_transaction.to_0x_hex(), signed.hash.to_0x_hex()


def enqueue(
    session: Session, key: str, sender: str, to_address: str, data: str, value: int = 0
) -> OutboundTx:
    """Asking twice with the same key returns the original request and sends nothing new."""
    session.execute(
        insert(OutboundTx)
        .values(
            idempotency_key=key,
            sender=sender,
            to_address=Web3.to_checksum_address(to_address),
            data=data,
            value=Decimal(value),
            status="queued",
        )
        .on_conflict_do_nothing(index_elements=["idempotency_key"])
    )
    return session.scalar(select(OutboundTx).where(OutboundTx.idempotency_key == key))


class TxWorker:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        endpoints: Endpoints,
        signer: Signer,
        chain_id: int,
        confirmations: int = 3,
        stuck_seconds: float = 60.0,
        max_fee_per_gas: int = 100 * GWEI,
    ) -> None:
        self._sessions = sessions
        self._rpc = endpoints
        self._signer = signer
        self._chain_id = chain_id
        self._confirmations = confirmations
        self._stuck_after = timedelta(seconds=stuck_seconds)
        self._max_fee = max_fee_per_gas

    def tick(self) -> None:
        with self._sessions.begin() as session:
            tx = session.scalar(
                select(OutboundTx)
                .where(
                    OutboundTx.sender == self._signer.address,
                    OutboundTx.status.in_(("pending", "mined")),
                )
                .order_by(OutboundTx.id)
                .limit(1)
                .with_for_update()
            )
            if tx is not None:
                self._advance(session, tx)
                return
            tx = session.scalar(
                select(OutboundTx)
                .where(OutboundTx.sender == self._signer.address, OutboundTx.status == "queued")
                .order_by(OutboundTx.id)
                .limit(1)
                .with_for_update()
            )
            if tx is not None:
                self._start(session, tx)

    def _start(self, session: Session, tx: OutboundTx) -> None:
        request = {
            "from": tx.sender,
            "to": tx.to_address,
            "data": tx.data,
            "value": int(tx.value),
        }
        try:
            estimate = self._rpc.call(lambda w3: w3.eth.estimate_gas(request))
        except ContractLogicError as exc:
            # It would revert. Nothing was signed and no nonce was used, so this is final.
            tx.status = "failed"
            tx.error = f"would revert: {exc}"[:500]
            log.error("tx %s failed estimation: %s", tx.idempotency_key, exc)
            return

        max_fee, priority_fee = self._current_fees()
        if max_fee > self._max_fee:
            log.warning("network fee %d is above the cap, waiting", max_fee)
            return
        tx.gas_limit = with_gas_buffer(estimate)
        tx.nonce = self._next_nonce(session, tx.sender)
        attempt = self._sign_attempt(session, tx, max_fee, priority_fee)
        tx.status = "pending"
        # Commit before the broadcast. If we crash right after sending, the attempt is on record
        # and the next run sees it rather than sending something new.
        session.commit()
        self._broadcast(attempt)

    def _next_nonce(self, session: Session, sender: str) -> int:
        on_chain = self._rpc.call(lambda w3: w3.eth.get_transaction_count(sender, "latest"))
        highest_used = session.scalar(
            select(func.max(OutboundTx.nonce)).where(OutboundTx.sender == sender)
        )
        # A lagging backup node can report an old count, so never reuse a nonce we already used.
        return on_chain if highest_used is None else max(on_chain, highest_used + 1)

    def _current_fees(self) -> tuple[int, int]:
        def read(w3: Web3) -> tuple[int, int]:
            base_fee = w3.eth.get_block("latest")["baseFeePerGas"]
            priority = w3.eth.max_priority_fee
            return 2 * base_fee + priority, priority

        return self._rpc.call(read)

    def _sign_attempt(
        self, session: Session, tx: OutboundTx, max_fee: int, priority_fee: int
    ) -> TxAttempt:
        raw, tx_hash = self._signer.sign(
            {
                "chainId": self._chain_id,
                "nonce": tx.nonce,
                "to": tx.to_address,
                "data": tx.data,
                "value": int(tx.value),
                "gas": tx.gas_limit,
                "maxFeePerGas": max_fee,
                "maxPriorityFeePerGas": priority_fee,
                "type": 2,
            }
        )
        attempt = TxAttempt(
            tx_id=tx.id,
            tx_hash=tx_hash,
            raw_tx=raw,
            max_fee_per_gas=Decimal(max_fee),
            max_priority_fee_per_gas=Decimal(priority_fee),
        )
        session.add(attempt)
        session.flush()
        return attempt

    def _broadcast(self, attempt: TxAttempt) -> None:
        raw = HexBytes(attempt.raw_tx)

        def send(w3: Web3) -> str | None:
            try:
                w3.eth.send_raw_transaction(raw)
            except Web3RPCError as exc:
                # The node is healthy and said no, usually "already known" or "nonce too low" on a
                # rebroadcast. That must not count as an endpoint failure.
                return str(exc)
            return None

        try:
            rejection = self._rpc.call(send)
        except RpcUnavailable as exc:
            # The attempt is saved, so the next tick sends it again.
            log.info("broadcast of %s did not go through: %s", attempt.tx_hash, exc)
            return
        if rejection is None:
            TX_BROADCASTS.inc()
        else:
            log.info("node rejected %s: %s", attempt.tx_hash, rejection)

    def _attempts(self, session: Session, tx: OutboundTx) -> list[TxAttempt]:
        return list(
            session.scalars(
                select(TxAttempt).where(TxAttempt.tx_id == tx.id).order_by(TxAttempt.id)
            )
        )

    def _find_receipt(self, attempts: list[TxAttempt]) -> tuple[str, dict] | None:
        for attempt in attempts:
            try:
                receipt = self._rpc.call(
                    lambda w3, tx_hash=attempt.tx_hash: w3.eth.get_transaction_receipt(tx_hash)
                )
            except TransactionNotFound:
                continue
            return attempt.tx_hash, receipt
        return None

    def _advance(self, session: Session, tx: OutboundTx) -> None:
        attempts = self._attempts(session, tx)
        found = self._find_receipt(attempts)

        if found is None:
            if tx.status == "mined":
                # We had a receipt and now we don't: a reorg removed it. Send it again.
                log.warning("tx %s was removed by a reorg", tx.idempotency_key)
                TX_REORGED.inc()
                tx.status = "pending"
                tx.winning_hash = tx.mined_block = tx.receipt_success = None
            self._handle_unmined(session, tx, attempts)
            return

        tx_hash, receipt = found
        tx.status = "mined"
        tx.winning_hash = tx_hash
        tx.mined_block = receipt["blockNumber"]
        tx.receipt_success = receipt["status"] == 1

        head = self._rpc.call(lambda w3: w3.eth.block_number)
        if head - receipt["blockNumber"] + 1 >= self._confirmations:
            tx.status = "confirmed" if tx.receipt_success else "reverted"
            log.info("tx %s is %s in %s", tx.idempotency_key, tx.status, tx_hash)

    def _handle_unmined(self, session: Session, tx: OutboundTx, attempts: list[TxAttempt]) -> None:
        latest = attempts[-1]
        on_chain = self._rpc.call(lambda w3: w3.eth.get_transaction_count(tx.sender, "latest"))
        age = datetime.now(UTC) - latest.created_at

        if on_chain > tx.nonce:
            # Something else used this nonce, and none of our versions has a receipt.
            if age > timedelta(seconds=DROP_GRACE_SECONDS):
                tx.status = "dropped"
                tx.error = "nonce was used by a transaction this queue did not send"
                log.error("tx %s dropped: %s", tx.idempotency_key, tx.error)
            return

        if age < self._stuck_after:
            self._broadcast(latest)
            return

        max_fee, priority_fee = self._bumped_fees(latest)
        if max_fee > self._max_fee:
            log.error("tx %s is stuck at the fee cap", tx.idempotency_key)
            self._broadcast(latest)
            return
        TX_FEE_BUMPS.inc()
        log.info("tx %s stuck, raising max fee to %d", tx.idempotency_key, max_fee)
        attempt = self._sign_attempt(session, tx, max_fee, priority_fee)
        session.commit()
        self._broadcast(attempt)

    def _bumped_fees(self, latest: TxAttempt) -> tuple[int, int]:
        def bump(value: Decimal) -> int:
            return int(value * Decimal(str(FEE_BUMP_PERCENT)) / 100) + 1

        current_max, current_priority = self._current_fees()
        return (
            max(bump(latest.max_fee_per_gas), current_max),
            max(bump(latest.max_priority_fee_per_gas), current_priority),
        )


def run_forever(engine: Engine, worker: TxWorker, poll_seconds: float) -> None:
    with engine.connect() as lock_connection:
        acquired = lock_connection.scalar(
            text("SELECT pg_try_advisory_lock(:id)"), {"id": ADVISORY_LOCK_ID}
        )
        if not acquired:
            raise SystemExit("another transaction worker is already running")
        lock_connection.commit()
        while True:
            try:
                worker.tick()
            except (RpcUnavailable, Web3RPCError) as exc:
                log.warning("rpc problem, will retry: %s", type(exc).__name__)
            time.sleep(poll_seconds)
