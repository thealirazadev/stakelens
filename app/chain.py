"""The only module that talks to an Ethereum node."""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from web3 import Web3
from web3.exceptions import BlockNotFound

EVENT_SIGNATURES = {
    "Staked": "Staked(address,uint256)",
    "Unstaked": "Unstaked(address,uint256)",
    "RewardClaimed": "RewardClaimed(address,uint256)",
}
EVENT_NAME_BY_TOPIC = {
    Web3.keccak(text=signature).to_0x_hex(): name for name, signature in EVENT_SIGNATURES.items()
}


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


class ChainReader(Protocol):
    def head_number(self) -> int: ...

    def block(self, number: int) -> BlockRef | None: ...

    def events(self, block_hash: str) -> list[ChainEvent]: ...


class Web3Chain:
    def __init__(self, rpc_url: str, vault_address: str) -> None:
        self._w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 15}))
        self._vault = Web3.to_checksum_address(vault_address)

    def head_number(self) -> int:
        return self._w3.eth.block_number

    def block(self, number: int) -> BlockRef | None:
        try:
            raw = self._w3.eth.get_block(number)
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
        logs = self._w3.eth.get_logs(
            {
                "blockHash": block_hash,
                "address": self._vault,
                "topics": [list(EVENT_NAME_BY_TOPIC)],
            }
        )
        return [decode_log(log) for log in logs]


def decode_log(log) -> ChainEvent:
    return ChainEvent(
        tx_hash=log["transactionHash"].to_0x_hex(),
        log_index=log["logIndex"],
        name=EVENT_NAME_BY_TOPIC[log["topics"][0].to_0x_hex()],
        wallet=Web3.to_checksum_address(log["topics"][1][-20:]),
        amount=int.from_bytes(log["data"], "big"),
    )
