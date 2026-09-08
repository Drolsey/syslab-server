"""Tests for the inference plane, with vLLM replaced.

What is under test is the gateway around the model server: does an unknown
token get in, does an alias reach the real model name, does the caller ever
see that real name, and does this plane stay incapable of resolving a tenant.
The model server itself is not under test -- llm.raw_request is replaced.
"""

from __future__ import annotations

import io
import json

import pytest
from fastapi.testclient import TestClient

from app import config, gateway, llm, main

GATEWAY_TOKEN = "a-gateway-token-long-enough"
REAL_MODEL = "Qwen/Qwen3-32B-AWQ"
ALIAS = "syslab-default"


@pytest.fixture(autouse=True)
def gateway_config(monkeypatch):
    monkeypatch.setattr(config, "GATEWAY_TOKENS", {GATEWAY_TOKEN})
    monkeypatch.setattr(gateway, "GATEWAY_TOKENS", {GATEWAY_TOKEN})
    monkeypatch.setattr(gateway, "MODEL_ALIASES", {ALIAS: REAL_MODEL})
    monkeypatch.setattr(gateway, "GATEWAY_RATE_LIMIT_PER_MINUTE", 60)
    gateway._requests.clear()
    yield


@pytest.fixture
def client():
    signed_in = TestClient(main.app)
    signed_in.headers.update({"Authorization": f"Bearer {GATEWAY_TOKEN}"})
    return signed_in


@pytest.fixture
def stranger():
    return TestClient(main.app)


class FakeResponse:
    """Stands in for the urlopen response llm.raw_request returns."""

    def __init__(self, body: str = "", lines: list[str] | None = None):
        self._body = body.encode("utf-8")
        self._lines = [line.encode("utf-8") for line in (lines or [])]

    def read(self):
        return self._body

    def __iter__(self):
        return iter(self._lines)


