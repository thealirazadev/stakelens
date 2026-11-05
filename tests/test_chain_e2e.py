"""The indexer against a real EVM node and the compiled contracts, including a real fork.

Needs `make chain-test` and `make build`. Skipped when the node is not reachable.
"""

import pytest
from eth_account import Account
from sqlalchemy import select

from app import queries
from app.chain import GAS_BUFFER_PERCENT, Web3Chain
from app.indexer import Indexer
from app.models import Reorg
from scripts.deploy import ANVIL_DEPLOYER_KEY, E18, send
from scripts.simulate import STAKER_KEYS
from tests.conftest import RPC_URL

pytestmark = pytest.mark.chain


def stake(w3, deployed, account, tokens):
    wei = tokens * E18
    deployer = Account.from_key(ANVIL_DEPLOYER_KEY)
    send(w3, deployer, deployed.token.functions.transfer(account.address, wei))
    send(w3, account, deployed.token.functions.approve(deployed.vault.address, wei))
    send(w3, account, deployed.vault.functions.stake(wei))


def drain(indexer):
    while indexer.sync_once().blocks:
        pass


def assert_matches_contract(sessions, deployed, accounts):
    with sessions() as session:
        total = int(queries.vault_stats(session, 1)["total_staked"]["wei"])
        assert total == deployed.vault.functions.totalStaked().call()
        for account in accounts:
            indexed = queries.wallet_position(session, account.address, 1)
            on_chain = deployed.vault.functions.stakedOf(account.address).call()
            assert int(indexed["staked"]["wei"]) == on_chain


def make_indexer(sessions, deployed):
    return Indexer(
        sessions, Web3Chain(RPC_URL, deployed.vault.address), start_block=deployed.start_block
    )


def test_decodes_real_events_and_matches_contract_state(sessions, w3, deployed):
    alice, bob = (Account.from_key(key) for key in STAKER_KEYS[:2])
    stake(w3, deployed, alice, 100)
    stake(w3, deployed, bob, 40)
    send(w3, bob, deployed.vault.functions.unstake(15 * E18))
    w3.provider.make_request("evm_increaseTime", [100])
    w3.provider.make_request("evm_mine", [])
    send(w3, alice, deployed.vault.functions.claim())

    drain(make_indexer(sessions, deployed))

    assert_matches_contract(sessions, deployed, [alice, bob])
    with sessions() as session:
        events = queries.recent_events(session, 1)
        position = queries.wallet_position(session, alice.address, 1)
    assert [event["name"] for event in events] == ["RewardClaimed", "Unstaked", "Staked", "Staked"]
    assert events[1]["amount"]["tokens"] == "15"
    assert events[1]["wallet"] == bob.address
    assert int(position["rewards_claimed"]["wei"]) > 0


def test_real_fork_rolls_back_orphaned_stake(sessions, w3, deployed):
    alice, bob = (Account.from_key(key) for key in STAKER_KEYS[:2])
    stake(w3, deployed, alice, 100)
    indexer = make_indexer(sessions, deployed)
    drain(indexer)

    fork_point = w3.provider.make_request("evm_snapshot", [])["result"]
    stake(w3, deployed, bob, 999)
    drain(indexer)
    with sessions() as session:
        assert queries.wallet_position(session, bob.address, 1)["staked"]["tokens"] == "999"

    w3.provider.make_request("evm_revert", [fork_point])
    stake(w3, deployed, bob, 5)
    stake(w3, deployed, alice, 1)
    drain(indexer)

    assert_matches_contract(sessions, deployed, [alice, bob])
    with sessions() as session:
        assert queries.wallet_position(session, bob.address, 1)["staked"]["tokens"] == "5"
        assert session.scalar(select(Reorg.events_removed)) == 1


def test_fork_to_a_shorter_chain_rolls_back(sessions, w3, deployed):
    alice = Account.from_key(STAKER_KEYS[0])
    fork_point = w3.provider.make_request("evm_snapshot", [])["result"]
    stake(w3, deployed, alice, 50)
    indexer = make_indexer(sessions, deployed)
    drain(indexer)

    w3.provider.make_request("evm_revert", [fork_point])
    w3.provider.make_request("evm_mine", [])
    drain(indexer)

    assert_matches_contract(sessions, deployed, [alice])


def test_gas_buffer_covers_the_worst_case_for_a_new_staker(w3, deployed):
    """The estimate sees no elapsed time, but the transaction lands after some has passed."""
    deployer = Account.from_key(ANVIL_DEPLOYER_KEY)
    alice, bob = (Account.from_key(key) for key in STAKER_KEYS[:2])
    for account in (alice, bob):
        send(w3, deployer, deployed.token.functions.transfer(account.address, 1000 * E18))
        send(w3, account, deployed.token.functions.approve(deployed.vault.address, 1000 * E18))
    send(w3, alice, deployed.vault.functions.stake(100 * E18))

    w3.provider.make_request("anvil_setBlockTimestampInterval", [0])
    call = deployed.vault.functions.stake(5 * E18)
    estimate = call.estimate_gas({"from": bob.address})
    w3.provider.make_request("anvil_removeBlockTimestampInterval", [])
    w3.provider.make_request("evm_increaseTime", [50])
    w3.provider.make_request("evm_mine", [])

    tx = call.build_transaction(
        {
            "from": bob.address,
            "nonce": w3.eth.get_transaction_count(bob.address),
            "gas": estimate * GAS_BUFFER_PERCENT // 100,
        }
    )
    receipt = w3.eth.wait_for_transaction_receipt(
        w3.eth.send_raw_transaction(bob.sign_transaction(tx).raw_transaction)
    )

    assert receipt["status"] == 1
    assert receipt["gasUsed"] > estimate  # Proves this really is the expensive path.
