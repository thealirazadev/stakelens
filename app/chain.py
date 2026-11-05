"""The only module the indexer uses to read from an Ethereum node."""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, TypeVar

import requests
from web3 import Web3
from web3.exceptions import (
    BlockNotFound,
    ContractLogicError,
    TransactionNotFound,
    Web3Exception,
    Web3RPCError,
)

from app.metrics import RPC_ERRORS, RPC_FAILOVERS

log = logging.getLogger("stakelens.chain")

T = TypeVar("T")

# Gas is estimated against the current block. A vault call landing later costs more, because the
# reward accumulator and the user's checkpoint go from zero to non-zero. The measured worst case
# is 41% above the estimate (tests/test_chain_e2e.py). Unused gas is refunded.
GAS_BUFFER_PERCENT = 160

EVENT_SIGNATURES = {
    "Staked": "Staked(address,uint256)",
    "Unstaked": "Unstaked(address,uint256)",
    "RewardClaimed": "RewardClaimed(address,uint256)",
}
EVENT_NAME_BY_TOPIC = {
    Web3.keccak(text=signature).to_0x_hex(): name for name, signature in EVENT_SIGNATURES.items()
}
VAULT_READ_ABI = [
    {
        "type": "function",
        "name": "totalStaked",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "uint256"}],
    },
    {
        "type": "function",
        "name": "stakedOf",
        "stateMutability": "view",
        "inputs": [{"name": "user", "type": "address"}],
        "outputs": [{"type": "uint256"}],
    },
]

# Providers word this differently. Rate limits are excluded on purpose: they should fail over,
# and a smaller range would not help.
_RANGE_ERROR_HINTS = ("range", "too many", "more than", "limit exceeded", "exceeds")
_NOT_RANGE_HINTS = ("rate", "compute unit", "capacity", "credits")


class RpcUnavailable(RuntimeError):
    """Every configured endpoint failed. Safe to retry later."""


class RangeTooLarge(Exception):
    """The provider refused the size of an eth_getLogs block range."""


@dataclass(frozen=True)
class BlockRef:
    number: int
    hash: str
    parent_hash: str
    timestamp: datetime


@dataclass(frozen=True)
class ChainEvent:
    tx_hash: str
    log_index: int
    name: str
    wallet: str
    amount: int


@dataclass(frozen=True)
class LocatedEvent:
    block_number: int
    block_hash: str
    event: ChainEvent


class ChainReader(Protocol):
    def head_number(self) -> int: ...

    def block(self, number: int) -> BlockRef | None: ...

    def events(self, block_hash: str) -> list[ChainEvent]: ...

    def events_in_range(self, first: int, last: int) -> list[LocatedEvent]: ...

    def total_staked(self, block: int) -> int: ...

    def staked_of(self, wallet: str, block: int) -> int: ...


class Endpoints:
    """Sends each call to the first healthy node, in the order configured.

    A failed endpoint is left alone for a while, then tried again so traffic returns to the
    primary. RPC URLs usually contain an API key, so endpoints are only ever logged by position.
    """

    def __init__(self, urls: list[str], retry_after_seconds: float = 60.0) -> None:
        if not urls:
            raise ValueError("at least one RPC URL is required")
        # web3's built-in retries are off: moving to the next endpoint is faster than waiting.
        self.clients = [
            Web3(
                Web3.HTTPProvider(
                    url, request_kwargs={"timeout": 15}, exception_retry_configuration=None
                )
            )
            for url in urls
        ]
        self._retry_after = retry_after_seconds
        self._down_until = [0.0] * len(urls)

    def call(self, fn: Callable[[Web3], T]) -> T:
        now = time.monotonic()
        healthy = [i for i, until in enumerate(self._down_until) if until <= now]
        resting = [i for i, until in enumerate(self._down_until) if until > now]

        for position, index in enumerate(healthy + resting):
            try:
                result = fn(self.clients[index])
            except (
                BlockNotFound,
                TransactionNotFound,
                ContractLogicError,
                RangeTooLarge,
            ):
                raise  # Real answers from a working node, not endpoint failures.
            except (requests.RequestException, Web3Exception, ValueError) as exc:
                RPC_ERRORS.labels(endpoint=str(index)).inc()
                self._down_until[index] = time.monotonic() + self._retry_after
                log.warning("rpc endpoint %d failed: %s", index, type(exc).__name__)
                continue
            if position > 0:
                RPC_FAILOVERS.inc()
            self._down_until[index] = 0.0
            return result
        raise RpcUnavailable(f"all {len(self.clients)} rpc endpoints failed")


