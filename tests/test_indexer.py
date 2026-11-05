import pytest
from sqlalchemy import delete, func, select

from app import queries
from app.indexer import DeepReorgError, Indexer
from app.models import IndexedBlock, IndexerState, Reorg, VaultEvent
from tests.fakes import ALICE, BOB, FakeChain

E18 = 10**18


def make_indexer(sessions, chain, **kwargs) -> Indexer:
    return Indexer(sessions, chain, **kwargs)


def drain(indexer: Indexer) -> None:
    while indexer.sync_once().blocks:
        pass


def staked(sessions, wallet: str) -> int:
    with sessions() as session:
        return int(queries.wallet_position(session, wallet, 1)["staked"]["wei"])


def test_indexes_events_and_derives_balances(sessions):
    chain = FakeChain()
    chain.mine(("Staked", ALICE, 100 * E18), ("Staked", BOB, 50 * E18))
    chain.mine(("Unstaked", ALICE, 30 * E18))
    chain.mine(("RewardClaimed", ALICE, 2 * E18))

    drain(make_indexer(sessions, chain))

    assert staked(sessions, ALICE) == 70 * E18
    assert staked(sessions, BOB) == 50 * E18
    with sessions() as session:
        position = queries.wallet_position(session, ALICE, 1)
    assert position["rewards_claimed"]["tokens"] == "2"


def test_resyncing_with_nothing_new_changes_nothing(sessions):
    chain = FakeChain()
    chain.mine(("Staked", ALICE, E18))
    indexer = make_indexer(sessions, chain)
    drain(indexer)

    result = indexer.sync_once()

    assert (result.blocks, result.events, result.reorgs) == (0, 0, 0)
    assert staked(sessions, ALICE) == E18


def test_batch_size_limits_one_pass_and_resumes_from_cursor(sessions):
    chain = FakeChain()
    for _ in range(9):
        chain.mine()
    indexer = make_indexer(sessions, chain, batch_size=4)

    assert indexer.sync_once().blocks == 4
    with sessions() as session:
        assert session.scalar(select(IndexerState.last_block)) == 3

    drain(indexer)
    with sessions() as session:
        assert session.scalar(select(IndexerState.last_block)) == 9


def test_restart_continues_without_duplicates(sessions):
    chain = FakeChain()
    chain.mine(("Staked", ALICE, E18))
    drain(make_indexer(sessions, chain))

    chain.mine(("Staked", ALICE, E18))
    drain(make_indexer(sessions, chain))

    assert staked(sessions, ALICE) == 2 * E18


def test_starts_from_configured_block(sessions):
    chain = FakeChain()
    chain.mine(("Staked", ALICE, E18))  # block 1, before the start block
    chain.mine(("Staked", BOB, E18))  # block 2

    drain(make_indexer(sessions, chain, start_block=2))

    assert staked(sessions, ALICE) == 0
    assert staked(sessions, BOB) == E18


def test_reorg_during_catch_up_replaces_orphaned_events(sessions):
    chain = FakeChain()
    chain.mine(("Staked", ALICE, 10 * E18))
    chain.mine(("Staked", ALICE, 99 * E18))
    indexer = make_indexer(sessions, chain)
    drain(indexer)
    assert staked(sessions, ALICE) == 109 * E18

    chain.fork_from(1)
    chain.mine(("Staked", BOB, 7 * E18))
    chain.mine()
    drain(indexer)

    assert staked(sessions, ALICE) == 10 * E18
    assert staked(sessions, BOB) == 7 * E18
    with sessions() as session:
        reorg = session.scalar(select(Reorg))
    assert (reorg.old_tip, reorg.ancestor_block, reorg.depth, reorg.events_removed) == (2, 1, 1, 1)


def test_same_height_fork_is_detected_from_the_tip(sessions):
    chain = FakeChain()
    chain.mine(("Staked", ALICE, E18))
    indexer = make_indexer(sessions, chain)
    drain(indexer)

    chain.fork_from(0)
    chain.mine(("Staked", BOB, E18))  # Same height as our tip, so no new block to notice.
    drain(indexer)

    assert staked(sessions, ALICE) == 0
    assert staked(sessions, BOB) == E18


