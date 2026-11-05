from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, DateTime, Index, Numeric, String, UniqueConstraint, func
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
