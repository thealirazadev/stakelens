"""Checks that the indexed data still agrees with the contract.

Balances are derived from events, so a missed or duplicated event would otherwise go unnoticed.
The check runs at a block that is already confirmed and asks the node for the contract's state at
that exact block, so a transaction landing during the check cannot cause a false alarm.
"""

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app import queries
from app.chain import ChainReader
from app.models import IndexerState, Reconciliation

log = logging.getLogger("stakelens.reconcile")

MAX_WALLETS_CHECKED = 20


def reconcile(
    sessions: sessionmaker[Session],
    chain: ChainReader,
    confirmations: int,
    start_block: int = 0,
) -> Reconciliation | None:
    """Returns None when no block is confirmed yet, so there is nothing to compare."""
    with sessions() as session:
        state = session.scalar(select(IndexerState))
        if state is None:
            return None
        block = min(state.last_block, state.chain_head - confirmations + 1)
        if block < start_block:
            return None
        indexed_total = queries.staked_total_at(session, block)
        wallets = queries.largest_stakes_at(session, block, MAX_WALLETS_CHECKED)

    onchain_total = chain.total_staked(block)
    mismatched = [wallet for wallet, amount in wallets if chain.staked_of(wallet, block) != amount]
    result = Reconciliation(
        block_number=block,
        onchain_total=onchain_total,
        indexed_total=indexed_total,
        wallets_checked=len(wallets),
        wallets_mismatched=len(mismatched),
        ok=onchain_total == indexed_total and not mismatched,
    )
    if not result.ok:
        log.error(
            "index does not match the contract at block %d: chain %d, indexed %d, wallets %s",
            block,
            onchain_total,
            indexed_total,
            mismatched,
        )
    with sessions.begin() as session:
        session.add(result)
    return result
