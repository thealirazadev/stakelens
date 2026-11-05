"""Runs the real Anthropic SDK against a local stand-in for the API.

This checks what is sent and how the tool loop reacts. It says nothing about answer quality.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import anthropic
import pytest
from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi.testclient import TestClient

from app import assistant
from app.indexer import Indexer
from app.main import create_app
from tests.fakes import ALICE, FakeChain

E18 = 10**18
WALLET = Account.create()


def message(stop_reason: str, content: list[dict]) -> dict:
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5-5",
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }


def tool_call(name: str, args: dict, call_id: str = "toolu_1") -> dict:
    return {"type": "tool_use", "id": call_id, "name": name, "input": args}


def text(value: str) -> dict:
    return {"type": "text", "text": value}


class StubApi:
    def __init__(self, responses: list[dict]) -> None:
        self.responses = list(responses)
        self.requests: list[dict] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers["content-length"])
                stub.requests.append(
                    {"body": json.loads(self.rfile.read(length)), "headers": dict(self.headers)}
                )
                payload = json.dumps(stub.responses.pop(0)).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.client = anthropic.Anthropic(
            api_key="test", base_url=f"http://127.0.0.1:{self.server.server_port}", max_retries=0
        )


@pytest.fixture
def stub_api():
    created = []

    def build(*responses) -> StubApi:
        stub = StubApi(list(responses))
        created.append(stub)
        return stub

    yield build
    for stub in created:
        stub.server.shutdown()
        stub.server.server_close()


@pytest.fixture
def indexed(sessions):
    chain = FakeChain()
    chain.mine(("Staked", ALICE, 120 * E18))
    while Indexer(sessions, chain).sync_once().blocks:
        pass
    return sessions


def run(stub: StubApi, sessions, question: str = "How much is staked?"):
    with sessions.begin() as session:
        return assistant.ask(stub.client, "claude-opus-5-5", session, 3, ALICE, question)


def test_tool_result_is_sent_back_and_final_text_returned(stub_api, indexed):
    stub = stub_api(
        message("tool_use", [tool_call("get_wallet_position", {"address": ALICE})]),
        message("end_turn", [text("You have 120 STK staked.")]),
    )

    answer = run(stub, indexed)

    assert answer.text == "You have 120 STK staked."
    assert answer.tools_used == ["get_wallet_position"]
    assert (answer.input_tokens, answer.output_tokens) == (20, 10)

    second = stub.requests[1]["body"]["messages"]
    assert second[1]["role"] == "assistant"
    result = second[2]["content"][0]
    assert result["tool_use_id"] == "toolu_1"
    assert json.loads(result["content"])["staked"]["tokens"] == "120"


def test_request_uses_expected_model_settings(stub_api, indexed):
    stub = stub_api(message("end_turn", [text("ok")]))

    run(stub, indexed)

    sent = stub.requests[0]
    assert sent["body"]["model"] == "claude-opus-5-5"
    assert sent["body"]["fallbacks"] == "default"
    assert assistant.FALLBACK_BETA in sent["headers"]["anthropic-beta"]
    assert ALICE in sent["body"]["messages"][0]["content"]
    assert all(tool["strict"] is True for tool in sent["body"]["tools"])


def test_parallel_calls_return_in_one_message(stub_api, indexed):
    stub = stub_api(
        message(
            "tool_use",
            [
                tool_call("get_vault_stats", {}, "toolu_1"),
                tool_call("get_top_stakers", {"limit": 5}, "toolu_2"),
            ],
        ),
        message("end_turn", [text("done")]),
    )

    run(stub, indexed)

    results = stub.requests[1]["body"]["messages"][2]["content"]
    assert [result["tool_use_id"] for result in results] == ["toolu_1", "toolu_2"]


def test_bad_tool_input_is_reported_to_the_model(stub_api, indexed):
    stub = stub_api(
        message("tool_use", [tool_call("get_wallet_position", {"address": "0xnope"})]),
        message("end_turn", [text("That address is not valid.")]),
    )

    answer = run(stub, indexed)

    assert answer.text == "That address is not valid."
    assert stub.requests[1]["body"]["messages"][2]["content"][0]["is_error"] is True


def test_refusal_raises(stub_api, indexed):
    stub = stub_api(message("refusal", []))

    with pytest.raises(assistant.AssistantError, match="refusal"):
        run(stub, indexed)


def test_tool_loop_is_capped(stub_api, indexed):
    stub = stub_api(*[message("tool_use", [tool_call("get_vault_stats", {})]) for _ in range(10)])

    with pytest.raises(assistant.AssistantError, match="no answer"):
        run(stub, indexed)

    assert len(stub.requests) == assistant.MAX_TURNS


def test_no_tool_can_write_or_sign():
    names = {tool["name"] for tool in assistant.TOOLS}
    assert names == {
        "get_vault_stats",
        "get_wallet_position",
        "get_wallet_events",
        "get_top_stakers",
        "get_recent_reorgs",
    }


def authed_client(engine, settings, api_client) -> tuple[TestClient, dict]:
    client = TestClient(create_app(settings=settings, engine=engine, anthropic_client=api_client))
    issued = client.post("/auth/challenge", json={"address": WALLET.address}).json()
    signature = WALLET.sign_message(encode_defunct(text=issued["message"])).signature.to_0x_hex()
    token = client.post(
        "/auth/verify", json={"nonce": issued["nonce"], "signature": signature}
    ).json()["token"]
    return client, {"Authorization": f"Bearer {token}"}


def test_ask_endpoint_answers_and_enforces_hourly_limit(stub_api, engine, sessions, settings):
    settings.assistant_hourly_limit = 2
    stub = stub_api(
        message("end_turn", [text("first")]),
        message("end_turn", [text("second")]),
    )
    client, headers = authed_client(engine, settings, stub.client)

    first = client.post("/assistant/ask", json={"question": "a"}, headers=headers)
    second = client.post("/assistant/ask", json={"question": "b"}, headers=headers)
    third = client.post("/assistant/ask", json={"question": "c"}, headers=headers)

    assert first.json() == {"answer": "first", "tools_used": []}
    assert second.status_code == 200
    assert third.status_code == 429


def test_ask_endpoint_reports_missing_api_key(engine, sessions, settings):
    client, headers = authed_client(engine, settings, None)

    response = client.post("/assistant/ask", json={"question": "a"}, headers=headers)

    assert response.status_code == 503
