from io import BytesIO
import json
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from sheetforge_click.driver_mimo import (
    FileExchangeClient,
    MiMoClient,
    MiMoError,
    PendingCompletion,
)

MESSAGES = [{"role": "user", "content": "Respond with a JSON object."}]


def test_file_exchange_is_deterministic_and_resumable(tmp_path):
    client = FileExchangeClient(tmp_path)
    with pytest.raises(PendingCompletion) as raised:
        client.complete(MESSAGES, max_tokens=20)
    pending = raised.value
    request = json.loads(pending.request_path.read_text())
    assert request["messages"] == MESSAGES
    assert request["model"] == "mimo-v2.6-pro"
    assert request["response_format"] == {"type": "json_object"}
    assert request["max_completion_tokens"] == 20
    assert request["reply_path"] == pending.reply_path.name
    assert pending.request_path.stat().st_mode & 0o077 == 0

    pending.reply_path.write_text('{"status":"ok"}', encoding="utf-8")
    assert client.complete(MESSAGES, max_tokens=20).content == '{"status":"ok"}'
    with pytest.raises(PendingCompletion) as changed:
        client.complete(MESSAGES, max_tokens=21)
    assert changed.value.request_path != pending.request_path


def test_payg_rejects_token_plan_and_nonstandard_host():
    with pytest.raises(ValueError, match="pay-as-you-go key"):
        MiMoClient("tp-units-test")
    with pytest.raises(ValueError, match="pay-as-you-go endpoint"):
        MiMoClient("sk-unit-test", base_url="https://token-plan-ams.xiaomimimo.com/v1")


def test_env_load_uses_payg_only(tmp_path, monkeypatch):
    monkeypatch.delenv("MIMO_PAYG_API_KEY", raising=False)
    monkeypatch.delenv("MIMO_PAYG_BASE_URL", raising=False)
    path = tmp_path / "test.env"
    path.write_text("MIMO_API_KEY=tp-unit-test\n")
    with pytest.raises(ValueError, match="pay-as-you-go key"):
        MiMoClient.from_env(env_file=path)

    path.write_text("export MIMO_PAYG_API_KEY='sk-test'\n")
    assert MiMoClient.from_env(env_file=path).model == "mimo-v2.6-pro"


def test_streaming_request_joins_content_and_records_usage(monkeypatch):
    client = MiMoClient("sk-unit-test")
    frames = [
        {"choices": [{"index": 0, "delta": {"reasoning_content": "hidden"}}]},
        {"choices": [{"index": 0, "delta": {"content": '{"value":'}}]},
        {"choices": [{"index": 0, "delta": {"content": "42}"}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"completion_tokens": 3}},
    ]
    data = b"".join(b"data: " + json.dumps(frame).encode() + b"\r\n\r\n" for frame in frames)
    data += b"data: [DONE]\r\n\r\n"

    def fake_open(request, timeout):
        assert request.full_url == "https://api.xiaomimimo.com/v1/chat/completions"
        assert request.get_header("Api-key") == "sk-unit-test"
        assert timeout == 180
        payload = json.loads(request.data)
        assert payload["stream"] and payload["stream_options"]["include_usage"]
        assert payload["response_format"] == {"type": "json_object"}
        result = BytesIO(data)
        result.headers = {"Content-Type": "text/event-stream; charset=utf-8"}
        return result

    monkeypatch.setattr(client, "_opener", SimpleNamespace(open=fake_open))
    completion = client.complete(MESSAGES)
    assert completion.content == '{"value":42}'
    assert completion.usage == {"completion_tokens": 3}
    assert completion.finish_reason == "stop"


def test_retries_rate_limit_and_hides_auth_error(monkeypatch):
    client = MiMoClient("sk-unit-test", retries=2)
    calls = []

    def fake_open(request, timeout):
        calls.append(request.full_url)
        if len(calls) == 1:
            raise HTTPError(request.full_url, 429, "rate limit", {"Retry-After": "0"}, BytesIO(b""))
        raise HTTPError(request.full_url, 401, "sensitive", {}, BytesIO(b"internal auth diagnostics"))

    monkeypatch.setattr(client, "_opener", SimpleNamespace(open=fake_open))
    with pytest.raises(MiMoError, match=r"HTTP 401") as rejected:
        client.complete(MESSAGES)
    assert len(calls) == 2
    assert "internal auth diagnostics" not in str(rejected.value)
    assert "sk-unit-test" not in str(rejected.value)


def test_interrupted_stream_cannot_return_a_partial_completion(monkeypatch):
    client = MiMoClient("sk-unit-test")

    def fake_open(request, timeout):
        data = b'data: {"choices":[{"delta":{"content":"incomplete"}}]}\n\n'
        result = BytesIO(data)
        result.headers = {"Content-Type": "text/event-stream"}
        return result

    monkeypatch.setattr(client, "_opener", SimpleNamespace(open=fake_open))
    with pytest.raises(MiMoError, match="closed the stream"):
        client.complete(MESSAGES)
