"""Offline stand-ins for http.Client: canned JSON by URL substring."""
from __future__ import annotations

import json
import os
from typing import Any, Callable

from radar.http import HttpError

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def fixture(name: str) -> dict[str, Any]:
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        return json.load(f)


class FakeClient:
    """Routes get_json/post_json to handlers keyed by URL substring; records calls."""

    def __init__(self, routes: dict[str, Callable[[dict[str, Any]], dict[str, Any]]]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.steam_calls = 0

    def _go(self, url: str, params: dict[str, Any] | None) -> dict[str, Any]:
        self.calls.append((url, params or {}))
        for frag, fn in self.routes.items():
            if frag in url:
                self.steam_calls += 1
                return fn(params or {})
        raise HttpError(f"no route for {url}")

    def get_json(self, url: str, params: dict[str, Any] | None = None,
                 headers: dict[str, str] | None = None) -> dict[str, Any]:
        return self._go(url, params)

    def post_json(self, url: str, data: dict[str, Any] | None = None,
                  headers: dict[str, str] | None = None) -> dict[str, Any]:
        return self._go(url, data)


class _Usage:
    def __init__(self, i: int, o: int) -> None:
        self.input_tokens, self.output_tokens = i, o


class _Block:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class Msg:
    """A final message as the SDK returns it (content blocks, stop_reason, usage)."""

    def __init__(self, data: Any, stop: str = "end_turn", i: int = 1000, o: int = 500) -> None:
        self.content = [_Block(data if isinstance(data, str) else json.dumps(data))]
        self.stop_reason = stop
        self.usage = _Usage(i, o)


class _Stream:
    def __init__(self, msg: Any) -> None:
        self.msg = msg

    def __enter__(self) -> "_Stream":
        if isinstance(self.msg, BaseException):
            raise self.msg
        return self

    def __exit__(self, *a: Any) -> None:
        return None

    def get_final_message(self) -> Any:
        return self.msg


class FakeAnthropic:
    """client.messages.stream(**kw) -> canned Msg or raised exception; `answers` maps model -> callable(kwargs)."""

    def __init__(self, answers: dict[str, Callable[[dict[str, Any]], Any]]) -> None:
        self.answers = answers
        self.calls: list[dict[str, Any]] = []
        self.messages = self

    def stream(self, **kw: Any) -> _Stream:
        self.calls.append(kw)
        return _Stream(self.answers[kw["model"]](kw))