class Web3Chain:
    def __init__(self, rpc_urls: str | list[str], vault_address: str) -> None:
        urls = [rpc_urls] if isinstance(rpc_urls, str) else rpc_urls
        self._endpoints = Endpoints(urls)
        self._vault = Web3.to_checksum_address(vault_address)

    def _vault_contract(self, w3: Web3):
        return w3.eth.contract(address=self._vault, abi=VAULT_READ_ABI)

    def head_number(self) -> int:
        return self._endpoints.call(lambda w3: w3.eth.block_number)

    def block(self, number: int) -> BlockRef | None:
        try:
            raw = self._endpoints.call(lambda w3: w3.eth.get_block(number))
        except BlockNotFound:
            return None
        return BlockRef(
            number=raw["number"],
            hash=raw["hash"].to_0x_hex(),
            parent_hash=raw["parentHash"].to_0x_hex(),
            timestamp=datetime.fromtimestamp(raw["timestamp"], UTC),
        )

    def events(self, block_hash: str) -> list[ChainEvent]:
        # Filtering by block hash rather than number means the node either answers for exactly
        # this block or errors out. It can never return logs from a competing fork.
        logs = self._endpoints.call(
            lambda w3: w3.eth.get_logs(
                {
                    "blockHash": block_hash,
                    "address": self._vault,
                    "topics": [list(EVENT_NAME_BY_TOPIC)],
                }
            )
        )
        return [decode_log(log_entry) for log_entry in logs]

    def events_in_range(self, first: int, last: int) -> list[LocatedEvent]:
        def query(w3: Web3):
            try:
                return w3.eth.get_logs(
                    {
                        "fromBlock": first,
                        "toBlock": last,
                        "address": self._vault,
                        "topics": [list(EVENT_NAME_BY_TOPIC)],
                    }
                )
            except Web3RPCError as exc:
                if is_range_error(str(exc)):
                    raise RangeTooLarge(str(exc)) from exc
                raise

        return [
            LocatedEvent(
                block_number=log_entry["blockNumber"],
                block_hash=log_entry["blockHash"].to_0x_hex(),
                event=decode_log(log_entry),
            )
            for log_entry in self._endpoints.call(query)
        ]

    def total_staked(self, block: int) -> int:
        return self._endpoints.call(
            lambda w3: self._vault_contract(w3).functions.totalStaked().call(block_identifier=block)
        )

    def staked_of(self, wallet: str, block: int) -> int:
        return self._endpoints.call(
            lambda w3: self._vault_contract(w3)
            .functions.stakedOf(wallet)
            .call(block_identifier=block)
        )


def is_range_error(message: str) -> bool:
    text = message.lower()
    if any(hint in text for hint in _NOT_RANGE_HINTS):
        return False
    return any(hint in text for hint in _RANGE_ERROR_HINTS)


def decode_log(log_entry) -> ChainEvent:
    return ChainEvent(
        tx_hash=log_entry["transactionHash"].to_0x_hex(),
        log_index=log_entry["logIndex"],
        name=EVENT_NAME_BY_TOPIC[log_entry["topics"][0].to_0x_hex()],
        wallet=Web3.to_checksum_address(log_entry["topics"][1][-20:]),
        amount=int.from_bytes(log_entry["data"], "big"),
    )