def completion(model: str = REAL_MODEL) -> str:
    return json.dumps(
        {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "model": model,
            "choices": [
                {"index": 0, "message": {"role": "assistant", "content": "hello"}, "finish_reason": "stop"}
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
        }
    )


def capture_payload(monkeypatch, response: FakeResponse) -> dict:
    """Replace the upstream call and record the payload it was handed."""
    seen: dict = {}

    def fake_raw_request(payload, timeout=None):
        seen.update(payload)
        return response

    monkeypatch.setattr(llm, "raw_request", fake_raw_request)
    return seen


ASK = {"model": ALIAS, "messages": [{"role": "user", "content": "hi"}]}


# --- the door -------------------------------------------------------------

def test_no_token_is_refused(stranger):
    assert stranger.post("/v1/chat/completions", json=ASK).status_code == 401


def test_a_wrong_token_is_refused(stranger):
    stranger.headers.update({"Authorization": "Bearer not-the-right-token"})
    assert stranger.post("/v1/chat/completions", json=ASK).status_code == 401


def test_models_needs_a_token_too(stranger):
    assert stranger.get("/v1/models").status_code == 401


def test_with_no_tokens_configured_the_plane_refuses_everyone(monkeypatch, client):
    monkeypatch.setattr(gateway, "GATEWAY_TOKENS", set())
    response = client.post("/v1/chat/completions", json=ASK)
    assert response.status_code == 503


# --- aliases --------------------------------------------------------------

def test_models_advertises_the_alias_not_the_real_name(client):
    body = client.get("/v1/models").json()
    names = [m["id"] for m in body["data"]]
    assert names == [ALIAS]
    assert REAL_MODEL not in names


def test_the_alias_is_swapped_for_the_real_model_upstream(client, monkeypatch):
    seen = capture_payload(monkeypatch, FakeResponse(completion()))
    client.post("/v1/chat/completions", json=ASK)
    assert seen["model"] == REAL_MODEL


def test_the_caller_is_told_the_alias_it_asked_for(client, monkeypatch):
    capture_payload(monkeypatch, FakeResponse(completion()))
    body = client.post("/v1/chat/completions", json=ASK).json()
    # The whole point of aliasing: swapping the served model must not change
    # anything the website has configured, so it must never see the real name.
    assert body["model"] == ALIAS


def test_an_unknown_model_is_a_404(client, monkeypatch):
    capture_payload(monkeypatch, FakeResponse(completion()))
    response = client.post(
        "/v1/chat/completions", json={**ASK, "model": "some-model-nobody-configured"}
    )
    assert response.status_code == 404


# --- pass-through ---------------------------------------------------------

def test_tools_and_extra_fields_reach_the_model_server(client, monkeypatch):
    seen = capture_payload(monkeypatch, FakeResponse(completion()))
    tools = [{"type": "function", "function": {"name": "get_weather", "parameters": {}}}]
    client.post(
        "/v1/chat/completions",
        json={**ASK, "tools": tools, "tool_choice": "auto", "temperature": 0.2, "max_tokens": 50},
    )
    assert seen["tools"] == tools
    assert seen["tool_choice"] == "auto"
    assert seen["temperature"] == 0.2
    assert seen["max_tokens"] == 50


def test_thinking_is_off_unless_the_caller_asks_for_it(client, monkeypatch):
    seen = capture_payload(monkeypatch, FakeResponse(completion()))
    client.post("/v1/chat/completions", json=ASK)
    assert seen["chat_template_kwargs"] == {"enable_thinking": False}


def test_a_caller_can_turn_thinking_back_on(client, monkeypatch):
    seen = capture_payload(monkeypatch, FakeResponse(completion()))
    client.post(
        "/v1/chat/completions",
        json={**ASK, "chat_template_kwargs": {"enable_thinking": True}},
    )
    assert seen["chat_template_kwargs"] == {"enable_thinking": True}


def test_the_usage_block_survives_the_round_trip(client, monkeypatch):
    capture_payload(monkeypatch, FakeResponse(completion()))
    body = client.post("/v1/chat/completions", json=ASK).json()
    assert body["usage"]["total_tokens"] == 6


def test_a_model_server_that_is_down_is_a_503(client, monkeypatch):
    def refuse(payload, timeout=None):
        raise llm.LlmError("Cannot reach the model server.")

    monkeypatch.setattr(llm, "raw_request", refuse)
    assert client.post("/v1/chat/completions", json=ASK).status_code == 503


# --- streaming ------------------------------------------------------------

def chunk(model: str = REAL_MODEL, content: str = "hi") -> str:
    return "data: " + json.dumps(
        {
            "id": "chatcmpl-test",
            "object": "chat.completion.chunk",
            "model": model,
            "choices": [{"index": 0, "delta": {"content": content}}],
        }
    )


def test_a_streamed_response_relabels_every_chunk(client, monkeypatch):
    capture_payload(
        monkeypatch,
        FakeResponse(lines=[chunk(content="he"), chunk(content="llo"), "data: [DONE]"]),
    )
    response = client.post("/v1/chat/completions", json={**ASK, "stream": True})
    assert response.status_code == 200

    chunks = [
        json.loads(line[len("data:"):].strip())
        for line in response.text.splitlines()
        if line.startswith("data:") and "[DONE]" not in line
    ]
    assert chunks, "no chunks came back"
    assert all(c["model"] == ALIAS for c in chunks)
    assert "".join(c["choices"][0]["delta"]["content"] for c in chunks) == "hello"


def test_a_streamed_response_terminates_with_done(client, monkeypatch):
    capture_payload(monkeypatch, FakeResponse(lines=[chunk(), "data: [DONE]"]))
    response = client.post("/v1/chat/completions", json={**ASK, "stream": True})
    # An OpenAI client reads until this sentinel; without it, it waits forever.
    assert response.text.rstrip().endswith("data: [DONE]")


def test_streaming_asks_for_usage(client, monkeypatch):
    seen = capture_payload(monkeypatch, FakeResponse(lines=["data: [DONE]"]))
    client.post("/v1/chat/completions", json={**ASK, "stream": True})
    assert seen["stream_options"] == {"include_usage": True}


# --- rate limiting --------------------------------------------------------

def test_a_token_over_its_limit_is_refused(client, monkeypatch):
    monkeypatch.setattr(gateway, "GATEWAY_RATE_LIMIT_PER_MINUTE", 2)
    capture_payload(monkeypatch, FakeResponse(completion()))
    assert client.post("/v1/chat/completions", json=ASK).status_code == 200
    assert client.post("/v1/chat/completions", json=ASK).status_code == 200
    assert client.post("/v1/chat/completions", json=ASK).status_code == 429


# --- the boundary ---------------------------------------------------------

def test_the_inference_plane_sets_no_tenant(client, monkeypatch):
    """The plane's whole safety argument: no tenant is ever resolved here, so
    there is no tenant storage to open. scripts/check_gateway_isolation.py
    asserts the same thing against the source; this asserts it at runtime."""
    from app import context

    capture_payload(monkeypatch, FakeResponse(completion()))
    client.post("/v1/chat/completions", json=ASK)
    with pytest.raises(context.NoTenantError):
        context.current_tenant()
