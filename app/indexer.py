"""Follows the chain block by block, and rolls back cleanly when the chain forks.

Each block is written in one transaction together with the cursor, so a crash can never leave
events without their block or skip a block. Fork detection compares the next block's parent hash
with the hash stored for the previous block.
"""

import logging
import time
from dataclasses import dataclass
from decimal import Decimal

import requests
from sqlalchemy import delete, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from web3.exceptions import Web3Exception

from app.chain import BlockRef, ChainReader, Web3Chain
from app.config import get_settings
from app.db import make_engine, make_session_factory
from app.models import IndexedBlock, IndexerState, Reorg, VaultEvent

log = logging.getLogger("stakelens.indexer")

ADVISORY_LOCK_ID = 7_201_455
MAX_BACKOFF_SECONDS = 30.0


class DeepReorgError(RuntimeError):
    """The fork is deeper than we are willing to unwind automatically. Needs an operator."""


@dataclass
class SyncResult:
    blocks: int = 0
    events: int = 0
    reorgs: int = 0


class Indexer:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        chain: ChainReader,
        start_block: int = 0,
        batch_size: int = 100,
        max_reorg_depth: int = 64,
    ) -> None:
        self._sessions = session_factory
        self._chain = chain
        self._start_block = start_block
        self._batch_size = batch_size
        self._max_reorg_depth = max_reorg_depth

    def sync_once(self) -> SyncResult:
        result = SyncResult()
        head = self._chain.head_number()

        with self._sessions.begin() as session:
            state = self._state(session)
            state.chain_head = head
            if self._tip_is_stale(session, state, head):
                self._roll_back(session, state)
                result.reorgs += 1

        while result.blocks < self._batch_size:
            with self._sessions.begin() as session:
                state = self._state(session)
                number = state.last_block + 1
                block = self._chain.block(number) if number <= head else None
                if block is None:
                    break
                previous = session.get(IndexedBlock, number - 1)
                if previous is not None and previous.hash != block.parent_hash:
                    self._roll_back(session, state)
                    result.reorgs += 1
                    continue
                result.events += self._store_block(session, state, block)
                result.blocks += 1
        return result

    def _state(self, session: Session) -> IndexerState:
        state = session.scalar(select(IndexerState).with_for_update())
        if state is None:
            state = IndexerState(id=1, last_block=self._start_block - 1, chain_head=0)
            session.add(state)
            session.flush()
        return state

    def _tip_is_stale(self, session: Session, state: IndexerState, head: int) -> bool:
        stored = session.get(IndexedBlock, state.last_block)
        if stored is None:
            return False
        if head < stored.number:
            return True
        canonical = self._chain.block(stored.number)
        return canonical is None or canonical.hash != stored.hash

    def _store_block(self, session: Session, state: IndexerState, block: BlockRef) -> int:
        rows = [
            {
                "block_number": block.number,
                "block_hash": block.hash,
                "block_time": block.timestamp,
                "tx_hash": event.tx_hash,
                "log_index": event.log_index,
                "name": event.name,
                "wallet": event.wallet,
                "amount": Decimal(event.amount),
            }
            for event in self._chain.events(block.hash)
        ]
        if rows:
            session.execute(insert(VaultEvent).on_conflict_do_nothing(), rows)
        session.add(
            IndexedBlock(number=block.number, hash=block.hash, parent_hash=block.parent_hash)
        )
        state.last_block = block.number
        return len(rows)

    def _roll_back(self, session: Session, state: IndexerState) -> None:
        old_tip = state.last_block
        ancestor = self._find_common_ancestor(session, old_tip)
        depth = old_tip - ancestor
        if depth > self._max_reorg_depth:
            raise DeepReorgError(f"fork of depth {depth} at block {old_tip}; refusing to unwind")

        removed = session.execute(delete(VaultEvent).where(VaultEvent.block_number > ancestor))
        session.execute(delete(IndexedBlock).where(IndexedBlock.number > ancestor))
        state.last_block = ancestor
        session.add(
            Reorg(
                old_tip=old_tip,
                ancestor_block=ancestor,
                depth=depth,
                events_removed=removed.rowcount,
            )
        )
        log.warning(
            "reorg: rolled back %d blocks to %d, removed %d events",
            depth,
            ancestor,
            removed.rowcount,
        )

    def _find_common_ancestor(self, session: Session, tip: int) -> int:
        number = tip
        while number >= self._start_block:
            stored = session.get(IndexedBlock, number)
            canonical = self._chain.block(number)
            if stored is None or (canonical is not None and canonical.hash == stored.hash):
                return number
            number -= 1
        return self._start_block - 1


def run_forever(engine: Engine, indexer: Indexer, poll_seconds: float) -> None:
    backoff = poll_seconds
    # The lock is held by this connection for the life of the process, so a second indexer
    # started by mistake exits instead of racing the first one.
    with engine.connect() as lock_connection:
        acquired = lock_connection.scalar(
            text("SELECT pg_try_advisory_lock(:id)"), {"id": ADVISORY_LOCK_ID}
        )
        if not acquired:
            raise SystemExit("another indexer is already running against this database")

        while True:
            try:
                result = indexer.sync_once()
            except DeepReorgError:
                log.exception("stopping: operator action required")
                raise
            except (requests.RequestException, Web3Exception, ValueError) as exc:
                # web3 raises ValueError for JSON-RPC error responses, for example a block hash
                # that the node has already dropped because of a reorg.
                log.warning("rpc error, retrying in %.1fs: %s", backoff, exc)
                time.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
                continue

            backoff = poll_seconds
            if result.blocks or result.reorgs:
                log.info(
                    "indexed %d blocks, %d events, %d reorgs",
                    result.blocks,
                    result.events,
                    result.reorgs,
                )
            else:
                time.sleep(poll_seconds)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = get_settings()
    if not settings.vault_address:
        raise SystemExit("VAULT_ADDRESS is not set. Run scripts/deploy.py first.")
    engine = make_engine(settings.database_url)
    indexer = Indexer(
        make_session_factory(engine),
        Web3Chain(settings.rpc_url, settings.vault_address),
        start_block=settings.start_block,
        batch_size=settings.batch_size,
        max_reorg_depth=settings.max_reorg_depth,
    )
    run_forever(engine, indexer, settings.poll_seconds)


if __name__ == "__main__":
    main()