def test_chain_getting_shorter_rolls_back_once_a_competing_block_appears(sessions):
    chain = FakeChain()
    chain.mine(("Staked", ALICE, E18))
    chain.mine(("Staked", ALICE, E18))
    indexer = make_indexer(sessions, chain)
    drain(indexer)

    chain.fork_from(1)
    indexer.sync_once()
    assert staked(sessions, ALICE) == 2 * E18  # No hash disagrees yet.

    chain.mine(("Staked", BOB, E18))
    drain(indexer)

    assert staked(sessions, ALICE) == E18
    assert staked(sessions, BOB) == E18


def test_fork_below_our_tip_is_caught_before_the_chain_regrows(sessions):
    chain = FakeChain()
    for _ in range(5):
        chain.mine(("Staked", ALICE, E18))
    indexer = make_indexer(sessions, chain)
    drain(indexer)

    chain.fork_from(2)
    chain.mine(("Staked", BOB, E18))  # Height 3, while our tip is still at 5.
    result = indexer.sync_once()

    assert result.reorgs == 1
    assert staked(sessions, ALICE) == 2 * E18
    assert staked(sessions, BOB) == E18


def test_fork_landing_in_the_middle_of_a_sync_leaves_no_orphaned_events(sessions):
    class ForksDuringSync(FakeChain):
        fork_when_reading: str | None = None

        def events(self, block_hash):
            events = super().events(block_hash)
            if block_hash == self.fork_when_reading:
                self.fork_when_reading = None
                self.fork_from(3)
                self.mine(("Staked", BOB, 2 * E18))
                self.mine(("Staked", BOB, 3 * E18))
            return events

    chain = ForksDuringSync()
    for _ in range(3):
        chain.mine(("Staked", ALICE, E18))
    indexer = make_indexer(sessions, chain)
    drain(indexer)

    orphan = chain.mine(("Staked", ALICE, 100 * E18))  # Block 4, stored just before the fork.
    chain.mine(("Staked", ALICE, 100 * E18))
    chain.fork_when_reading = orphan.hash
    drain(indexer)

    assert staked(sessions, ALICE) == 3 * E18
    assert staked(sessions, BOB) == 5 * E18


def test_lagging_backup_node_is_not_mistaken_for_a_reorg(sessions):
    chain = FakeChain()
    for _ in range(5):
        chain.mine(("Staked", ALICE, E18))
    indexer = make_indexer(sessions, chain)
    drain(indexer)

    chain.hidden_blocks_above = 2  # The node we failed over to is three blocks behind.
    result = indexer.sync_once()

    assert result.reorgs == 0
    assert staked(sessions, ALICE) == 5 * E18
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(Reorg)) == 0

    chain.hidden_blocks_above = None
    chain.mine(("Staked", ALICE, E18))
    drain(indexer)
    assert staked(sessions, ALICE) == 6 * E18


def test_deep_history_uses_log_ranges_and_far_fewer_calls(sessions):
    def build() -> FakeChain:
        chain = FakeChain()
        for number in range(500):
            chain.mine(*([("Staked", ALICE, E18)] if number % 100 == 0 else []))
        return chain

    ranged = build()
    drain(make_indexer(sessions, ranged, max_reorg_depth=10, batch_size=1000))
    assert staked(sessions, ALICE) == 5 * E18

    with sessions.begin() as session:
        for model in (VaultEvent, IndexedBlock, IndexerState):
            session.execute(delete(model))

    slow = build()
    drain(make_indexer(sessions, slow, max_reorg_depth=10_000, batch_size=1000))
    assert staked(sessions, ALICE) == 5 * E18

    assert sum(ranged.calls.values()) * 10 < sum(slow.calls.values())


