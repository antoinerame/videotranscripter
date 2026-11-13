"""Middleware ASGI : authentification par token + logs de requêtes (sans jamais logger le token).

Deux façons de s'authentifier :
  * token dans le chemin : POST /mcp/<TOKEN>        (pratique pour les connecteurs claude.ai)
  * header :              POST /mcp + Authorization: Bearer <TOKEN>

La comparaison est faite en temps constant (hmac.compare_digest sur des empreintes SHA-256,
pour ne pas non plus révéler la longueur du token).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

log = logging.getLogger("x_transcript.http")


def _digest(value: str | bytes) -> bytes:
    if isinstance(value, str):
        value = value.encode("utf-8", "surrogateescape")
    return hashlib.sha256(value).digest()


class TokenAuthMiddleware:
    def __init__(self, app: ASGIApp, token: str, mcp_path: str = "/mcp"):
        self.app = app
        self._token_digest = _digest(token)
        self.mcp_path = mcp_path.rstrip("/")

    def _check(self, candidate: str | bytes | None) -> bool:
        if not candidate:
            return False
        return hmac.compare_digest(_digest(candidate), self._token_digest)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":  # lifespan (et websockets, non utilisés)
            await self.app(scope, receive, send)
            return

        start = time.monotonic()
        path: str = scope.get("path", "")
        method: str = scope.get("method", "")
        client = _client_ip(scope)
        shown_path = path
        status_holder: dict[str, int] = {}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
            await send(message)

        def log_request() -> None:
            status = status_holder.get("status", 0)
            level = logging.INFO if status < 400 or status in (404, 405) else logging.WARNING
            log.log(level, "%s %s %s -> %d (%.0f ms)", client, method, shown_path, status,
                    (time.monotonic() - start) * 1000)  # fmt: skip

        if path in ("/health", "/healthz"):
            await _json(send_wrapper, 200, {"status": "ok"})
            log.debug("%s %s %s -> 200", client, method, path)
            return

        authorized = False
        if path.startswith(self.mcp_path + "/"):
            # /mcp/<token>  (un éventuel "/" final est toléré)
            candidate = path[len(self.mcp_path) + 1 :].rstrip("/")
            shown_path = f"{self.mcp_path}/***"
            if candidate and "/" not in candidate and self._check(candidate):
                authorized = True
                scope = dict(scope)
                scope["path"] = self.mcp_path
                scope["raw_path"] = self.mcp_path.encode()
        elif path == self.mcp_path:
            header = _header(scope, b"authorization")
            if header and header[:7].lower() == b"bearer ":
                authorized = self._check(header[7:].strip())
        else:
            # pas d'endpoint OAuth / .well-known : on répond 404 proprement
            await _json(send_wrapper, 404, {"error": "not_found"})
            log_request()
            return

        if not authorized:
            await _json(
                send_wrapper,
                401,
                {"error": "unauthorized", "error_description": "Token MCP manquant ou invalide."},
                extra_headers=[(b"www-authenticate", b'Bearer realm="mcp"')],
            )
            log_request()
            return

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            log_request()


def _header(scope: Scope, name: bytes) -> bytes | None:
    for key, value in scope.get("headers", []):
        if key.lower() == name:
            return value
    return None


def _client_ip(scope: Scope) -> str:
    client = scope.get("client")
    return client[0] if client else "-"


async def _json(
    send: Send, status: int, body: dict[str, Any], extra_headers: list[tuple[bytes, bytes]] | None = None
) -> None:
    payload = json.dumps(body).encode()
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(payload)).encode())]
    headers.extend(extra_headers or [])
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": payload})
