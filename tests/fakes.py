"""An in-memory chain that can fork, so reorg behaviour can be tested exactly."""

import hashlib
from datetime import UTC, datetime

from web3 import Web3

from app.chain import BlockRef, ChainEvent

ALICE = Web3.to_checksum_address("0x0000000000000000000000000000000000000a11")
BOB = Web3.to_checksum_address("0x0000000000000000000000000000000000000b0b")


def _hash(*parts: object) -> str:
    return "0x" + hashlib.sha256(repr(parts).encode()).hexdigest()


class FakeChain:
    def __init__(self) -> None:
        self.blocks: list[BlockRef] = []
        self.logs: dict[str, list[ChainEvent]] = {}
        self._salt = 0
        self.mine()  # genesis

    def mine(self, *events: tuple[str, str, int]) -> BlockRef:
        number = len(self.blocks)
        parent = self.blocks[-1].hash if self.blocks else "0x" + "0" * 64
        block = BlockRef(
            number=number,
            hash=_hash(number, parent, self._salt, events),
            parent_hash=parent,
            timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        )
        self.blocks.append(block)
        self.logs[block.hash] = [
            ChainEvent(
                tx_hash=_hash("tx", block.hash, index),
                log_index=index,
                name=name,
                wallet=wallet,
                amount=amount,
            )
            for index, (name, wallet, amount) in enumerate(events)
        ]
        return block

    def fork_from(self, number: int) -> None:
        """Drop everything above `number`. Later mine() calls build a competing branch."""
        del self.blocks[number + 1 :]
        self._salt += 1

    def head_number(self) -> int:
        return len(self.blocks) - 1

    def block(self, number: int) -> BlockRef | None:
        return self.blocks[number] if 0 <= number < len(self.blocks) else None

    def events(self, block_hash: str) -> list[ChainEvent]:
        return self.logs[block_hash]
