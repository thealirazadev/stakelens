"""Verifies the deployed contracts on Etherscan, using the addresses in .env.deployment.

python -m scripts.verify            submit both contracts (needs ETHERSCAN_API_KEY)
python -m scripts.verify --check    build the submissions without sending anything
"""

import argparse
import os
import subprocess
from pathlib import Path

from eth_abi import encode
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.config import get_settings
from scripts.deploy import INITIAL_SUPPLY, REWARD_RATE_PER_SECOND

CONTRACTS = Path(__file__).parent.parent / "contracts"
FOUNDRY_IMAGE = "ghcr.io/foundry-rs/foundry:stable"


class VerifySettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    etherscan_api_key: str = ""


def token_constructor_args() -> str:
    return encode(["uint256"], [INITIAL_SUPPLY]).hex()


def vault_constructor_args(token_address: str) -> str:
    return encode(["address", "uint256"], [token_address, REWARD_RATE_PER_SECOND]).hex()


def forge_verify(
    address: str, contract: str, constructor_args: str, chain_id: int, api_key: str, check: bool
) -> subprocess.CompletedProcess:
    command = [
        "docker",
        "run",
        "--rm",
        "-u",
        f"{os.getuid()}:{os.getgid()}",
        "-e",
        "HOME=/work/.foundry-home",
        # Passing the name alone reads the value from our environment, so the key never appears
        # in the process list.
        "-e",
        "ETHERSCAN_API_KEY",
        "-v",
        f"{CONTRACTS}:/work",
        "-w",
        "/work",
        "--entrypoint",
        "forge",
        FOUNDRY_IMAGE,
        "verify-contract",
        address,
        contract,
        "--chain-id",
        str(chain_id),
        "--constructor-args",
        constructor_args,
    ]
    command.append("--show-standard-json-input" if check else "--watch")
    return subprocess.run(
        command, env={**os.environ, "ETHERSCAN_API_KEY": api_key}, capture_output=check, text=True
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    settings = get_settings()
    token = os.environ.get("TOKEN_ADDRESS") or settings.token_address
    if not (token and settings.vault_address):
        raise SystemExit("no deployment found. Run scripts/deploy.py first.")
    api_key = VerifySettings().etherscan_api_key
    if not api_key and not args.check:
        raise SystemExit("ETHERSCAN_API_KEY is not set")

    targets = [
        (token, "src/StakeToken.sol:StakeToken", token_constructor_args()),
        (
            settings.vault_address,
            "src/StakingVault.sol:StakingVault",
            vault_constructor_args(token),
        ),
    ]
    for address, contract, constructor_args in targets:
        result = forge_verify(
            address, contract, constructor_args, settings.chain_id, api_key, args.check
        )
        if result.returncode != 0:
            raise SystemExit(f"verification failed for {contract}: {result.stderr or ''}")
        print(f"{contract} ok")


if __name__ == "__main__":
    main()
