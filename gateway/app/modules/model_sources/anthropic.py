"""Native Anthropic transport: no OpenAI conversion and no ambiguous retries."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import AsyncIterator, Mapping
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

import aiohttp

from app.core.clients.http import lease_model_source_session
from app.core.clock import REAL_SCHEDULER
from app.core.types import JsonValue
from app.core.utils.shared_future import _await_cleanup_deferring_cancellation
from app.db.models import ModelSource
from app.modules.model_sources.api_usage import record_api_rate_headers
from app.modules.model_sources.forwarding import (
    SourceResponsesStream,
    SourceStreamTransport,
    SourceUsage,
    SourceUsageHolder,
    _source_client_timeout,
    source_stream_idle_seconds,
)
from app.modules.model_sources.providers.base import ProviderError
from app.modules.model_sources.selection import source_capacity_group
from app.modules.model_sources.subscription_runtime import upstream_headers

_SAFE_HEADERS = frozenset({"content-type", "retry-after", "request-id", "x-request-id", "x-should-retry"})
NATIVE_JSON_MAX_BYTES = 32 * 1024 * 1024
NATIVE_ERROR_MAX_BYTES = 1024 * 1024
_FRAME_BOUNDARY = re.compile(rb"\r\n\r\n|\n\n|\r\r")


@dataclass(frozen=True, slots=True)
class NativeResponse:
    body: bytes
    status: int
    headers: dict[str, str]
    usage: SourceUsage | None = None
    continuation_tokens: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class NativeStream(SourceResponsesStream):
    headers: dict[str, str] = field(default_factory=dict, kw_only=True)


@dataclass(frozen=True, slots=True)
class NativeQuotaObservation:
    window_key: str
    model: str | None
    used_ratio: float
    resets_at: datetime


def native_quota_observations(headers: Mapping[str, str], *, model: str) -> list[NativeQuotaObservation]:
    """Read native 0..1 utilization/epoch-second pairs, never invent missing quota."""
    normalized = {name.lower(): value for name, value in headers.items()}
    observations: list[NativeQuotaObservation] = []
    for prefix, key, scoped in (
        ("5h", "five_hour", False),
        ("7d", "seven_day", False),
        ("7d_oi", "header:seven_day_oi:" + hashlib.sha256(model.encode()).hexdigest()[:16], True),
    ):
        base = "anthropic-ratelimit-unified-" + prefix
        try:
            ratio = float(normalized[base + "-utilization"])
            reset = float(normalized[base + "-reset"])
            if not math.isfinite(ratio) or ratio < 0 or not math.isfinite(reset) or reset <= 0:
                continue
            resets_at = datetime.fromtimestamp(reset, UTC).replace(tzinfo=None)
        except (KeyError, ValueError, OverflowError, OSError):
            continue
        if normalized.get(base + "-status") == "rejected":
            ratio = max(ratio, 1.0)
        observations.append(NativeQuotaObservation(key, model if scoped else None, min(ratio, 1.0), resets_at))
    return observations


class NativeStreamIncomplete(aiohttp.ClientPayloadError):
    def __init__(self, *, partial_frame: bool) -> None:
        super().__init__("Native upstream stream ended without a complete terminal event")
        self.partial_frame = partial_frame


def validate_native_completion(payload: Mapping[str, JsonValue], *, count_tokens: bool) -> None:
    if count_tokens:
        count = payload.get("input_tokens")
        valid = isinstance(count, int) and not isinstance(count, bool) and count >= 0
    else:
        valid = (
            payload.get("type") == "message"
            and payload.get("role") == "assistant"
            and isinstance(payload.get("id"), str)
            and bool(payload["id"])
            and isinstance(payload.get("model"), str)
            and bool(payload["model"])
            and isinstance(payload.get("content"), list)
            and isinstance(payload.get("stop_reason"), str)
            and bool(payload["stop_reason"])
        )
    if not valid:
        raise aiohttp.ClientPayloadError("Native upstream did not return a completed native response")


class NativeRejected(Exception):
    def __init__(self, response: NativeResponse) -> None:
        super().__init__(f"Native upstream status {response.status}")
        self.response = response
        # Only explicit rejection is evidence of non-acceptance. Server errors,
        # timeouts and disconnects are never a license to duplicate inference.
        self.unaccepted = response.status in {401, 403, 404, 429}


def native_usage(payload: Mapping[str, JsonValue], served_model: str | None = None) -> SourceUsage | None:
    value = payload.get("usage")
    if not isinstance(value, dict):
        return None
    for token_field in ("input_tokens", "output_tokens"):
        number = value.get(token_field)
        if not isinstance(number, int) or isinstance(number, bool) or number < 0:
            return None
    for token_field in ("cache_read_input_tokens", "cache_creation_input_tokens"):
        number = value.get(token_field, 0)
        if not isinstance(number, int) or isinstance(number, bool) or number < 0:
            return None

    def count(key: str, data: Mapping[str, JsonValue] = value) -> int:
        number = data.get(key)
        return number if isinstance(number, int) and not isinstance(number, bool) and number >= 0 else 0

    creation = value.get("cache_creation")
    ttl = creation if isinstance(creation, dict) else {}
    for token_field in ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens"):
        number = ttl.get(token_field, 0)
        if not isinstance(number, int) or isinstance(number, bool) or number < 0:
            return None
    five = count("ephemeral_5m_input_tokens", ttl)
    hour = count("ephemeral_1h_input_tokens", ttl)
    created = count("cache_creation_input_tokens") if "cache_creation_input_tokens" in value else five + hour
    read = count("cache_read_input_tokens")
    model = payload.get("model")
    return SourceUsage(
        input_tokens=count("input_tokens") + read + created,
        output_tokens=count("output_tokens"),
        cached_input_tokens=read,
        cache_creation_input_tokens=created,
        cache_creation_5m_input_tokens=five,
        cache_creation_1h_input_tokens=hour,
        served_model=model if isinstance(model, str) else served_model,
    )


def native_source_principal(source: ModelSource) -> str:
    identity = source_capacity_group(source)
    if source.auth_type != "claude_oauth":
        credential_identity = (
            hashlib.sha256(source.api_key_encrypted or b"").hexdigest() if source.auth_type == "api_key" else None
        )
        identity = json.dumps([source.provider, source.auth_type, identity, credential_identity], separators=(",", ":"))
    # Message-root changes invalidate opaque continuation ownership even when
    # the OpenAI Chat root and credential remain unchanged.
    return hashlib.sha256(
        json.dumps([identity, source.base_url, source.messages_base_url], separators=(",", ":")).encode()
    ).hexdigest()


def continuation_token(kind: str, value: str) -> str:
    return hashlib.sha256((kind + ":" + value).encode()).hexdigest()


def native_continuation_tokens(payload: Mapping[str, JsonValue]) -> set[str]:
    tokens: set[str] = set()
    container = payload.get("container")
    container_id = container.get("id") if isinstance(container, dict) else container
    if isinstance(container_id, str) and container_id:
        tokens.add(continuation_token("container", container_id))
    content = payload.get("content")
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            signature = block.get("signature")
            if isinstance(signature, str) and signature:
                tokens.add(continuation_token("signature", signature))
            if block.get("type") == "redacted_thinking" and isinstance(block.get("data"), str):
                tokens.add(continuation_token("redacted_thinking", block["data"]))
            tool_id = block.get("tool_use_id")
            if block.get("type") in {"tool_use", "server_tool_use", "mcp_tool_use"}:
                tool_id = block.get("id")
            if isinstance(tool_id, str) and tool_id:
                tokens.add(continuation_token("tool", tool_id))
    return tokens


class NativeUsageParser:
    """Observe bounded SSE records while relaying their original bytes unchanged."""

    def __init__(self, holder: SourceUsageHolder) -> None:
        self.holder = holder
        self.buffer = b""
        self.discarding = False
        self.usage: dict[str, JsonValue] = {}
        self.model: str | None = None
        self.signatures: dict[int, bytearray] = {}

    def feed(self, chunk: bytes) -> None:
        self.buffer += chunk
        while match := _FRAME_BOUNDARY.search(self.buffer):
            frame, self.buffer = self.buffer[: match.start()], self.buffer[match.end() :]
            if not self.discarding:
                self._frame(frame)
            self.discarding = False
        if len(self.buffer) > 1_048_576:
            # Drop observations, not traffic; a later terminal still parses.
            self.buffer = self.buffer[-3:]
            self.discarding = True

    def finish(self) -> None:
        # SSE dispatch requires a blank line: a partial terminal at EOF is
        # not evidence that the client received message_stop.
        self.buffer = b""

    def _frame(self, frame: bytes) -> None:
        lines = re.split(rb"\r\n|\r|\n", frame)
        data = b"\n".join(line[5:].lstrip(b" ") for line in lines if line.startswith(b"data:"))
        try:
            payload = json.loads(data)
        except (ValueError, UnicodeError):
            return
        if not isinstance(payload, dict):
            return
        kind = payload.get("type")
        if kind == "message_start":
            message = payload.get("message")
            if isinstance(message, dict):
                self.model = message.get("model") if isinstance(message.get("model"), str) else None
                self.holder.response_id = message.get("id") if isinstance(message.get("id"), str) else None
                if isinstance(message.get("usage"), dict):
                    # message_start output is provisional (normally zero), not
                    # a measurement of the final generated output. Only a
                    # message_delta usage observation can complete usage.
                    self.usage.update({key: value for key, value in message["usage"].items() if key != "output_tokens"})
                self.holder.continuation_tokens.update(native_continuation_tokens(message))
        elif kind == "message_delta":
            if isinstance(payload.get("usage"), dict):
                self.usage.update(payload["usage"])
            self.holder.continuation_tokens.update(native_continuation_tokens(payload))
            delta = payload.get("delta")
            if isinstance(delta, dict):
                self.holder.continuation_tokens.update(native_continuation_tokens(delta))
        elif kind == "message_stop":
            self.holder.terminal_kind = "completed"
        elif kind == "error":
            self.holder.terminal_kind = "error"
        index = payload.get("index")
        if isinstance(index, int) and kind == "content_block_start":
            block = payload.get("content_block")
            if isinstance(block, dict):
                self.holder.continuation_tokens.update(native_continuation_tokens({"content": [block]}))
                if block.get("type") == "thinking":
                    if len(self.signatures) >= 64:
                        raise aiohttp.ClientPayloadError("Too many concurrent native thinking blocks")
                    self.signatures[index] = bytearray()
        if isinstance(index, int) and kind == "content_block_delta":
            delta = payload.get("delta")
            if isinstance(delta, dict) and delta.get("type") == "signature_delta":
                signature = delta.get("signature")
                if isinstance(signature, str) and index in self.signatures:
                    data = self.signatures[index]
                    if len(data) + len(signature.encode()) > 1_048_576:
                        del self.signatures[index]
                    else:
                        data.extend(signature.encode())
        if isinstance(index, int) and kind == "content_block_stop":
            signature = self.signatures.pop(index, None)
            if signature:
                self.holder.continuation_tokens.add(continuation_token("signature", signature.decode()))
        if kind in {"content_block_start", "content_block_delta"}:
            self.holder.first_content_seen = True
            self.holder.delta_chars += len(data)
        if self.usage and kind in {"message_delta", "message_stop"}:
            self.holder.usage = native_usage({"usage": self.usage}, self.model)


def _safe_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {
        key.lower(): value
        for key, value in headers.items()
        if (key.lower() in _SAFE_HEADERS or key.lower().startswith("anthropic-ratelimit-"))
        and "\r" not in value
        and "\n" not in value
    }


def _redact(body: bytes, secrets: list[str]) -> bytes:
    for secret in secrets:
        if secret:
            body = body.replace(secret.encode(), b"[redacted]")
    return body


class _NativeBodyTooLarge(aiohttp.ClientPayloadError):
    pass


async def _bounded_body(response: aiohttp.ClientResponse, maximum: int) -> bytes:
    body = bytearray()
    async for chunk in response.content.iter_chunked(64 * 1024):
        if len(body) + len(chunk) > maximum:
            raise _NativeBodyTooLarge("Native upstream body exceeded the gateway response bound")
        body.extend(chunk)
    return bytes(body)


def _safe_native_frame(frame: bytes, secrets: list[str]) -> bytes:
    lines = re.split(rb"\r\n|\r|\n", frame)
    if any(line.startswith(b"event:") and line[6:].strip() == b"error" for line in lines):
        return _redact(frame, secrets)
    data = b"\n".join(line[5:].lstrip(b" ") for line in lines if line.startswith(b"data:"))
    try:
        payload = json.loads(data)
    except (ValueError, UnicodeError):
        return frame
    return _redact(frame, secrets) if isinstance(payload, dict) and payload.get("type") == "error" else frame


def native_oauth_payload(source: ModelSource, payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Align only Claude Code's existing JSON-encoded user_id account identity."""
    if source.provider != "claude" or source.auth_type != "claude_oauth":
        return payload
    metadata = payload.get("metadata")
    user_id = metadata.get("user_id") if isinstance(metadata, dict) else None
    if not isinstance(user_id, str):
        return payload
    try:
        identity = json.loads(user_id)
    except ValueError:
        return payload
    if not isinstance(identity, dict) or not isinstance(identity.get("account_uuid"), str):
        return payload
    try:
        UUID(identity["account_uuid"])
    except ValueError:
        return payload
    credential = source.oauth_credential
    if credential is None or credential.account_uuid is None:
        raise ProviderError("Claude account identity is unavailable")
    if identity["account_uuid"] == credential.account_uuid:
        return payload
    identity["account_uuid"] = credential.account_uuid
    return {**payload, "metadata": {**metadata, "user_id": json.dumps(identity, separators=(",", ":"))}}


