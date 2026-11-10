"""One polite shared HTTP client: per-host spacing, backoff retries, Steam call budget.

WHY: a daily job hitting ~100k endpoints must never get us banned or leak the API key into logs.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any, Callable
from urllib.parse import urlparse

import requests

from . import config

log = logging.getLogger(__name__)

_KEY_RE = re.compile(r"(key|client_secret|access_token)=[^&\s'\")]+", re.I)


def redact(text: str) -> str:
    """Strip secrets from any string that may reach a log line."""
    return _KEY_RE.sub(r"\1=REDACTED", text)


class HttpError(Exception):
    """A request failed for good (message is already redacted)."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class CallBudgetExceeded(HttpError):
    """The per-run Steam Web API call ceiling was reached."""


class Client:
    """Shared session; `get_json`/`post_json` return parsed JSON or raise HttpError."""

    def __init__(self, steam_call_limit: int = config.STEAM_CALL_LIMIT,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic,
                 session: requests.Session | None = None) -> None:
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = config.USER_AGENT
        self.steam_call_limit = steam_call_limit
        self.steam_calls = 0
        self._sleep = sleep
        self._clock = clock
        self._last: dict[str, float] = {}

    def _pace(self, host: str) -> None:
        gap = config.HOST_MIN_INTERVAL.get(host, config.DEFAULT_MIN_INTERVAL)
        wait = self._last.get(host, -1e9) + gap - self._clock()
        if wait > 0:
            self._sleep(wait)
        self._last[host] = self._clock()

    def request_json(self, method: str, url: str, params: dict[str, Any] | None = None,
                     data: dict[str, Any] | None = None,
                     headers: dict[str, str] | None = None) -> dict[str, Any]:
        """Send with pacing, retries (429/5xx/timeouts, honouring Retry-After) and call counting."""
        host = urlparse(url).netloc
        path = urlparse(url).path
        for attempt in range(config.MAX_RETRIES + 1):
            if host == "api.steampowered.com":
                if self.steam_calls >= self.steam_call_limit:
                    raise CallBudgetExceeded(f"Steam call limit {self.steam_call_limit} reached")
                self.steam_calls += 1
            self._pace(host)
            delay = min(2.0 ** (attempt + 1), 60.0)
            try:
                resp = self.session.request(method, url, params=params, data=data,
                                            headers=headers, timeout=config.HTTP_TIMEOUT)
            except (requests.Timeout, requests.ConnectionError) as exc:
                if attempt >= config.MAX_RETRIES:
                    raise HttpError(f"{host}{path}: {redact(str(exc))[:200]}") from None
                log.debug("retry %d on %s: %s", attempt + 1, host, type(exc).__name__)
                self._sleep(delay)
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt >= config.MAX_RETRIES:
                    raise HttpError(f"{host}{path}: HTTP {resp.status_code} after retries")
                ra = resp.headers.get("Retry-After", "")
                if ra.isdigit():
                    delay = min(float(ra), 300.0)
                log.debug("HTTP %d on %s, sleeping %.1fs", resp.status_code, host, delay)
                self._sleep(delay)
                continue
            if resp.status_code >= 400:
                raise HttpError(f"{host}{path}: HTTP {resp.status_code}", resp.status_code)
            try:
                return resp.json()
            except ValueError:
                raise HttpError(f"{host}{path}: invalid JSON") from None
        raise HttpError("unreachable")  # pragma: no cover

    def get_json(self, url: str, params: dict[str, Any] | None = None,
                 headers: dict[str, str] | None = None) -> dict[str, Any]:
        """GET returning JSON."""
        return self.request_json("GET", url, params=params, headers=headers)

    def post_json(self, url: str, data: dict[str, Any] | None = None,
                  headers: dict[str, str] | None = None) -> dict[str, Any]:
        """POST (form) returning JSON."""
        return self.request_json("POST", url, data=data, headers=headers)
