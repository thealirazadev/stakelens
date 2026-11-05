"""An in-memory chain that can fork, so reorg behaviour can be tested exactly."""

import hashlib
from collections import Counter
from datetime import UTC, datetime

from web3 import Web3

from app.chain import BlockRef, ChainEvent, LocatedEvent, RangeTooLarge

ALICE = Web3.to_checksum_address("0x0000000000000000000000000000000000000a11")
BOB = Web3.to_checksum_address("0x0000000000000000000000000000000000000b0b")


def _hash(*parts: object) -> str:
    return "0x" + hashlib.sha256(repr(parts).encode()).hexdigest()


class FakeChain:
    def __init__(self) -> None:
        self.blocks: list[BlockRef] = []
        self.logs: dict[str, list[ChainEvent]] = {}
        self._salt = 0
        self.calls: Counter[str] = Counter()
        self.max_log_range: int | None = None
        self.hidden_blocks_above: int | None = None
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
        self.calls["head"] += 1
        return self._visible_head()

    def _visible_head(self) -> int:
        real = len(self.blocks) - 1
        return real if self.hidden_blocks_above is None else min(real, self.hidden_blocks_above)

    def block(self, number: int) -> BlockRef | None:
        self.calls["block"] += 1
        return self.blocks[number] if 0 <= number <= self._visible_head() else None

    def events(self, block_hash: str) -> list[ChainEvent]:
        self.calls["events"] += 1
        return self.logs[block_hash]

    def events_in_range(self, first: int, last: int) -> list[LocatedEvent]:
        self.calls["range"] += 1
        if self.max_log_range is not None and last - first + 1 > self.max_log_range:
            raise RangeTooLarge(f"limited to {self.max_log_range} blocks")
        return [
            LocatedEvent(block.number, block.hash, event)
            for block in self.blocks[first : last + 1]
            for event in self.logs[block.hash]
        ]

    def _deltas(self, block: int) -> Counter[str]:
        balances: Counter[str] = Counter()
        for item in self.blocks[: block + 1]:
            for event in self.logs[item.hash]:
                sign = {"Staked": 1, "Unstaked": -1}.get(event.name, 0)
                balances[event.wallet] += sign * event.amount
        return balances

    def total_staked(self, block: int) -> int:
        return sum(self._deltas(block).values())

    def staked_of(self, wallet: str, block: int) -> int:
        return self._deltas(block)[wallet]
