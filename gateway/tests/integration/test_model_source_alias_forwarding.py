from __future__ import annotations

import pytest
from aiohttp import web

from tests.integration.model_source_helpers import _enable_api_key_auth, stub_source_upstreams

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["chat/completions", "responses", "embeddings", "audio/transcriptions", "messages"])
async def test_namespaced_metered_models_are_callable_with_only_their_upstream_identity(async_client, surface):
    await _enable_api_key_auth(async_client)
    native = "native-model"
    alias = ("anthropic/" if surface == "messages" else "openai/") + native

    async def upstream(request):
        if request.path != "/v1/" + surface:
            raise web.HTTPNotFound()
        if surface == "messages":
            authenticated = (
                request.headers.get("x-api-key") == "synthetic-source-key" and "authorization" not in request.headers
            )
        else:
            authenticated = request.headers.get("authorization") == "Bearer " + "synthetic-source-key"
        if not authenticated:
            raise web.HTTPUnauthorized()
        if surface == "audio/transcriptions":
            reader = await request.multipart()
            async for part in reader:
                if part.name == "model":
                    if await part.text() != native:
                        raise web.HTTPBadRequest(reason="Unknown native model")
                else:
                    await part.read()
            return web.json_response({"text": "synthetic transcription"})
        body = await request.json()
        if body["model"] != native:
            raise web.HTTPBadRequest(reason="Unknown native model")
        if surface == "embeddings":
            return web.json_response({
                "object": "list", "data": [{"index": 0, "object": "embedding", "embedding": [0.25, -0.5]}],
                "model": native, "usage": {"prompt_tokens": 1, "total_tokens": 1},
            })
        if surface == "messages":
            return web.json_response({"id": "msg_alias", "type": "message", "role": "assistant",
                "model": native, "content": [{"type": "text", "text": "synthetic reply"}],
                "stop_reason": "end_turn", "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 1}})
        if surface == "responses":
            return web.json_response({"id": "resp_alias", "object": "response", "status": "completed",
                "model": native, "output": [{"type": "message", "id": "msg_alias", "role": "assistant",
                "status": "completed", "content": [{
                    "type": "output_text", "text": "synthetic reply", "annotations": [],
                }]}],
                "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}})
        return web.json_response({"id": "chatcmpl-alias", "object": "chat.completion", "created": 0,
            "model": native, "choices": [{"index": 0, "message": {"role": "assistant", "content": "synthetic reply"},
            "finish_reason": "stop"}], "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})

    async with stub_source_upstreams() as start:
        base_url = await start(upstream)
        created = await async_client.post("/api/model-sources/", json={
            "name": "alias-transport", "baseUrl": base_url, "apiKey": "synthetic-source-key",
            "supportsChatCompletions": surface == "chat/completions", "supportsResponses": surface == "responses",
            "supportsEmbeddings": surface == "embeddings",
            "supportsAudioTranscriptions": surface == "audio/transcriptions",
            "supportsMessages": surface == "messages", "messagesBaseUrl": base_url.removesuffix("/v1"),
            "models": [{"model": alias, "upstreamModel": native, "contextWindow": 8192, "maxOutputTokens": 1024}],
        })
        assert created.status_code == 200, created.text
        key = await async_client.post("/api/api-keys/", json={
            "name": "alias-client", "assignedSourceIds": [created.json()["id"]],
        })
        assert key.status_code == 200, key.text
        headers = {"Authorization": "Bearer " + key.json()["key"]}
        if surface == "audio/transcriptions":
            response = await async_client.post("/v1/" + surface, headers=headers, files=[
                ("model", (None, alias)), ("file", ("fixture.wav", b"synthetic-audio", "audio/wav")),
            ])
        else:
            payload = {"model": alias}
            if surface in {"chat/completions", "messages"}:
                payload["messages"] = [{"role": "user", "content": "synthetic prompt"}]
            else:
                payload["input"] = "synthetic prompt"
            if surface == "messages":
                payload["max_tokens"] = 16
            response = await async_client.post("/v1/" + surface, headers=headers, json=payload)
        assert response.status_code == 200, response.text
        result = response.json()
        if surface == "audio/transcriptions":
            assert result["text"] == "synthetic transcription"
        elif surface == "embeddings":
            assert result["data"][0]["embedding"] == [0.25, -0.5]
        elif surface == "responses":
            assert result["status"] == "completed"
            assert result["output"][0]["content"][0]["text"] == "synthetic reply"
        elif surface == "messages":
            assert result["stop_reason"] == "end_turn"
            assert result["content"][0]["text"] == "synthetic reply"
        else:
            assert result["choices"][0]["message"]["content"] == "synthetic reply"
