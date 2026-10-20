"""Prevent caching client quota responses, including pre-handler refusals."""

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class ClientQuotaCacheMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") not in {"/v1/usage/quota", "/v1/usage/quota/"}:
            await self.app(scope, receive, send)
            return

        async def no_store(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(raw=message.setdefault("headers", []))["Cache-Control"] = "no-store"
            await send(message)

        await self.app(scope, receive, no_store)
