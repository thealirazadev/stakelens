"""Read side, shared by the HTTP API and the assistant's tools so both report the same numbers."""

from decimal import Decimal

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.models import IndexerState, Reconciliation, Reorg, VaultEvent

TOKEN_DECIMALS = 18

_stake_delta = case(
    (VaultEvent.name == "Staked", VaultEvent.amount),
    (VaultEvent.name == "Unstaked", -VaultEvent.amount),
    else_=0,
)


def format_units(wei: int) -> str:
    """Render wei as an exact decimal string, without going through a float."""
    sign = "-" if wei < 0 else ""
    whole, fraction = divmod(abs(wei), 10**TOKEN_DECIMALS)
    fraction_text = f"{fraction:0{TOKEN_DECIMALS}d}".rstrip("0")
    return f"{sign}{whole}.{fraction_text}" if fraction_text else f"{sign}{whole}"


def _amount(wei: int | Decimal | None) -> dict[str, str]:
    value = int(wei or 0)
    return {"wei": str(value), "tokens": format_units(value)}


def _chain_position(session: Session) -> tuple[int, int]:
    state = session.scalar(select(IndexerState))
    if state is None:
        return -1, -1
    return state.chain_head, state.last_block


def _confirmed_up_to(head: int, confirmations: int) -> int:
    # A block counts its own inclusion as the first confirmation.
    return head - confirmations + 1


def vault_stats(session: Session, confirmations: int) -> dict:
    head, last_block = _chain_position(session)
    cutoff = _confirmed_up_to(head, confirmations)
    total, confirmed, event_count = session.execute(
        select(
            func.sum(_stake_delta),
            func.sum(case((VaultEvent.block_number <= cutoff, _stake_delta), else_=0)),
            func.count(),
        ).select_from(VaultEvent)
    ).one()
    balances = (
        select(VaultEvent.wallet)
        .group_by(VaultEvent.wallet)
        .having(func.sum(_stake_delta) > 0)
        .subquery()
    )
    return {
        "total_staked": _amount(total),
        "confirmed_staked": _amount(confirmed),
        "active_stakers": session.scalar(select(func.count()).select_from(balances)),
        "event_count": event_count,
        "reorg_count": session.scalar(select(func.count()).select_from(Reorg)),
        "chain_head": head,
        "last_indexed_block": last_block,
        "blocks_behind": max(head - last_block, 0),
        "confirmations_required": confirmations,
    }


def wallet_position(session: Session, wallet: str, confirmations: int) -> dict:
    head, _ = _chain_position(session)
    cutoff = _confirmed_up_to(head, confirmations)
    staked, confirmed, claimed, event_count, last_activity = session.execute(
        select(
            func.sum(_stake_delta),
            func.sum(case((VaultEvent.block_number <= cutoff, _stake_delta), else_=0)),
            func.sum(case((VaultEvent.name == "RewardClaimed", VaultEvent.amount), else_=0)),
            func.count(),
            func.max(VaultEvent.block_time),
        ).where(VaultEvent.wallet == wallet)
    ).one()
    return {
        "wallet": wallet,
        "staked": _amount(staked),
        "confirmed_staked": _amount(confirmed),
        "rewards_claimed": _amount(claimed),
        "event_count": event_count,
        "last_activity": last_activity.isoformat() if last_activity else None,
    }


def recent_events(
    session: Session, confirmations: int, limit: int = 50, wallet: str | None = None
) -> list[dict]:
    head, _ = _chain_position(session)
    query = select(VaultEvent).order_by(VaultEvent.block_number.desc(), VaultEvent.log_index.desc())
    if wallet is not None:
        query = query.where(VaultEvent.wallet == wallet)
    rows = session.scalars(query.limit(limit)).all()

    events = []
    for row in rows:
        depth = max(head - row.block_number + 1, 0)
        events.append(
            {
                "name": row.name,
                "wallet": row.wallet,
                "amount": _amount(row.amount),
                "block_number": row.block_number,
                "block_time": row.block_time.isoformat(),
                "tx_hash": row.tx_hash,
                "confirmations": depth,
                "status": "confirmed" if depth >= confirmations else "pending",
            }
        )
    return events


def top_stakers(session: Session, limit: int = 10) -> list[dict]:
    staked = func.sum(_stake_delta)
    rows = session.execute(
        select(VaultEvent.wallet, staked)
        .group_by(VaultEvent.wallet)
        .having(staked > 0)
        .order_by(staked.desc(), VaultEvent.wallet)
        .limit(limit)
    ).all()
    return [{"wallet": wallet, "staked": _amount(amount)} for wallet, amount in rows]


def recent_reorgs(session: Session, limit: int = 10) -> list[dict]:
    rows = session.scalars(select(Reorg).order_by(Reorg.id.desc()).limit(limit)).all()
    return [
        {
            "detected_at": row.detected_at.isoformat(),
            "old_tip": row.old_tip,
            "ancestor_block": row.ancestor_block,
            "depth": row.depth,
            "events_removed": row.events_removed,
        }
        for row in rows
    ]


def staked_total_at(session: Session, block: int) -> int:
    total = session.scalar(select(func.sum(_stake_delta)).where(VaultEvent.block_number <= block))
    return int(total or 0)


def largest_stakes_at(session: Session, block: int, limit: int) -> list[tuple[str, int]]:
    staked = func.sum(_stake_delta)
    rows = session.execute(
        select(VaultEvent.wallet, staked)
        .where(VaultEvent.block_number <= block)
        .group_by(VaultEvent.wallet)
        .order_by(staked.desc(), VaultEvent.wallet)
        .limit(limit)
    ).all()
    return [(wallet, int(amount)) for wallet, amount in rows]


def latest_reconciliation(session: Session) -> dict | None:
    row = session.scalar(select(Reconciliation).order_by(Reconciliation.id.desc()).limit(1))
    if row is None:
        return None
    return {
        "checked_at": row.checked_at.isoformat(),
        "block_number": row.block_number,
        "ok": row.ok,
        "onchain_total": _amount(row.onchain_total),
        "indexed_total": _amount(row.indexed_total),
        "wallets_checked": row.wallets_checked,
        "wallets_mismatched": row.wallets_mismatched,
    }
