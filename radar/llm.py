"""One guarded door to the Anthropic API: a budget check before every call, a spend row after it.

WHY: the weekly digest must never spend more than the caps in config, whatever a retry loop or a bad
prompt does, and a billing problem must surface as one clear error instead of a stack trace.
The key is read by the SDK from ANTHROPIC_API_KEY and is never logged.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any

from . import config
from .http import redact

log = logging.getLogger(__name__)


class LlmError(Exception):
    """The call failed or its answer is unusable."""


class LlmRefusal(LlmError):
    """The model declined to answer (stop_reason refusal)."""


class OutOfCredit(LlmError):
    """Billing problem: no credit left or the Console usage limit is reached."""


class BudgetExceeded(LlmError):
    """The worst case of the next call would pass a cap in config; no call was made."""


def month_start(now: datetime) -> str:
    """First instant of the UTC month, ISO, for comparing against spend.ts."""
    return now.strftime("%Y-%m-01T00:00:00")


def month_spend(con: sqlite3.Connection, now: datetime | None = None) -> float:
    """USD spent so far in the current UTC month (sum of spend.usd)."""
    now = now or datetime.now(timezone.utc)
    row = con.execute("SELECT COALESCE(SUM(usd), 0) FROM spend WHERE ts >= ?", (month_start(now),)).fetchone()
    return float(row[0])


def usd(model: str, input_tokens: int, output_tokens: int) -> float:
    """Cost of a call from the price table; an unknown model is a config error, not a free call."""
    if model not in config.PRICES:
        raise LlmError(f"no price for model {model!r} in config.PRICES")
    pin, pout = config.PRICES[model]
    return (input_tokens * pin + output_tokens * pout) / 1_000_000


class Llm:
    """Budgeted JSON-schema calls. `client` is injectable for tests; the real one is built lazily."""

    def __init__(self, client: Any = None, con: sqlite3.Connection | None = None,
                 monthly_cap: float | None = None, run_cap: float | None = None) -> None:
        if con is None:
            raise ValueError("Llm needs a database connection for the spend table")
        self._client = client
        self.con = con
        self.monthly_cap = config.LLM_MONTHLY_CAP_USD if monthly_cap is None else monthly_cap
        self.run_cap = config.LLM_RUN_CAP_USD if run_cap is None else run_cap
        self.run_spend = 0.0

    @property
    def client(self) -> Any:
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic()
        return self._client

    def month_spend(self) -> float:
        return month_spend(self.con)

    def _check_budget(self, model: str, system: str, user: str, max_tokens: int) -> None:
        est_in = (len(system) + len(user)) // 3 + 1
        worst = usd(model, est_in, max_tokens)
        if self.month_spend() + worst > self.monthly_cap:
            raise BudgetExceeded(f"monthly cap ${self.monthly_cap:.2f} would be passed "
                                 f"(spent ${self.month_spend():.2f}, next call worst case ${worst:.2f})")
        if self.run_spend + worst > self.run_cap:
            raise BudgetExceeded(f"run cap ${self.run_cap:.2f} would be passed "
                                 f"(spent ${self.run_spend:.2f}, next call worst case ${worst:.2f})")

    def call_json(self, model: str, effort: str, max_tokens: int, system: str, user: str,
                  schema: dict[str, Any], purpose: str) -> dict[str, Any]:
        """One streamed call with a JSON-schema answer; returns the parsed object."""
        self._check_budget(model, system, user, max_tokens)
        try:
            with self.client.messages.stream(
                    model=model, max_tokens=max_tokens, system=system,
                    messages=[{"role": "user", "content": user}],
                    output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}}) as s:
                msg = s.get_final_message()
        except Exception as exc:  # the SDK's own error types are matched below without importing it eagerly
            raise self._translate(exc) from None
        cost = usd(model, msg.usage.input_tokens, msg.usage.output_tokens)
        self.con.execute("INSERT INTO spend(ts, model, input_tokens, output_tokens, usd, purpose) VALUES(?,?,?,?,?,?)",
                         (datetime.now(timezone.utc).isoformat(timespec="seconds"), model,
                          msg.usage.input_tokens, msg.usage.output_tokens, cost, purpose))
        self.con.commit()
        self.run_spend += cost
        if msg.stop_reason == "refusal":
            raise LlmRefusal(f"{purpose}: the model refused")
        if msg.stop_reason == "max_tokens":
            raise LlmError(f"{purpose}: answer cut off at max_tokens={max_tokens}")
        try:
            text = next(b.text for b in msg.content if b.type == "text")
            data = json.loads(text)
        except (StopIteration, ValueError) as exc:
            raise LlmError(f"{purpose}: no JSON answer ({exc})") from None
        if not isinstance(data, dict):
            raise LlmError(f"{purpose}: answer is not a JSON object")
        return data

    @staticmethod
    def _translate(exc: Exception) -> Exception:
        """Map an SDK/transport failure to our errors; the text is redacted."""
        text = redact(str(exc))
        status = getattr(exc, "status_code", None)
        body = getattr(exc, "body", None)
        etype = getattr(exc, "type", None) or (
            (body.get("error") or {}).get("type") if isinstance(body, dict) and isinstance(body.get("error"), dict)
            else None)
        low = text.lower()
        if status == 402 or etype == "billing_error" or "credit balance" in low or "usage limit" in low:
            return OutOfCredit(text)
        return LlmError(text)
