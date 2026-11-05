"""Breaks safety rules one at a time and checks that the tests notice.

A test suite only proves something if it fails when the code is wrong. Run with `make mutation`.
Needs Postgres and the test chain. Takes several minutes, and restores each file afterwards.
"""

import pathlib
import signal
import subprocess
import sys

INDEXER_TESTS = "tests/test_indexer.py"
RESILIENCE_TESTS = "tests/test_resilience.py"
QUEUE_TESTS = "tests/test_txqueue.py"
PER_RUN_TIMEOUT_SECONDS = 120

MUTATIONS = [
    (
        "a lower head from a lagging node counts as a reorg",
        "app/indexer.py",
        "        if stored is None:\n            return False\n        canonical",
        "        if head < state.last_block:\n            return True\n"
        "        if stored is None:\n            return False\n        canonical",
        INDEXER_TESTS,
    ),
    (
        "forks are only compared at our tip",
        "app/indexer.py",
        ".where(IndexedBlock.number <= min(state.last_block, head))",
        ".where(IndexedBlock.number <= state.last_block)",
        INDEXER_TESTS,
    ),
    (
        "parent hash check removed",
        "app/indexer.py",
        "if previous is not None and previous.hash != block.parent_hash:",
        "if False:",
        INDEXER_TESTS,
    ),
    (
        "deep reorg limit removed",
        "app/indexer.py",
        'raise DeepReorgError(f"no common',
        'return self._start_block - 1\n        raise DeepReorgError(f"no common',
        INDEXER_TESTS,
    ),
    (
        "log range never shrinks",
        "app/indexer.py",
        "self._log_range_size = max(self._log_range_size // 2, 1)",
        "pass",
        INDEXER_TESTS,
    ),
    (
        "failover disabled",
        "app/chain.py",
        "enumerate(healthy + resting)",
        "enumerate([0])",
        RESILIENCE_TESTS,
    ),
    (
        "rpc url logged, leaking the api key",
        "app/chain.py",
        'log.warning("rpc endpoint %d failed: %s", index, type(exc).__name__)',
        'log.warning("rpc endpoint %d failed: %s", index, exc)',
        RESILIENCE_TESTS,
    ),
    (
        "second indexer allowed to run",
        "app/indexer.py",
        "if not acquired:",
        "if False:",
        RESILIENCE_TESTS,
    ),
    (
        "reconciliation always passes",
        "app/reconcile.py",
        "ok=onchain_total == indexed_total and not mismatched",
        "ok=True",
        RESILIENCE_TESTS,
    ),
    (
        "flat gas allowance removed",
        "app/chain.py",
        "GAS_BUFFER_FLAT = 70_000",
        "GAS_BUFFER_FLAT = 0",
        "tests/test_chain_e2e.py",
    ),
    (
        "broadcast before saving the attempt",
        "app/txqueue.py",
        '        tx.status = "pending"\n        # Commit before',
        '        self._broadcast(attempt)\n        tx.status = "pending"\n        # Commit before',
        QUEUE_TESTS,
    ),
    (
        "fee bump removed",
        "app/txqueue.py",
        "return int(value * Decimal(str(FEE_BUMP_PERCENT)) / 100) + 1",
        "return int(value)",
        QUEUE_TESTS,
    ),
    (
        "idempotency removed",
        "app/txqueue.py",
        '.on_conflict_do_nothing(index_elements=["idempotency_key"])',
        "",
        QUEUE_TESTS,
    ),
    (
        "reorged transaction not put back to pending",
        "app/txqueue.py",
        '            if tx.status == "mined":\n                # We had',
        "            if False:\n                # We had",
        QUEUE_TESTS,
    ),
    (
        "fee cap ignored",
        "app/txqueue.py",
        '        if max_fee > self._max_fee:\n            log.warning("network',
        '        if False:\n            log.warning("network',
        QUEUE_TESTS,
    ),
    (
        "used nonce not detected",
        "app/txqueue.py",
        "        if on_chain > tx.nonce:",
        "        if False:",
        QUEUE_TESTS,
    ),
    (
        "confirmations ignored",
        "app/txqueue.py",
        'if head - receipt["blockNumber"] + 1 >= self._confirmations:',
        "if True:",
        QUEUE_TESTS,
    ),
]


def _stop(signum, frame) -> None:
    raise KeyboardInterrupt  # Unwinds through `finally`, so the file being tested is restored.


def main() -> None:
    signal.signal(signal.SIGTERM, _stop)
    survived = 0
    for name, path, original, broken, tests in MUTATIONS:
        target = pathlib.Path(path)
        source = target.read_text()
        if original not in source:
            raise SystemExit(f"out of date, cannot find the code for: {name}")
        target.write_text(source.replace(original, broken, 1))
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", "-x", "--tb=no", tests],
                capture_output=True,
                timeout=PER_RUN_TIMEOUT_SECONDS,
            )
            caught = result.returncode == 1
        except subprocess.TimeoutExpired:
            caught = True
        finally:
            target.write_text(source)
        survived += not caught
        print(f"{'caught  ' if caught else 'SURVIVED'} {name}", flush=True)
    print(f"{len(MUTATIONS) - survived} of {len(MUTATIONS)} caught")
    if survived:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
