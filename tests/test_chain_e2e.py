"""The indexer against a real EVM node and the compiled contracts, including a real fork.

Needs `make chain-test` and `make build`. Skipped when the node is not reachable.
"""

import os

import pytest
from eth_account import Account
from sqlalchemy import select
from web3 import Web3

from app import queries
from app.chain import Web3Chain
from app.indexer import Indexer
from app.models import Reorg
from scripts.deploy import ANVIL_DEPLOYER_KEY, ARTIFACTS, E18, deploy, send
from scripts.simulate import STAKER_KEYS

pytestmark = pytest.mark.chain

RPC_URL = os.environ.get("TEST_RPC_URL", "http://127.0.0.1:8546")


@pytest.fixture
def w3():
    node = Web3(Web3.HTTPProvider(RPC_URL))
    if not node.is_connected():
        pytest.skip(f"no EVM node at {RPC_URL}")
    if not ARTIFACTS.exists():
        pytest.skip("contracts are not compiled")
    snapshot = node.provider.make_request("evm_snapshot", [])["result"]
    yield node
    node.provider.make_request("evm_revert", [snapshot])


@pytest.fixture
def deployed(w3):
    return deploy(w3, Account.from_key(ANVIL_DEPLOYER_KEY))


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
    indexer.sync_once()

    assert_matches_contract(sessions, deployed, [alice])
