"""Drive a local anvil chain so there is something to index.

activity  stakes, unstakes, and claims from three wallets
reorg     stakes, waits for it to be indexed, then forks the chain so that stake never happened
check     compares every indexed balance with what the contract reports
"""

import argparse
import time

from eth_account import Account
from eth_account.signers.local import LocalAccount
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import select
from web3 import Web3

from app import queries
from app.config import get_settings
from app.db import make_engine, make_session_factory
from app.models import VaultEvent
from scripts.deploy import ANVIL_CHAIN_ID, ANVIL_DEPLOYER_KEY, E18, load_contract, send

# anvil's default accounts 1 to 3. Publicly known; only meaningful on a local chain.
STAKER_KEYS = [
    "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d",
    "0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a",
    "0x7c852118294e51e653712a81e05800f419141751be58f605c371e15141b007a6",
]
INDEXER_WAIT_SECONDS = 15


class Deployed(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env.deployment", extra="ignore")

    token_address: str
    vault_address: str


class Sim:
    def __init__(self) -> None:
        settings = get_settings()
        if settings.chain_id != ANVIL_CHAIN_ID:
            raise SystemExit("simulate.py only runs against a local anvil chain")
        deployed = Deployed()
        self.w3 = Web3(Web3.HTTPProvider(settings.rpc_url))
        self.token = load_contract(self.w3, "StakeToken", deployed.token_address)
        self.vault = load_contract(self.w3, "StakingVault", deployed.vault_address)
        self.deployer = Account.from_key(ANVIL_DEPLOYER_KEY)
        self.stakers = [Account.from_key(key) for key in STAKER_KEYS]
        self.sessions = make_session_factory(make_engine(settings.database_url))

    def stake(self, account: LocalAccount, tokens: int) -> dict:
        wei = tokens * E18
        if self.token.functions.balanceOf(account.address).call() < wei:
            send(self.w3, self.deployer, self.token.functions.transfer(account.address, wei))
        send(self.w3, account, self.token.functions.approve(self.vault.address, wei))
        receipt = send(self.w3, account, self.vault.functions.stake(wei))
        print(f"{account.address}  stake {tokens} STK  block {receipt['blockNumber']}")
        return receipt

    def unstake(self, account: LocalAccount, tokens: int) -> None:
        receipt = send(self.w3, account, self.vault.functions.unstake(tokens * E18))
        print(f"{account.address}  unstake {tokens} STK  block {receipt['blockNumber']}")

    def claim(self, account: LocalAccount) -> None:
        receipt = send(self.w3, account, self.vault.functions.claim())
        print(f"{account.address}  claim  block {receipt['blockNumber']}")

    def wait_until_indexed(self, tx_hash: str) -> None:
        deadline = time.monotonic() + INDEXER_WAIT_SECONDS
        while time.monotonic() < deadline:
            with self.sessions() as session:
                found = session.scalar(select(VaultEvent.id).where(VaultEvent.tx_hash == tx_hash))
            if found:
                return
            time.sleep(0.5)
        raise SystemExit("the indexer did not pick up the transaction. Is `make indexer` running?")


def activity(sim: Sim) -> None:
    alice, bob, carol = sim.stakers
    sim.stake(alice, 1200)
    sim.stake(bob, 450)
    sim.stake(carol, 75)
    sim.unstake(bob, 150)
    time.sleep(3)
    sim.claim(alice)


def reorg(sim: Sim) -> None:
    carol = sim.stakers[2]
    snapshot = sim.w3.provider.make_request("evm_snapshot", [])["result"]

    print("Staking 999 STK on a branch that is about to be abandoned:")
    receipt = sim.stake(carol, 999)
    sim.wait_until_indexed(receipt["transactionHash"].to_0x_hex())
    print("Indexed. It should appear as pending in the dashboard.")
    time.sleep(4)

    print("Forking the chain back to before that stake...")
    sim.w3.provider.make_request("evm_revert", [snapshot])
    print("On the new branch the same wallet stakes 5 STK instead:")
    receipt = sim.stake(carol, 5)
    sim.wait_until_indexed(receipt["transactionHash"].to_0x_hex())
    print("Indexed. The 999 STK event is gone, and the rollback is listed under reorgs.")


def check(sim: Sim) -> None:
    mismatches = 0
    with sim.sessions() as session:
        indexed_total = int(queries.vault_stats(session, 1)["total_staked"]["wei"])
        for account in sim.stakers:
            on_chain = sim.vault.functions.stakedOf(account.address).call()
            indexed = int(queries.wallet_position(session, account.address, 1)["staked"]["wei"])
            mismatches += on_chain != indexed
            print(f"{account.address}  chain {on_chain / E18:g}  indexed {indexed / E18:g}")
    on_chain_total = sim.vault.functions.totalStaked().call()
    mismatches += on_chain_total != indexed_total
    print(f"total  chain {on_chain_total / E18:g}  indexed {indexed_total / E18:g}")
    if mismatches:
        raise SystemExit("MISMATCH")
    print("indexed balances match the contract")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["activity", "reorg", "check"])
    command = parser.parse_args().command
    {"activity": activity, "reorg": reorg, "check": check}[command](Sim())


if __name__ == "__main__":
    main()