async def open_native(
    source: ModelSource,
    payload: dict[str, JsonValue],
    *,
    incoming_headers: Mapping[str, str],
    count_tokens: bool = False,
    query_string: str = "",
    expected_principal: str | None = None,
) -> NativeResponse | SourceResponsesStream:
    headers = await upstream_headers(source, protocol="messages", incoming_headers=incoming_headers)
    if expected_principal is not None and native_source_principal(source) != expected_principal:
        raise ProviderError("Selected native source identity changed before dispatch")
    if not count_tokens:
        payload = native_oauth_payload(source, payload)
    headers["content-type"] = "application/json"
    streaming = payload.get("stream") is True and not count_tokens
    headers["accept"] = "text/event-stream" if streaming else "application/json"
    secrets = [
        value.removeprefix("Bearer ") for key, value in headers.items() if key.lower() in {"authorization", "x-api-key"}
    ]
    secrets.extend(
        value.removeprefix("Bearer ")
        for key, value in incoming_headers.items()
        if key.lower() in {"authorization", "x-api-key"}
    )
    base = (source.messages_base_url or source.base_url).rstrip("/")
    path = "/messages/count_tokens" if count_tokens else "/messages"
    url = base + (path if base.endswith("/v1") else "/v1" + path)
    if query_string:
        url += "?" + query_string
    stack = AsyncExitStack()
    try:
        session = await stack.enter_async_context(lease_model_source_session())
        response = await stack.enter_async_context(
            session.post(
                url,
                json=payload,
                headers=headers,
                timeout=_source_client_timeout(source),
                allow_redirects=False,
            )
        )
        if not count_tokens:
            await record_api_rate_headers(source, payload.get("model"), response.headers)
        safe = _safe_headers(response.headers)
        for secret in secrets:
            if secret:
                safe = {name: value.replace(secret, "[redacted]") for name, value in safe.items()}
        if not 200 <= response.status < 300:
            try:
                body = _redact(await _bounded_body(response, NATIVE_ERROR_MAX_BYTES), secrets)
            except _NativeBodyTooLarge:
                body = (
                    b'{"type":"error","error":{"type":"api_error",'
                    b'"message":"Upstream error body exceeded the gateway response bound"}}'
                )
                safe["content-type"] = "application/json"
            raise NativeRejected(NativeResponse(body, response.status, safe))
        if not streaming:
            body = await _bounded_body(response, NATIVE_JSON_MAX_BYTES)
            usage = None
            try:
                data = json.loads(body)
            except (ValueError, UnicodeError) as exc:
                raise aiohttp.ClientPayloadError("Native upstream did not return valid JSON") from exc
            if not isinstance(data, dict):
                raise aiohttp.ClientPayloadError("Native upstream did not return a JSON object")
            if data.get("type") == "error":
                raise NativeRejected(NativeResponse(_redact(body, secrets), response.status, safe))
            validate_native_completion(data, count_tokens=count_tokens)
            if not count_tokens:
                usage = native_usage(data, response.headers.get("anthropic-model"))
            result = NativeResponse(body, response.status, safe, usage, frozenset(native_continuation_tokens(data)))
            await _await_cleanup_deferring_cancellation(stack.aclose())
            return result
        holder = SourceUsageHolder()
        parser = NativeUsageParser(holder)
        transport = SourceStreamTransport(stack, scheduler=REAL_SCHEDULER)

        async def body_stream() -> AsyncIterator[bytes]:
            chunks = response.content.iter_chunked(4096).__aiter__()
            pending = b""
            try:
                while True:
                    try:
                        chunk = await REAL_SCHEDULER.wait_for(anext(chunks), source_stream_idle_seconds())
                    except StopAsyncIteration:
                        if pending:
                            raise NativeStreamIncomplete(partial_frame=True)
                        parser.finish()
                        if holder.terminal_kind is None:
                            raise NativeStreamIncomplete(partial_frame=False)
                        break
                    pending += chunk
                    while match := _FRAME_BOUNDARY.search(pending):
                        frame, pending = pending[: match.end()], pending[match.end() :]
                        if len(frame) > 1_048_576:
                            raise aiohttp.ClientPayloadError("Native SSE frame exceeded the gateway response bound")
                        frame = _safe_native_frame(frame, secrets)
                        parser.feed(frame)
                        yield frame
                        if holder.terminal_kind in {"completed", "error"}:
                            return
                    if len(pending) > 1_048_576:
                        raise aiohttp.ClientPayloadError("Native SSE frame exceeded the gateway response bound")
            except NativeStreamIncomplete:
                raise
            except (aiohttp.ClientError, TimeoutError) as exc:
                raise NativeStreamIncomplete(partial_frame=bool(pending)) from exc
            finally:
                await transport.aclose()

        return NativeStream(body_stream(), holder, response.status, transport=transport, headers=safe)
    except BaseException:
        await _await_cleanup_deferring_cancellation(stack.aclose())
        raise
