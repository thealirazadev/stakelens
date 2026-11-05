import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated

import anthropic
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from web3 import Web3

from app import assistant, auth, queries
from app.config import Settings, get_settings
from app.db import make_engine, make_session_factory

log = logging.getLogger("stakelens.api")
WEB_DIR = Path(__file__).parent / "web"


class ChallengeRequest(BaseModel):
    address: str

    @field_validator("address")
    @classmethod
    def checksum(cls, value: str) -> str:
        return _checksum(value)


class VerifyRequest(BaseModel):
    nonce: str = Field(max_length=32)
    signature: str = Field(max_length=200)


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=1000)


def _checksum(value: str) -> str:
    if not Web3.is_address(value):
        raise ValueError("not a valid Ethereum address")
    return Web3.to_checksum_address(value)


def _path_wallet(address: str) -> str:
    try:
        return _checksum(address)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def create_app(
    settings: Settings | None = None,
    engine: Engine | None = None,
    anthropic_client: anthropic.Anthropic | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    engine = engine or make_engine(settings.database_url)
    sessions = make_session_factory(engine)
    if anthropic_client is None and settings.anthropic_api_key:
        anthropic_client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    app = FastAPI(title="stakelens", version="0.1.0")

    def db() -> Iterator[Session]:
        with sessions.begin() as session:
            yield session

    Db = Annotated[Session, Depends(db)]

    def signed_in_wallet(session: Db, authorization: Annotated[str | None, Header()] = None) -> str:
        scheme, _, token = (authorization or "").partition(" ")
        wallet = auth.wallet_for_token(session, token) if scheme.lower() == "bearer" else None
        if wallet is None:
            raise HTTPException(401, "sign in required")
        return wallet

    Wallet = Annotated[str, Depends(signed_in_wallet)]
    Limit = Annotated[int, Query(ge=1, le=200)]

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/health")
    def health(session: Db) -> dict:
        stats = queries.vault_stats(session, settings.confirmations)
        return {
            "status": "ok",
            "chain_head": stats["chain_head"],
            "last_indexed_block": stats["last_indexed_block"],
            "blocks_behind": stats["blocks_behind"],
        }

    @app.get("/vault/stats")
    def vault_stats(session: Db) -> dict:
        return queries.vault_stats(session, settings.confirmations)

    @app.get("/vault/events")
    def vault_events(session: Db, limit: Limit = 50) -> list[dict]:
        return queries.recent_events(session, settings.confirmations, limit)

    @app.get("/vault/stakers")
    def vault_stakers(session: Db, limit: Limit = 10) -> list[dict]:
        return queries.top_stakers(session, limit)

    @app.get("/vault/reorgs")
    def vault_reorgs(session: Db, limit: Limit = 10) -> list[dict]:
        return queries.recent_reorgs(session, limit)

    @app.get("/wallets/{address}/position")
    def wallet_position(address: str, session: Db) -> dict:
        return queries.wallet_position(session, _path_wallet(address), settings.confirmations)

    @app.get("/wallets/{address}/events")
    def wallet_events(address: str, session: Db, limit: Limit = 50) -> list[dict]:
        return queries.recent_events(
            session, settings.confirmations, limit, wallet=_path_wallet(address)
        )

    @app.post("/auth/challenge")
    def auth_challenge(body: ChallengeRequest, session: Db) -> dict:
        challenge = auth.create_challenge(session, settings, body.address)
        return {"nonce": challenge.nonce, "message": challenge.message}

    @app.post("/auth/verify")
    def auth_verify(body: VerifyRequest) -> dict:
        # Own transaction: a bad signature must still burn the nonce, so it commits before the
        # error reaches the client rather than rolling back with the request.
        with sessions.begin() as session:
            try:
                token, wallet, expires_at = auth.verify_challenge(
                    session, settings, body.nonce, body.signature
                )
            except auth.AuthError as exc:
                failure = str(exc)
            else:
                return {"token": token, "wallet": wallet, "expires_at": expires_at.isoformat()}
        raise HTTPException(401, failure)

    @app.get("/me/position")
    def my_position(wallet: Wallet, session: Db) -> dict:
        return queries.wallet_position(session, wallet, settings.confirmations)

    @app.post("/assistant/ask")
    def assistant_ask(body: AskRequest, wallet: Wallet, session: Db) -> dict:
        if anthropic_client is None:
            raise HTTPException(503, "assistant is not configured: ANTHROPIC_API_KEY is not set")
        if assistant.calls_in_last_hour(session, wallet) >= settings.assistant_hourly_limit:
            raise HTTPException(429, "hourly question limit reached for this wallet")
        try:
            answer = assistant.ask(
                anthropic_client,
                settings.assistant_model,
                session,
                settings.confirmations,
                wallet,
                body.question,
            )
        except anthropic.RateLimitError as exc:
            raise HTTPException(503, "the model is rate limited, try again shortly") from exc
        except (anthropic.APIStatusError, anthropic.APIConnectionError) as exc:
            log.warning("anthropic call failed: %s", exc)
            raise HTTPException(502, "the model could not be reached") from exc
        except assistant.AssistantError as exc:
            raise HTTPException(502, str(exc)) from exc
        assistant.record_call(session, wallet, settings.assistant_model, answer)
        return {"answer": answer.text, "tools_used": answer.tools_used}

    return app


app = create_app()
