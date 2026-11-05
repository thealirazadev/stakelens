"""Deploy the token and vault, fund the reward pool, and write .env.deployment."""

import json
from dataclasses import dataclass
from pathlib import Path

from eth_account import Account
from eth_account.signers.local import LocalAccount
from pydantic_settings import BaseSettings, SettingsConfigDict
from web3 import Web3
from web3.contract import Contract

from app.config import get_settings

ARTIFACTS = Path(__file__).parent.parent / "contracts" / "out"
ENV_FILE = Path(__file__).parent.parent / ".env.deployment"

E18 = 10**18
INITIAL_SUPPLY = 10_000_000 * E18
REWARD_POOL = 100_000 * E18
REWARD_RATE_PER_SECOND = E18 // 10

GAS_BUFFER_PERCENT = 130

ANVIL_CHAIN_ID = 31337
# anvil's first default account. Publicly known; never use it outside a local chain.
ANVIL_DEPLOYER_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"


class DeployerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    deployer_private_key: str = ""


@dataclass(frozen=True)
class Deployment:
    token: Contract
    vault: Contract
    start_block: int


def artifact(contract: str) -> tuple[list, str]:
    data = json.loads((ARTIFACTS / f"{contract}.sol" / f"{contract}.json").read_text())
    return data["abi"], data["bytecode"]["object"]


def load_contract(w3: Web3, name: str, address: str) -> Contract:
    abi, _ = artifact(name)
    return w3.eth.contract(address=address, abi=abi)


def send(w3: Web3, account: LocalAccount, call) -> dict:
    """Sign locally, send, and wait. Raises if the transaction reverted."""
    params = {"from": account.address, "nonce": w3.eth.get_transaction_count(account.address)}
    # The estimate runs against the current block. Vault calls cost more once time has passed,
    # because the reward accumulator goes from zero to non-zero, so a bare estimate can run out.
    params["gas"] = call.estimate_gas(params) * GAS_BUFFER_PERCENT // 100
    signed = account.sign_transaction(call.build_transaction(params))
    receipt = w3.eth.wait_for_transaction_receipt(
        w3.eth.send_raw_transaction(signed.raw_transaction)
    )
    if receipt["status"] != 1:
        raise RuntimeError(f"transaction reverted: {receipt['transactionHash'].to_0x_hex()}")
    return receipt


def _deploy_contract(w3: Web3, account: LocalAccount, name: str, *args) -> tuple[Contract, int]:
    abi, bytecode = artifact(name)
    receipt = send(w3, account, w3.eth.contract(abi=abi, bytecode=bytecode).constructor(*args))
    return w3.eth.contract(address=receipt["contractAddress"], abi=abi), receipt["blockNumber"]


def deploy(w3: Web3, account: LocalAccount) -> Deployment:
    token, _ = _deploy_contract(w3, account, "StakeToken", INITIAL_SUPPLY)
    vault, start_block = _deploy_contract(
        w3, account, "StakingVault", token.address, REWARD_RATE_PER_SECOND
    )
    send(w3, account, token.functions.transfer(vault.address, REWARD_POOL))
    return Deployment(token=token, vault=vault, start_block=start_block)


def main() -> None:
    settings = get_settings()
    w3 = Web3(Web3.HTTPProvider(settings.rpc_url))
    if w3.eth.chain_id != settings.chain_id:
        raise SystemExit(f"node is on chain {w3.eth.chain_id}, CHAIN_ID is {settings.chain_id}")

    key = DeployerSettings().deployer_private_key
    if not key:
        if settings.chain_id != ANVIL_CHAIN_ID:
            raise SystemExit("DEPLOYER_PRIVATE_KEY is required outside a local chain")
        key = ANVIL_DEPLOYER_KEY

    deployment = deploy(w3, Account.from_key(key))
    ENV_FILE.write_text(
        f"TOKEN_ADDRESS={deployment.token.address}\n"
        f"VAULT_ADDRESS={deployment.vault.address}\n"
        f"START_BLOCK={deployment.start_block}\n"
    )
    print(f"token  {deployment.token.address}")
    print(f"vault  {deployment.vault.address}")
    print(f"block  {deployment.start_block}")
    print(f"wrote  {ENV_FILE.name}")


if __name__ == "__main__":
    main()