def test_log_range_shrinks_to_fit_the_provider_limit(sessions):
    chain = FakeChain()
    for number in range(100):
        chain.mine(*([("Staked", ALICE, E18)] if number % 7 == 0 else []))
    chain.max_log_range = 10

    drain(make_indexer(sessions, chain, max_reorg_depth=5, log_range_size=64, batch_size=1000))

    assert staked(sessions, ALICE) == 15 * E18
    with sessions() as session:
        assert session.scalar(select(IndexerState.last_block)) == 100


def test_reorg_just_above_range_indexed_history_is_handled(sessions):
    chain = FakeChain()
    for _ in range(50):
        chain.mine(("Staked", ALICE, E18))
    indexer = make_indexer(sessions, chain, max_reorg_depth=10, batch_size=1000)
    drain(indexer)

    chain.fork_from(45)
    for _ in range(8):
        chain.mine(("Staked", BOB, E18))
    drain(indexer)

    assert staked(sessions, ALICE) == 45 * E18
    assert staked(sessions, BOB) == 8 * E18
    with sessions() as session:
        total = queries.vault_stats(session, 1)["total_staked"]["wei"]
    assert int(total) == chain.total_staked(chain.head_number())


def test_multi_block_reorg_rewinds_to_common_ancestor(sessions):
    chain = FakeChain()
    for _ in range(6):
        chain.mine(("Staked", ALICE, E18))
    indexer = make_indexer(sessions, chain)
    drain(indexer)

    chain.fork_from(2)
    for _ in range(5):
        chain.mine(("Staked", BOB, E18))
    drain(indexer)

    assert staked(sessions, ALICE) == 2 * E18
    assert staked(sessions, BOB) == 5 * E18
    with sessions() as session:
        canonical = session.scalars(select(IndexedBlock).order_by(IndexedBlock.number)).all()
    assert [block.hash for block in canonical] == [block.hash for block in chain.blocks]


def test_reorg_deeper_than_limit_stops_and_keeps_data(sessions):
    chain = FakeChain()
    for _ in range(5):
        chain.mine(("Staked", ALICE, E18))
    indexer = make_indexer(sessions, chain, max_reorg_depth=2)
    drain(indexer)

    chain.fork_from(1)
    for _ in range(6):
        chain.mine()

    with pytest.raises(DeepReorgError):
        indexer.sync_once()

    assert staked(sessions, ALICE) == 5 * E18
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(Reorg)) == 0


def test_failed_block_write_leaves_cursor_untouched(sessions):
    chain = FakeChain()
    chain.mine(("Staked", ALICE, E18))
    block = chain.mine(("Staked", ALICE, E18))
    logs = chain.logs.pop(block.hash)

    with pytest.raises(KeyError):
        make_indexer(sessions, chain).sync_once()

    with sessions() as session:
        assert session.scalar(select(IndexerState.last_block)) == 1
        assert session.scalar(select(func.count()).select_from(VaultEvent)) == 1

    chain.logs[block.hash] = logs
    drain(make_indexer(sessions, chain))
    assert staked(sessions, ALICE) == 2 * E18


def test_confirmed_balance_excludes_recent_blocks(sessions):
    chain = FakeChain()
    chain.mine(("Staked", ALICE, 10 * E18))  # block 1
    chain.mine(("Staked", ALICE, 5 * E18))  # block 2, the head
    drain(make_indexer(sessions, chain))

    with sessions() as session:
        two = queries.wallet_position(session, ALICE, confirmations=2)
        events = queries.recent_events(session, confirmations=2)

    assert two["staked"]["tokens"] == "15"
    assert two["confirmed_staked"]["tokens"] == "10"
    assert [(e["block_number"], e["confirmations"], e["status"]) for e in events] == [
        (2, 1, "pending"),
        (1, 2, "confirmed"),
    ]


@pytest.mark.parametrize(
    ("wei", "text"),
    [
        (0, "0"),
        (10**18, "1"),
        (1, "0.000000000000000001"),
        (1_500_000_000_000_000_000, "1.5"),
        (-(10**18), "-1"),
        (
            2**256 - 1,
            "115792089237316195423570985008687907853269984665640564039457.584007913129639935",
        ),
    ],
)
def test_format_units_is_exact(wei, text):
    assert queries.format_units(wei) == text
