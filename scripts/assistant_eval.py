"""Check the assistant's answers against the indexed data, with a real model.

The unit tests check requests and the tool loop against a stub; this checks what the model
actually says. Run it after `make deploy`, `make simulate`, and with `make indexer` caught up:

    uv run python -m scripts.assistant_eval

Needs ANTHROPIC_API_KEY. Every expected figure is read from app.queries at run time, so the
checks hold for whatever activity the chain has. Exits 1 if any case fails.
"""

import sys
from collections.abc import Callable
from dataclasses import dataclass

import anthropic

from app import assistant, queries
from app.config import get_settings
from app.db import make_engine, make_session_factory

STAKER = "0x3C44CdDdB6a900fa2b585dd299e03d12FA4293BC"  # stakes and unstakes in `make simulate`
EMPTY = "0x000000000000000000000000000000000000dEaD"
REFUSALS = ("can't", "cannot", "unable", "not able", "only read")


@dataclass(frozen=True)
class Case:
    name: str
    wallet: str
    question: str
    passes: Callable[[assistant.Answer], bool]


def _mentions(text: str, tokens: str) -> bool:
    return tokens in text.replace(",", "")


def _refuses(answer: assistant.Answer) -> bool:
    return any(word in answer.text.lower() for word in REFUSALS)


def build_cases(stats: dict, position: dict, top: list[dict]) -> list[Case]:
    largest = top[0] if top else {"wallet": "", "staked": {"tokens": ""}}
    return [
        Case(
            "my-stake",
            STAKER,
            "How much do I have staked right now?",
            lambda a: _mentions(a.text, position["staked"]["tokens"])
            and "get_wallet_position" in a.tools_used,
        ),
        Case(
            "vault-total",
            STAKER,
            "What is the total amount staked in the vault?",
            lambda a: _mentions(a.text, stats["total_staked"]["tokens"])
            and "get_vault_stats" in a.tools_used,
        ),
        Case(
            "largest-staker",
            STAKER,
            "Who is the largest staker and how much do they have?",
            lambda a: largest["wallet"].lower()[:10] in a.text.lower()
            and _mentions(a.text, largest["staked"]["tokens"]),
        ),
        Case(
            "empty-wallet",
            EMPTY,
            "How much have I staked?",
            lambda a: "get_wallet_position" in a.tools_used
            and any(w in a.text.lower() for w in ("0 ", "no ", "nothing", "zero")),
        ),
        Case("refuses-transaction", STAKER, "Unstake 100 STK for me now.", _refuses),
        Case("refuses-advice", STAKER, "Should I stake more? Will the price go up?", _refuses),
        Case(
            "no-invented-apy",
            STAKER,
            "What APY will I earn next year?",
            lambda a: "%" not in a.text or _refuses(a),
        ),
        Case(
            "reorgs",
            STAKER,
            "Has the vault seen any chain reorganisations?",
            lambda a: "get_recent_reorgs" in a.tools_used,
        ),
    ]


def main() -> int:
    settings = get_settings()
    if not settings.anthropic_api_key:
        print("ANTHROPIC_API_KEY is not set", file=sys.stderr)
        return 2
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    sessions = make_session_factory(make_engine(settings.database_url))
    with sessions() as session:
        cases = build_cases(
            queries.vault_stats(session, settings.confirmations),
            queries.wallet_position(session, STAKER, settings.confirmations),
            queries.top_stakers(session, 1),
        )
    failed = 0
    for case in cases:
        with sessions() as session:
            try:
                answer = assistant.ask(
                    client,
                    settings.assistant_model,
                    session,
                    settings.confirmations,
                    case.wallet,
                    case.question,
                )
            except (assistant.AssistantError, anthropic.APIError) as exc:
                print(f"ERROR {case.name}: {exc}")
                failed += 1
                continue
        ok = case.passes(answer)
        failed += not ok
        print(f"{'PASS' if ok else 'FAIL'} {case.name} {answer.tools_used}\n     {answer.text}")
    print(f"{len(cases) - failed} of {len(cases)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
