"""Provider transports for Xiaomi MiMo driver generation."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

DEFAULT_MODEL = "mimo-v2.6-pro"
PAYG_BASE_URL = "https://api.xiaomimimo.com/v1"
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class MiMoError(RuntimeError):
    """A failed provider request. Errors omit credentials and response bodies."""


class PendingCompletion(MiMoError):
    """An interactive request is ready for an external coding tool."""

    def __init__(self, request_path: Path, reply_path: Path) -> None:
        self.request_path = request_path
        self.reply_path = reply_path
        super().__init__(f"Interactive completion pending. Read {request_path} and write {reply_path}.")


@dataclass(frozen=True)
class Completion:
    content: str
    usage: dict = field(default_factory=dict)
    finish_reason: str = "stop"


def _read_payg_env(path: Path | str | None) -> dict[str, str]:
    if path is None:
        return {}
    values = {}
    for raw in Path(path).expanduser().read_text(encoding="utf-8-sig").splitlines():
        key, separator, value = raw.strip().removeprefix("export ").partition("=")
        key = key.strip()
        if separator and key in {"MIMO_PAYG_API_KEY", "MIMO_PAYG_BASE_URL"}:
            value = value.strip()
            if len(value) >= 2 and value[0] in ('"', "'") and value[0] == value[-1]:
                value = value[1:-1]
            values[key] = value
    return values


def _body(model: str, messages: list[dict], max_tokens: int, thinking: bool, json_mode: bool) -> dict:
    if not 1 <= max_tokens <= 131072:
        raise ValueError("MiMo completion budget must be between 1 and 131072 tokens.")
    result = {
        "model": model,
        "messages": messages,
        "max_completion_tokens": max_tokens,
        "thinking": {"type": "enabled" if thinking else "disabled"},
    }
    if json_mode:
        result["response_format"] = {"type": "json_object"}
    return result


def _canonical_json(value: dict) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _write_request(path: Path, record: dict) -> None:
    # Atomic publication also keeps the prompt readable only by the invoking user.
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as temp:
        staged = Path(temp.name)
        try:
            json.dump(record, temp, ensure_ascii=False, indent=2)
            temp.write("\n")
            temp.close()
            staged.replace(path)
        finally:
            staged.unlink(missing_ok=True)


class FileExchangeClient:
    """Stage deterministic requests for MiMo running inside a coding tool."""

    def __init__(self, directory: Path | str, *, model: str = DEFAULT_MODEL) -> None:
        self.directory = Path(directory)
        self.model = model

    def complete(
        self,
        messages: list[dict],
        *,
        max_tokens: int = 16384,
        thinking: bool = True,
        json_mode: bool = True,
    ) -> Completion:
        request = _body(self.model, messages, max_tokens, thinking, json_mode)
        request_id = hashlib.sha256(_canonical_json(request)).hexdigest()
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        request_path = self.directory / f"{request_id}.request.json"
        reply_path = self.directory / f"{request_id}.reply.json"
        if not request_path.exists():
            _write_request(request_path, {
                "request_id": request_id,
                **request,
                "reply_path": reply_path.name,
                "instructions": (
                    "Apply the ordered messages using MiMo in your coding tool. "
                    "Write the completion itself to reply_path as text, with no API envelope."
                ),
            })
        if not reply_path.is_file():
            raise PendingCompletion(request_path, reply_path)
        content = reply_path.read_text(encoding="utf-8-sig")
        if not content.strip():
            raise MiMoError(f"The interactive reply is empty at {reply_path}.")
        return Completion(content)


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _stream_events(response):
    data = []
    for line in response:
        line = line.rstrip(b"\r\n")
        if not line:
            if data:
                yield b"\n".join(data)
                data.clear()
        elif line.startswith(b"data:"):
            data.append(line[5:].lstrip(b" "))
    if data:
        yield b"\n".join(data)


def _read_response(response) -> Completion:
    if "text/event-stream" not in response.headers.get("Content-Type", ""):
        try:
            data = json.load(response)
            choice = data["choices"][0]
            content = choice["message"]["content"]
            reason = choice["finish_reason"]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise MiMoError("MiMo returned an invalid chat completion.") from exc
        if not isinstance(content, str) or not isinstance(reason, str):
            raise MiMoError("MiMo returned an invalid message or finish reason.")
        return Completion(content, data.get("usage") or {}, reason)

    parts = []
    usage = {}
    reason = None
    try:
        for event in _stream_events(response):
            if event == b"[DONE]":
                break
            data = json.loads(event)
            if "error" in data:
                raise MiMoError("MiMo reported a streaming error.")
            if isinstance(data.get("usage"), dict):
                usage = data["usage"]
            for choice in data.get("choices") or []:
                if choice.get("index", 0) != 0:
                    continue
                delta = choice.get("delta") or {}
                if isinstance(delta.get("content"), str):
                    parts.append(delta["content"])
                if choice.get("finish_reason"):
                    reason = choice["finish_reason"]
    except (AttributeError, TypeError, ValueError) as exc:
        raise MiMoError("MiMo returned an invalid chat stream.") from exc
    if not isinstance(reason, str):
        raise MiMoError("MiMo closed the stream without a finish reason.")
    return Completion("".join(parts), usage, reason)


def _retry_delay(error: HTTPError, attempt: int) -> float:
    value = error.headers.get("Retry-After") if error.headers else None
    try:
        return min(120, max(0, float(value)))
    except (TypeError, ValueError):
        if value:
            try:
                return min(120, max(0, parsedate_to_datetime(value).timestamp() - time.time()))
            except (TypeError, ValueError, OverflowError, OSError):
                pass
        return min(30, 2 ** attempt)


class MiMoClient:
    """Call the official MiMo pay-as-you-go endpoint with scoped credentials."""

    def __init__(
        self,
        api_key: str,
        *,
        model: str = DEFAULT_MODEL,
        base_url: str = PAYG_BASE_URL,
        timeout: float = 180,
        retries: int = 3,
    ) -> None:
        if not re.fullmatch(r"sk-[A-Za-z0-9_-]+", api_key):
            raise ValueError("Automated generation requires a MiMo pay-as-you-go key (MIMO_PAYG_API_KEY).")
        if base_url.rstrip("/") != PAYG_BASE_URL:
            raise ValueError(f"The live client uses the official pay-as-you-go endpoint {PAYG_BASE_URL}.")
        if timeout <= 0 or retries < 0:
            raise ValueError("MiMo timeout must be positive and retries must be nonnegative.")
        self._api_key = api_key
        self._opener = build_opener(_NoRedirects())
        self.model = model
        self.timeout = timeout
        self.retries = retries

    @classmethod
    def from_env(
        cls,
        *,
        env_file: Path | str | None = None,
        model: str = DEFAULT_MODEL,
        timeout: float = 180,
        retries: int = 3,
    ) -> MiMoClient:
        values = _read_payg_env(env_file)
        key = os.environ.get("MIMO_PAYG_API_KEY") or values.get("MIMO_PAYG_API_KEY") or ""
        base = os.environ.get("MIMO_PAYG_BASE_URL") or values.get("MIMO_PAYG_BASE_URL") or PAYG_BASE_URL
        return cls(key, model=model, base_url=base, timeout=timeout, retries=retries)

    def complete(
        self,
        messages: list[dict],
        *,
        max_tokens: int = 16384,
        thinking: bool = True,
        json_mode: bool = True,
    ) -> Completion:
        payload = _body(self.model, messages, max_tokens, thinking, json_mode)
        payload["stream"] = True
        payload["stream_options"] = {"include_usage": True}
        request = Request(
            f"{PAYG_BASE_URL}/chat/completions",
            data=_canonical_json(payload),
            headers={
                "api-key": self._api_key,
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            },
            method="POST",
        )
        for attempt in range(self.retries + 1):
            try:
                with self._opener.open(request, timeout=self.timeout) as response:
                    return _read_response(response)
            except HTTPError as exc:
                try:
                    if exc.code not in RETRYABLE_STATUS or attempt == self.retries:
                        raise MiMoError(f"MiMo pay-as-you-go API rejected the request (HTTP {exc.code}).") from None
                    delay = _retry_delay(exc, attempt)
                finally:
                    exc.close()
                time.sleep(delay)
            except (HTTPException, URLError, OSError) as exc:
                # A dropped stream may already have consumed completion tokens.
                raise MiMoError(f"MiMo connection failed ({type(exc).__name__}).") from None
        raise AssertionError("The retry loop must return or raise.")
