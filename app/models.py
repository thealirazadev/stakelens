from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

# uint256 needs 78 decimal digits. Amounts are stored in wei and never as floats.
Wei = Numeric(78, 0)


class IndexedBlock(Base):
    """The canonical chain as this indexer last saw it. Parent hashes are how forks are detected."""

    __tablename__ = "indexed_blocks"

    number: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    hash: Mapped[str] = mapped_column(String(66))
    parent_hash: Mapped[str] = mapped_column(String(66))


class VaultEvent(Base):
    __tablename__ = "vault_events"
    __table_args__ = (
        UniqueConstraint("block_hash", "log_index", name="uq_vault_events_block_log"),
        Index("ix_vault_events_wallet_block", "wallet", "block_number"),
        Index("ix_vault_events_block", "block_number"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    block_number: Mapped[int] = mapped_column(BigInteger)
    block_hash: Mapped[str] = mapped_column(String(66))
    block_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    tx_hash: Mapped[str] = mapped_column(String(66))
    log_index: Mapped[int] = mapped_column(BigInteger)
    name: Mapped[str] = mapped_column(String(32))
    wallet: Mapped[str] = mapped_column(String(42))
    amount: Mapped[Decimal] = mapped_column(Wei)


class IndexerState(Base):
    __tablename__ = "indexer_state"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    last_block: Mapped[int] = mapped_column(BigInteger)
    chain_head: Mapped[int] = mapped_column(BigInteger)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Reorg(Base):
    __tablename__ = "reorgs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    old_tip: Mapped[int] = mapped_column(BigInteger)
    ancestor_block: Mapped[int] = mapped_column(BigInteger)
    depth: Mapped[int] = mapped_column(BigInteger)
    events_removed: Mapped[int] = mapped_column(BigInteger)


class AuthChallenge(Base):
    __tablename__ = "auth_challenges"

    nonce: Mapped[str] = mapped_column(String(32), primary_key=True)
    address: Mapped[str] = mapped_column(String(42))
    message: Mapped[str] = mapped_column(String(1024))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuthSession(Base):
    __tablename__ = "auth_sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    address: Mapped[str] = mapped_column(String(42))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AssistantCall(Base):
    __tablename__ = "assistant_calls"
    __table_args__ = (Index("ix_assistant_calls_wallet_time", "wallet", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    wallet: Mapped[str] = mapped_column(String(42))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    model: Mapped[str] = mapped_column(String(64))
    input_tokens: Mapped[int] = mapped_column(BigInteger)
    output_tokens: Mapped[int] = mapped_column(BigInteger)
    tool_calls: Mapped[int] = mapped_column(BigInteger)


class Reconciliation(Base):
    __tablename__ = "reconciliations"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    block_number: Mapped[int] = mapped_column(BigInteger)
    onchain_total: Mapped[Decimal] = mapped_column(Wei)
    indexed_total: Mapped[Decimal] = mapped_column(Wei)
    wallets_checked: Mapped[int] = mapped_column(BigInteger)
    wallets_mismatched: Mapped[int] = mapped_column(BigInteger)
    ok: Mapped[bool] = mapped_column(Boolean)


class OutboundTx(Base):
    """A transaction the system has been asked to send.

    queued -> pending -> mined -> confirmed | reverted. Terminal failures: failed, dropped.
    """

    __tablename__ = "outbound_txs"
    __table_args__ = (Index("ix_outbound_txs_sender_status", "sender", "status", "id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    sender: Mapped[str] = mapped_column(String(42))
    to_address: Mapped[str] = mapped_column(String(42))
    data: Mapped[str] = mapped_column(Text)
    value: Mapped[Decimal] = mapped_column(Wei, default=0)
    status: Mapped[str] = mapped_column(String(16), default="queued")
    nonce: Mapped[int | None] = mapped_column(BigInteger)
    gas_limit: Mapped[int | None] = mapped_column(BigInteger)
    winning_hash: Mapped[str | None] = mapped_column(String(66))
    mined_block: Mapped[int | None] = mapped_column(BigInteger)
    receipt_success: Mapped[bool | None] = mapped_column(Boolean)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TxAttempt(Base):
    """One signed version of a transaction. Written before broadcast, so a crash loses nothing.

    Fee bumps add attempts under the same nonce. Any of them may be the one that gets mined.
    """

    __tablename__ = "tx_attempts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tx_id: Mapped[int] = mapped_column(ForeignKey("outbound_txs.id"), index=True)
    tx_hash: Mapped[str] = mapped_column(String(66), unique=True)
    raw_tx: Mapped[str] = mapped_column(Text)
    max_fee_per_gas: Mapped[Decimal] = mapped_column(Wei)
    max_priority_fee_per_gas: Mapped[Decimal] = mapped_column(Wei)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
