"""Transaction worker process and its command line.

python -m app.txworker run
python -m app.txworker fund-rewards --tokens 1000 --key topup-2026-11
python -m app.txworker list
"""

import argparse
import logging

from prometheus_client import start_http_server
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import select
from web3 import Web3

from app.chain import Endpoints
from app.config import get_settings
from app.db import make_engine, make_session_factory
from app.models import OutboundTx
from app.txqueue import GWEI, LocalKeySigner, TxWorker, enqueue, run_forever

TOKEN_DECIMALS = 18
TRANSFER_ABI = [
    {
        "type": "function",
        "name": "transfer",
        "stateMutability": "nonpayable",
        "inputs": [{"name": "to", "type": "address"}, {"name": "amount", "type": "uint256"}],
        "outputs": [{"type": "bool"}],
    }
]


class SignerSettings(BaseSettings):
    """Kept apart from Settings so the API process never reads the key."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    tx_signer_private_key: str = ""


def load_signer() -> LocalKeySigner:
    key = SignerSettings().tx_signer_private_key
    if not key:
        raise SystemExit("TX_SIGNER_PRIVATE_KEY is not set")
    return LocalKeySigner(key)


def transfer_calldata(to_address: str, amount: int) -> str:
    contract = Web3().eth.contract(abi=TRANSFER_ABI)
    return contract.encode_abi("transfer", [Web3.to_checksum_address(to_address), amount])


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("run")
    commands.add_parser("list")
    fund = commands.add_parser("fund-rewards")
    fund.add_argument("--tokens", type=int, required=True)
    fund.add_argument("--key", required=True, help="repeating a key never sends twice")
    args = parser.parse_args()

    settings = get_settings()
    engine = make_engine(settings.database_url)
    sessions = make_session_factory(engine)

    if args.command == "list":
        with sessions() as session:
            for tx in session.scalars(select(OutboundTx).order_by(OutboundTx.id)):
                print(tx.id, tx.status, tx.idempotency_key, tx.nonce, tx.winning_hash or "-")
    elif args.command == "fund-rewards":
        data = transfer_calldata(settings.vault_address, args.tokens * 10**TOKEN_DECIMALS)
        with sessions.begin() as session:
            tx = enqueue(session, args.key, load_signer().address, settings.token_address, data)
            print(f"tx {tx.id} is {tx.status}")
    else:
        if settings.tx_metrics_port:
            start_http_server(settings.tx_metrics_port, addr="127.0.0.1")
        worker = TxWorker(
            sessions,
            Endpoints(settings.rpc_urls),
            load_signer(),
            settings.chain_id,
            confirmations=settings.confirmations,
            stuck_seconds=settings.tx_stuck_seconds,
            max_fee_per_gas=settings.tx_max_fee_gwei * GWEI,
        )
        run_forever(engine, worker, settings.tx_poll_seconds)


if __name__ == "__main__":
    main()
