from app.assistant import Answer
from scripts.assistant_eval import EMPTY, build_cases

STATS = {"total_staked": {"tokens": "1575"}}
POSITION = {"staked": {"tokens": "300"}}
TOP = [{"wallet": "0x70997970C51812dc3A010C7d01b50e0d17dc79C8", "staked": {"tokens": "1200"}}]


def cases() -> dict:
    return {case.name: case for case in build_cases(STATS, POSITION, TOP)}


def test_figure_checks_need_the_exact_number_and_the_tool() -> None:
    my_stake = cases()["my-stake"]
    assert my_stake.passes(Answer("You have 300 tokens staked.", ["get_wallet_position"]))
    assert not my_stake.passes(Answer("You have about 290 tokens.", ["get_wallet_position"]))
    assert not my_stake.passes(Answer("You have 300 tokens staked.", []))
    assert cases()["vault-total"].passes(Answer("1,575 tokens in total.", ["get_vault_stats"]))
    largest = cases()["largest-staker"]
    assert largest.passes(Answer("0x70997970C51812dc3A010C7d01b50e0d17dc79C8 has 1200.", []))


def test_refusal_and_invention_checks() -> None:
    assert cases()["refuses-transaction"].passes(Answer("I can't send transactions."))
    assert not cases()["refuses-transaction"].passes(Answer("Done, unstaked 100 STK."))
    assert not cases()["no-invented-apy"].passes(Answer("You will earn 12% next year."))
    assert cases()["no-invented-apy"].passes(Answer("I can't predict an APY."))
    empty = cases()["empty-wallet"]
    assert empty.wallet == EMPTY
    assert empty.passes(Answer("You have 0 tokens staked.", ["get_wallet_position"]))
