from datetime import UTC, datetime, timedelta

from eth_account import Account
from eth_account.messages import encode_defunct
from sqlalchemy import update

from app.indexer import Indexer
from app.models import AuthChallenge, AuthSession
from tests.fakes import ALICE, BOB, FakeChain

E18 = 10**18
WALLET = Account.create()
OTHER_WALLET = Account.create()


def sign(account, message: str) -> str:
    return account.sign_message(encode_defunct(text=message)).signature.to_0x_hex()


def challenge(client, account=WALLET) -> dict:
    response = client.post("/auth/challenge", json={"address": account.address})
    assert response.status_code == 200
    return response.json()


def sign_in(client, account=WALLET) -> str:
    issued = challenge(client, account)
    response = client.post(
        "/auth/verify",
        json={"nonce": issued["nonce"], "signature": sign(account, issued["message"])},
    )
    assert response.status_code == 200
    return response.json()["token"]


def test_challenge_message_follows_eip_4361(client, settings):
    message = challenge(client)["message"]

    lines = message.split("\n")
    assert lines[0] == f"{settings.siwe_domain} wants you to sign in with your Ethereum account:"
    assert lines[1] == WALLET.address
    assert "Version: 1" in lines
    assert f"Chain ID: {settings.chain_id}" in lines


def test_valid_signature_signs_in(client):
    token = sign_in(client)

    response = client.get("/me/position", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json()["wallet"] == WALLET.address


def test_signature_from_another_wallet_is_rejected(client):
    issued = challenge(client)

    response = client.post(
        "/auth/verify",
        json={"nonce": issued["nonce"], "signature": sign(OTHER_WALLET, issued["message"])},
    )

    assert response.status_code == 401


def test_nonce_cannot_be_replayed(client):
    issued = challenge(client)
    body = {"nonce": issued["nonce"], "signature": sign(WALLET, issued["message"])}

    assert client.post("/auth/verify", json=body).status_code == 200
    assert client.post("/auth/verify", json=body).status_code == 401


def test_failed_attempt_burns_the_nonce(client):
    issued = challenge(client)
    bad = {"nonce": issued["nonce"], "signature": sign(OTHER_WALLET, issued["message"])}
    good = {"nonce": issued["nonce"], "signature": sign(WALLET, issued["message"])}

    assert client.post("/auth/verify", json=bad).status_code == 401
    assert client.post("/auth/verify", json=good).status_code == 401


def test_malformed_signature_is_rejected_not_a_server_error(client):
    issued = challenge(client)

    response = client.post("/auth/verify", json={"nonce": issued["nonce"], "signature": "0xnothex"})

    assert response.status_code == 401


def test_expired_challenge_is_rejected(client, sessions):
    issued = challenge(client)
    with sessions.begin() as session:
        session.execute(
            update(AuthChallenge).values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )

    response = client.post(
        "/auth/verify",
        json={"nonce": issued["nonce"], "signature": sign(WALLET, issued["message"])},
    )

    assert response.status_code == 401


def test_expired_session_is_rejected(client, sessions):
    token = sign_in(client)
    with sessions.begin() as session:
        session.execute(
            update(AuthSession).values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )

    response = client.get("/me/position", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401


def test_session_tokens_are_not_stored_in_plain_text(client, sessions):
    token = sign_in(client)
    with sessions() as session:
        stored = session.query(AuthSession).one()
    assert stored.token_hash != token


def test_protected_routes_need_a_token(client):
    assert client.get("/me/position").status_code == 401
    assert client.post("/assistant/ask", json={"question": "hi"}).status_code == 401


def test_invalid_addresses_are_rejected(client):
    assert client.post("/auth/challenge", json={"address": "0x123"}).status_code == 422
    assert client.get("/wallets/not-an-address/position").status_code == 422


def test_public_read_endpoints(client, sessions):
    chain = FakeChain()
    chain.mine(("Staked", ALICE, 100 * E18), ("Staked", BOB, 50 * E18))
    chain.mine(("Unstaked", BOB, 50 * E18))
    chain.mine()
    chain.mine()
    while Indexer(sessions, chain).sync_once().blocks:
        pass

    stats = client.get("/vault/stats").json()
    assert stats["total_staked"]["tokens"] == "100"
    assert stats["active_stakers"] == 1
    assert stats["blocks_behind"] == 0

    assert client.get("/vault/stakers").json() == [
        {"wallet": ALICE, "staked": {"wei": str(100 * E18), "tokens": "100"}}
    ]
    events = client.get("/vault/events", params={"limit": 1}).json()
    assert events[0]["name"] == "Unstaked"
    assert client.get(f"/wallets/{ALICE.lower()}/position").json()["wallet"] == ALICE
    assert client.get("/health").json()["status"] == "ok"
