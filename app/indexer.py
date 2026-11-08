"""Follows the chain and rolls back cleanly when it forks.

Each write commits together with the cursor, so a crash can never leave events without their block
or skip a block. Fork detection compares the next block's parent hash with the stored hash of the
previous block.

Two modes: blocks too old to reorg (deeper than max_reorg_depth) are fetched in large log ranges,
which is far cheaper when catching up. Blocks near the head are processed one at a time, because
that is where forks need to be caught.
"""

import logging
import time
from dataclasses import dataclass
from decimal import Decimal

from prometheus_client import start_http_server
from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.chain import BlockRef, ChainEvent, ChainReader, RangeTooLarge, RpcUnavailable, Web3Chain
from app.config import get_settings
from app.db import make_engine, make_session_factory
from app.metrics import BLOCKS_INDEXED, EVENTS_INDEXED, REORGS, SYNC_SECONDS
from app.models import IndexedBlock, IndexerState, Reorg, VaultEvent
from app.reconcile import reconcile

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
        log_range_size: int = 2000,
    ) -> None:
        self._sessions = session_factory
        self._chain = chain
        self._start_block = start_block
        self._batch_size = batch_size
        self._max_reorg_depth = max_reorg_depth
        self._log_range_size = log_range_size

    def sync_once(self) -> SyncResult:
        with SYNC_SECONDS.time():
            result = self._sync()
        BLOCKS_INDEXED.inc(result.blocks)
        EVENTS_INDEXED.inc(result.events)
        REORGS.inc(result.reorgs)
        return result

    def _sync(self) -> SyncResult:
        result = SyncResult()
        head = self._chain.head_number()

        with self._sessions.begin() as session:
            state = self._state(session)
            state.chain_head = head
            state.updated_at = func.now()
            if self._tip_is_on_another_fork(session, state, head):
                self._roll_back(session, state)
                result.reorgs += 1

        while result.blocks < self._batch_size:
            with self._sessions.begin() as session:
                state = self._state(session)
                number = state.last_block + 1
                safe_head = head - self._max_reorg_depth
                if number <= safe_head:
                    self._store_range(session, state, number, safe_head, result)
                    continue

                block = self._chain.block(number) if number <= head else None
                if block is None:
                    break
                previous = session.get(IndexedBlock, number - 1)
                if previous is not None and previous.hash != block.parent_hash:
                    self._roll_back(session, state)
                    result.reorgs += 1
                    continue
                events = self._chain.events(block.hash)
                self._store_events(session, block, events)
                self._store_block(session, state, block)
                result.events += len(events)
                result.blocks += 1
        return result

    def _state(self, session: Session) -> IndexerState:
        state = session.scalar(select(IndexerState).with_for_update())
        if state is None:
            state = IndexerState(id=1, last_block=self._start_block - 1, chain_head=0)
            session.add(state)
            session.flush()
        return state

    def _tip_is_on_another_fork(self, session: Session, state: IndexerState, head: int) -> bool:
        """True only when a block hash disagrees with what we stored.

        A head below our tip is not evidence on its own, because a backup RPC node may simply be a
        few blocks behind. So compare at the highest height both sides have. If the chain really
        did get shorter, the disagreement shows up as soon as a competing block is mined there.
        """
        stored = session.scalar(
            select(IndexedBlock)
            .where(IndexedBlock.number <= min(state.last_block, head))
            .order_by(IndexedBlock.number.desc())
            .limit(1)
        )
        if stored is None:
            return False
        canonical = self._chain.block(stored.number)
        return canonical is not None and canonical.hash != stored.hash

    def _store_events(self, session: Session, block: BlockRef, events: list[ChainEvent]) -> None:
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
            for event in events
        ]
        if rows:
            session.execute(insert(VaultEvent).on_conflict_do_nothing(), rows)

    def _store_block(self, session: Session, state: IndexerState, block: BlockRef) -> None:
        session.add(
            IndexedBlock(number=block.number, hash=block.hash, parent_hash=block.parent_hash)
        )
        state.last_block = block.number

    def _store_range(
        self, session: Session, state: IndexerState, first: int, safe_head: int, result: SyncResult
    ) -> None:
        last = min(first + self._log_range_size - 1, safe_head)
        try:
            located = self._chain.events_in_range(first, last)
        except RangeTooLarge:
            if self._log_range_size == 1:
                raise
            self._log_range_size = max(self._log_range_size // 2, 1)
            log.info("provider limit hit, log range is now %d blocks", self._log_range_size)
            return

        # Only blocks holding events need a header, for their timestamp. Everything else in the
        # range is skipped, which is where the saving comes from.
        by_block: dict[int, list[ChainEvent]] = {}
        for item in located:
            by_block.setdefault(item.block_number, []).append(item.event)
        for number, events in by_block.items():
            block = self._require_block(number)
            self._store_events(session, block, events)
            if number != last:
                session.add(
                    IndexedBlock(
                        number=block.number, hash=block.hash, parent_hash=block.parent_hash
                    )
                )
        self._store_block(session, state, self._require_block(last))
        result.blocks += last - first + 1
        result.events += len(located)

    def _require_block(self, number: int) -> BlockRef:
        block = self._chain.block(number)
        if block is None:
            raise RpcUnavailable(f"node has no block {number}")
        return block

    def _roll_back(self, session: Session, state: IndexerState) -> None:
        old_tip = state.last_block
        ancestor = self._find_common_ancestor(session, old_tip)
        depth = old_tip - ancestor

        removed = session.scalars(
            delete(VaultEvent)
            .where(VaultEvent.block_number > ancestor)
            .returning(VaultEvent.wallet)
        ).all()
        session.execute(delete(IndexedBlock).where(IndexedBlock.number > ancestor))
        state.last_block = ancestor
        session.add(
            Reorg(
                old_tip=old_tip,
                ancestor_block=ancestor,
                depth=depth,
                events_removed=len(removed),
                wallets_affected=sorted(set(removed)),
            )
        )
        log.warning(
            "reorg: rolled back %d blocks to %d, removed %d events",
            depth,
            ancestor,
            len(removed),
        )

    def _find_common_ancestor(self, session: Session, tip: int) -> int:
        # Blocks indexed in range mode are not all stored, so walk the stored ones only.
        for stored in session.scalars(
            select(IndexedBlock)
            .where(IndexedBlock.number <= tip)
            .order_by(IndexedBlock.number.desc())
        ):
            if tip - stored.number > self._max_reorg_depth:
                break
            canonical = self._chain.block(stored.number)
            if canonical is not None and canonical.hash == stored.hash:
                return stored.number
        else:
            if tip - (self._start_block - 1) <= self._max_reorg_depth:
                return self._start_block - 1
        raise DeepReorgError(f"no common ancestor within {self._max_reorg_depth} blocks of {tip}")


def run_forever(
    engine: Engine,
    sessions: sessionmaker[Session],
    indexer: Indexer,
    chain: ChainReader,
    poll_seconds: float,
    reconcile_seconds: float,
    confirmations: int,
    start_block: int,
) -> None:
    backoff = poll_seconds
    next_reconcile = time.monotonic() + reconcile_seconds
    # The lock is held by this connection for the life of the process, so a second indexer
    # started by mistake exits instead of racing the first one.
    with engine.connect() as lock_connection:
        acquired = lock_connection.scalar(
            text("SELECT pg_try_advisory_lock(:id)"), {"id": ADVISORY_LOCK_ID}
        )
        if not acquired:
            raise SystemExit("another indexer is already running against this database")
        lock_connection.commit()

        while True:
            try:
                result = indexer.sync_once()
                if time.monotonic() >= next_reconcile:
                    reconcile(sessions, chain, confirmations, start_block)
                    next_reconcile = time.monotonic() + reconcile_seconds
            except DeepReorgError:
                log.exception("stopping: operator action required")
                raise
            except RpcUnavailable as exc:
                log.warning("%s, retrying in %.1fs", exc, backoff)
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
    sessions = make_session_factory(engine)
    chain = Web3Chain(settings.rpc_urls, settings.vault_address)
    indexer = Indexer(
        sessions,
        chain,
        start_block=settings.start_block,
        batch_size=settings.batch_size,
        max_reorg_depth=settings.max_reorg_depth,
        log_range_size=settings.log_range_size,
    )
    if settings.indexer_metrics_port:
        start_http_server(settings.indexer_metrics_port, addr="127.0.0.1")
    run_forever(
        engine,
        sessions,
        indexer,
        chain,
        settings.poll_seconds,
        settings.reconcile_seconds,
        settings.confirmations,
        settings.start_block,
    )


if __name__ == "__main__":
    main()
