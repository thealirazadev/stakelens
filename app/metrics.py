"""Prometheus metrics.

The indexer and transaction worker publish process counters from their own metrics port. The API
publishes gauges read from the database on each scrape, so it reports correct figures even when
the worker processes are down, which is exactly when they matter.
"""

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import IndexerState, OutboundTx, Reconciliation, Reorg, VaultEvent

RPC_ERRORS = Counter("stakelens_rpc_errors_total", "Failed RPC calls by endpoint", ["endpoint"])
RPC_FAILOVERS = Counter(
    "stakelens_rpc_failovers_total", "Calls served by an endpoint other than the first choice"
)
BLOCKS_INDEXED = Counter("stakelens_blocks_indexed_total", "Blocks processed by the indexer")
EVENTS_INDEXED = Counter("stakelens_events_indexed_total", "Vault events written by the indexer")
REORGS = Counter("stakelens_reorgs_total", "Chain reorganisations rolled back")
SYNC_SECONDS = Histogram("stakelens_sync_seconds", "Duration of one indexer pass")
TX_BROADCASTS = Counter("stakelens_tx_broadcasts_total", "Transactions sent to a node")
TX_FEE_BUMPS = Counter("stakelens_tx_fee_bumps_total", "Stuck transactions replaced at higher fee")
TX_REORGED = Counter("stakelens_tx_reorged_total", "Mined transactions dropped by a reorg")


def render_database_metrics(session: Session) -> bytes:
    registry = CollectorRegistry()

    def gauge(name: str, description: str, value: float) -> None:
        Gauge(name, description, registry=registry).set(value)

    state = session.scalar(select(IndexerState))
    if state is not None:
        gauge("stakelens_chain_head", "Latest block seen on the chain", state.chain_head)
        gauge("stakelens_last_indexed_block", "Latest block indexed", state.last_block)
        gauge(
            "stakelens_blocks_behind",
            "Blocks between the chain head and the indexer",
            max(state.chain_head - state.last_block, 0),
        )
        gauge(
            "stakelens_indexer_heartbeat_age_seconds",
            "Seconds since the indexer last completed a pass",
            session.scalar(select(func.extract("epoch", func.now() - state.updated_at))),
        )

    gauge(
        "stakelens_events",
        "Indexed vault events",
        session.scalar(select(func.count()).select_from(VaultEvent)),
    )
    gauge(
        "stakelens_reorgs",
        "Reorgs recorded",
        session.scalar(select(func.count()).select_from(Reorg)),
    )
    gauge(
        "stakelens_deepest_reorg_blocks",
        "Deepest reorg recorded",
        session.scalar(select(func.coalesce(func.max(Reorg.depth), 0))),
    )

    latest = session.scalar(select(Reconciliation).order_by(Reconciliation.id.desc()).limit(1))
    if latest is not None:
        gauge("stakelens_reconcile_ok", "1 if the last check matched the contract", latest.ok)
        gauge(
            "stakelens_reconcile_age_seconds",
            "Seconds since the last check against the contract",
            session.scalar(select(func.extract("epoch", func.now() - latest.checked_at))),
        )

    open_statuses = ("queued", "pending", "mined")
    gauge(
        "stakelens_tx_open",
        "Outbound transactions not yet final",
        session.scalar(
            select(func.count()).select_from(OutboundTx).where(OutboundTx.status.in_(open_statuses))
        ),
    )
    gauge(
        "stakelens_tx_oldest_open_age_seconds",
        "Age of the oldest outbound transaction not yet final",
        session.scalar(
            select(
                func.coalesce(
                    func.extract("epoch", func.now() - func.min(OutboundTx.created_at)), 0
                )
            ).where(OutboundTx.status.in_(open_statuses))
        ),
    )
    return generate_latest(registry)
