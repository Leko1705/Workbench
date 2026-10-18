"""Building blocks for browser logins: server-side sessions, login throttling,
Origin checking and security headers. Shared by worker and workbench servers."""
from __future__ import annotations

import hashlib
import math
import secrets
import time
from collections import deque
from typing import Callable
from urllib.parse import urlparse

from starlette.datastructures import MutableHeaders

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _h(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class SessionStore:
    """In-memory sessions. Only a hash of the cookie value is kept server-side.

    A session dies after `idle_timeout` seconds without use or `max_age` seconds
    after login. Sessions do not survive a restart (users just log in again).
    """

    def __init__(self, idle_timeout: float = 1800, max_age: float = 12 * 3600,
                 clock: Callable[[], float] = time.monotonic):
        self.idle_timeout, self.max_age, self._clock = idle_timeout, max_age, clock
        self._sessions: dict[str, dict] = {}

    def create(self) -> tuple[str, dict]:
        self._purge()
        token, now = secrets.token_urlsafe(32), self._clock()
        sess = {"csrf": secrets.token_urlsafe(32), "created": now, "last": now}
        self._sessions[_h(token)] = sess
        return token, sess

    def get(self, token: str | None) -> dict | None:
        if not token:
            return None
        key, sess = _h(token), self._sessions.get(_h(token))
        if sess is None:
            return None
        now = self._clock()
        if now - sess["last"] > self.idle_timeout or now - sess["created"] > self.max_age:
            del self._sessions[key]
            return None
        sess["last"] = now
        return sess

    def destroy(self, token: str | None) -> None:
        if token:
            self._sessions.pop(_h(token), None)

    def _purge(self) -> None:
        now = self._clock()
        for k in [k for k, s in self._sessions.items()
                  if now - s["last"] > self.idle_timeout or now - s["created"] > self.max_age]:
            del self._sessions[k]


class LoginLimiter:
    """Sliding-window throttle: `max_failures` failed logins per `window` seconds per key."""

    def __init__(self, max_failures: int = 5, window: float = 300.0,
                 clock: Callable[[], float] = time.monotonic):
        self.max_failures, self.window, self._clock = max_failures, window, clock
        self._fails: dict[str, deque[float]] = {}

    def retry_after(self, key: str) -> int:
        """Seconds the caller must wait, or 0 if it may try."""
        q = self._fails.get(key)
        if q is None:
            return 0
        now = self._clock()
        while q and now - q[0] > self.window:
            q.popleft()
        if not q:
            del self._fails[key]
            return 0
        return math.ceil(q[0] + self.window - now) if len(q) >= self.max_failures else 0

    def failure(self, key: str) -> None:
        if len(self._fails) > 10_000:          # bound memory under a distributed attack
            self._fails.clear()
        self._fails.setdefault(key, deque()).append(self._clock())

    def success(self, key: str) -> None:
        self._fails.pop(key, None)


def origin_allowed(origin: str | None, allowed_hosts: set[str]) -> bool:
    """CSRF layer: a browser-sent Origin must be one of our own hosts.

    A missing Origin is allowed (non-browser clients, some same-origin requests);
    the CSRF token is the primary defence.
    """
    return origin is None or urlparse(origin).netloc in allowed_hosts


UI_CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
          "connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


class SecurityHeaders:
    """Pure-ASGI middleware: hardening headers on /ui and /admin responses only
    (leaves the streaming A2A endpoint untouched)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope["type"] != "http" or not (
                path == "/ui" or path.startswith(("/ui/", "/admin"))):
            return await self.app(scope, receive, send)

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["Content-Security-Policy"] = UI_CSP
                headers["X-Content-Type-Options"] = "nosniff"
                headers["X-Frame-Options"] = "DENY"
                headers["Referrer-Policy"] = "no-referrer"
                if path.startswith("/admin"):
                    headers["Cache-Control"] = "no-store"   # responses can contain secrets
            await send(message)

        await self.app(scope, receive, send_wrapper)