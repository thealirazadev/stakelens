"""Answers questions about the vault using read-only tools over the indexed data.

The assistant holds no keys and has no tool that writes anything. Every figure it can quote comes
from app.queries, so the arithmetic happens in SQL and not in the model.
"""

import json
from dataclasses import dataclass, field

import anthropic
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from web3 import Web3

from app import queries
from app.models import AssistantCall

MAX_TURNS = 6
MAX_LIST_LIMIT = 50
FALLBACK_BETA = "server-side-fallback-2026-07-01"

SYSTEM_PROMPT = """You answer questions about one staking vault for its users.

Take every figure from the tools and quote it exactly. Never estimate or compute a number the \
tools did not return. If the tools cannot answer the question, say so.

A balance can include events that are still pending. When "staked" differs from \
"confirmed_staked", tell the user that the difference could still be undone by a chain reorg.

You can only read data. You cannot send transactions, move funds, or give investment advice. If \
asked to do any of these, say you cannot. "The signed-in wallet" refers to the address given at \
the start of the conversation.

Keep answers short and in plain language. Use token amounts, not wei."""

_ADDRESS = {"type": "string", "description": "0x-prefixed Ethereum address"}
_LIMIT = {"type": "integer", "description": f"Rows to return, at most {MAX_LIST_LIMIT}"}


def _tool(name: str, description: str, properties: dict) -> dict:
    return {
        "name": name,
        "description": description,
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": list(properties),
            "additionalProperties": False,
        },
    }


TOOLS = [
    _tool(
        "get_vault_stats",
        "Total and confirmed stake, active stakers, event and reorg counts, and how far the "
        "indexer is behind the chain head.",
        {},
    ),
    _tool(
        "get_wallet_position",
        "One wallet's staked balance, its confirmed part, rewards claimed, and last activity.",
        {"address": _ADDRESS},
    ),
    _tool(
        "get_wallet_events",
        "One wallet's most recent stake, unstake, and claim events, newest first, each with "
        "confirmation count and status.",
        {"address": _ADDRESS, "limit": _LIMIT},
    ),
    _tool(
        "get_top_stakers",
        "Wallets with the largest current stake, largest first.",
        {"limit": _LIMIT},
    ),
    _tool(
        "get_recent_reorgs",
        "Chain reorganisations the indexer handled, newest first: depth and events removed.",
        {"limit": _LIMIT},
    ),
]


class AssistantError(Exception):
    pass


@dataclass
class Answer:
    text: str
    tools_used: list[str] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0


def _clamp(limit: int) -> int:
    return max(1, min(limit, MAX_LIST_LIMIT))


def run_tool(session: Session, confirmations: int, name: str, args: dict) -> dict | list:
    if name == "get_vault_stats":
        return queries.vault_stats(session, confirmations)
    if name == "get_wallet_position":
        return queries.wallet_position(
            session, Web3.to_checksum_address(args["address"]), confirmations
        )
    if name == "get_wallet_events":
        return queries.recent_events(
            session,
            confirmations,
            limit=_clamp(args["limit"]),
            wallet=Web3.to_checksum_address(args["address"]),
        )
    if name == "get_top_stakers":
        return queries.top_stakers(session, _clamp(args["limit"]))
    if name == "get_recent_reorgs":
        return queries.recent_reorgs(session, _clamp(args["limit"]))
    raise ValueError(f"unknown tool {name}")


def _tool_result(session: Session, confirmations: int, block) -> dict:
    result = {"type": "tool_result", "tool_use_id": block.id}
    try:
        result["content"] = json.dumps(run_tool(session, confirmations, block.name, block.input))
    except (ValueError, KeyError, TypeError) as exc:
        result.update(content=f"Invalid tool input: {exc}", is_error=True)
    return result


def ask(
    client: anthropic.Anthropic,
    model: str,
    session: Session,
    confirmations: int,
    wallet: str,
    question: str,
) -> Answer:
    answer = Answer(text="")
    messages: list[dict] = [
        {"role": "user", "content": f"The signed-in wallet is {wallet}.\n\n{question}"}
    ]

    for _ in range(MAX_TURNS):
        response = client.beta.messages.create(
            model=model,
            max_tokens=16000,
            betas=[FALLBACK_BETA],
            fallbacks="default",
            output_config={"effort": "medium"},
            system=[
                {"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}
            ],
            tools=TOOLS,
            messages=messages,
        )
        answer.input_tokens += response.usage.input_tokens
        answer.output_tokens += response.usage.output_tokens

        if response.stop_reason == "tool_use":
            calls = [block for block in response.content if block.type == "tool_use"]
            answer.tools_used.extend(call.name for call in calls)
            messages.append({"role": "assistant", "content": response.content})
            messages.append(
                {
                    "role": "user",
                    "content": [_tool_result(session, confirmations, call) for call in calls],
                }
            )
            continue

        if response.stop_reason == "end_turn":
            answer.text = "".join(b.text for b in response.content if b.type == "text").strip()
            return answer
        raise AssistantError(f"the model stopped early: {response.stop_reason}")

    raise AssistantError(f"no answer after {MAX_TURNS} turns")


def calls_in_last_hour(session: Session, wallet: str) -> int:
    return session.scalar(
        select(func.count())
        .select_from(AssistantCall)
        .where(
            AssistantCall.wallet == wallet,
            AssistantCall.created_at > func.now() - func.make_interval(0, 0, 0, 0, 1),
        )
    )


def record_call(session: Session, wallet: str, model: str, answer: Answer) -> None:
    session.add(
        AssistantCall(
            wallet=wallet,
            model=model,
            input_tokens=answer.input_tokens,
            output_tokens=answer.output_tokens,
            tool_calls=len(answer.tools_used),
        )
    )
