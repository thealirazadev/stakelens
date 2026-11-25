"""Sign-In with Ethereum (EIP-4361).

The server writes and stores the exact message the wallet signs. On verify it recovers the signer
from that stored text, so it never has to parse or trust a message sent back by the client.
Contract wallets (ERC-1271) are not supported.
"""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from eth_account import Account
from eth_account.messages import encode_defunct
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import AuthChallenge, AuthSession


class AuthError(Exception):
    pass


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def create_challenge(session: Session, settings: Settings, address: str) -> AuthChallenge:
    now = datetime.now(UTC)
    # Anyone can ask for a challenge, so finished ones are deleted as new ones are made instead
    # of piling up forever. An expired nonce is refused whether or not its row still exists.
    session.execute(delete(AuthChallenge).where(AuthChallenge.expires_at <= now))
    nonce = secrets.token_hex(16)
    expires_at = now + timedelta(seconds=settings.challenge_ttl_seconds)
    message = (
        f"{settings.siwe_domain} wants you to sign in with your Ethereum account:\n"
        f"{address}\n\n"
        "Sign in to stakelens. This does not send a transaction or cost gas.\n\n"
        f"URI: {settings.siwe_uri}\n"
        "Version: 1\n"
        f"Chain ID: {settings.chain_id}\n"
        f"Nonce: {nonce}\n"
        f"Issued At: {_iso(now)}\n"
        f"Expiration Time: {_iso(expires_at)}"
    )
    challenge = AuthChallenge(nonce=nonce, address=address, message=message, expires_at=expires_at)
    session.add(challenge)
    session.flush()
    return challenge


def verify_challenge(
    session: Session, settings: Settings, nonce: str, signature: str
) -> tuple[str, str, datetime]:
    now = datetime.now(UTC)
    # Consume the nonce atomically first: of two concurrent replays, exactly one gets a row.
    claimed = session.execute(
        update(AuthChallenge)
        .where(
            AuthChallenge.nonce == nonce,
            AuthChallenge.used_at.is_(None),
            AuthChallenge.expires_at > now,
        )
        .values(used_at=now)
        .returning(AuthChallenge.address, AuthChallenge.message)
    ).one_or_none()
    if claimed is None:
        raise AuthError("challenge is unknown, expired, or already used")

    address, message = claimed
    try:
        signer = Account.recover_message(encode_defunct(text=message), signature=signature)
    except Exception as exc:  # eth_account raises several unrelated types for malformed input.
        raise AuthError("signature could not be read") from exc
    if signer != address:
        raise AuthError("signature does not match the address")

    token = secrets.token_urlsafe(32)
    expires_at = now + timedelta(seconds=settings.session_ttl_seconds)
    # Same for sessions: an expired token is refused either way, so its row is only clutter.
    session.execute(delete(AuthSession).where(AuthSession.expires_at <= now))
    session.add(AuthSession(token_hash=_hash(token), address=address, expires_at=expires_at))
    return token, address, expires_at


def wallet_for_token(session: Session, token: str) -> str | None:
    return session.scalar(
        select(AuthSession.address).where(
            AuthSession.token_hash == _hash(token),
            AuthSession.expires_at > datetime.now(UTC),
        )
    )


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
