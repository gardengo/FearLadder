"""Telegram notification provider (TASK-120).

Secrets come from the environment and nowhere else (``ARCHITECTURE.md`` §9).
``alerts.yaml`` holds the *names* of the variables, never their values, and the
configuration schema rejects anything that does not look like a variable name.

Two failure modes are handled deliberately:

* **Not configured.** Missing credentials raise a clear error naming the
  variables, rather than silently doing nothing. A monitoring system that
  quietly stops notifying is worse than one that fails loudly.
* **Rate limited.** Telegram answers 429 with ``retry_after``; that is honoured
  instead of hammering. Other 4xx responses are permanent and are not retried.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Self

from fear_ladder.alerts.engine import NotificationError
from fear_ladder.alerts.templates import RenderedAlert, as_html
from fear_ladder.config.schema import TelegramSpec
from fear_ladder.data.models import AlertEvent

logger = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"
#: Telegram rejects messages beyond this; longer bodies are truncated with a note.
MAX_MESSAGE_CHARS = 4096
TRUNCATION_NOTE = "\n\n… (메시지가 잘렸습니다. 대시보드에서 전체 내용을 확인하세요.)"


class TelegramNotConfiguredError(NotificationError):
    """Raised when the bot token or chat id is missing."""


@dataclass(frozen=True, slots=True)
class TelegramNotifier:
    """Sends alerts to one Telegram chat."""

    bot_token: str
    chat_id: str
    parse_mode: str = "HTML"
    timeout_seconds: float = 15.0
    attempts: int = 3
    backoff_seconds: float = 2.0
    api_base: str = API_BASE
    name: str = "telegram"
    delivers: bool = True
    #: Injected so tests never touch the network and never sleep.
    transport: Callable[[str, bytes, float], dict[str, Any]] | None = None
    sleep: Callable[[float], None] = time.sleep

    @classmethod
    def from_spec(
        cls, spec: TelegramSpec, env: Mapping[str, str] | None = None, **overrides: Any
    ) -> Self:
        source = os.environ if env is None else env
        token = (source.get(spec.bot_token_env) or "").strip()
        chat_id = (source.get(spec.chat_id_env) or "").strip()
        missing = [
            name
            for name, value in ((spec.bot_token_env, token), (spec.chat_id_env, chat_id))
            if not value
        ]
        if missing:
            raise TelegramNotConfiguredError(
                f"Telegram is enabled but {', '.join(missing)} is not set. "
                "Set it as a GitHub Actions secret or in your environment; it must "
                "never be written into config or committed (ARCHITECTURE.md 9)."
            )
        return cls(
            bot_token=token,
            chat_id=chat_id,
            parse_mode=spec.parse_mode,
            timeout_seconds=spec.timeout_seconds,
            attempts=spec.retry.attempts,
            backoff_seconds=spec.retry.backoff_seconds,
            **overrides,
        )

    # -- sending -----------------------------------------------------------
    def send(self, alert: AlertEvent) -> None:
        self.send_text(self._format(alert))

    def send_text(self, text: str) -> None:
        payload: dict[str, Any] = {
            "chat_id": self.chat_id,
            "text": _truncate(text),
            "disable_web_page_preview": True,
        }
        if self.parse_mode != "none":
            payload["parse_mode"] = self.parse_mode

        url = f"{self.api_base}/bot{self.bot_token}/sendMessage"
        body = urllib.parse.urlencode(payload).encode("utf-8")

        last_error: str | None = None
        for attempt in range(1, self.attempts + 1):
            try:
                response = self._post(url, body)
            except _RateLimitedError as exc:
                last_error = f"rate limited, retry after {exc.retry_after}s"
                logger.warning("telegram %s (attempt %d/%d)", last_error, attempt, self.attempts)
                if attempt < self.attempts:
                    self.sleep(exc.retry_after)
                continue
            except _PermanentError as exc:
                # 400/401/403: retrying cannot help and would only delay the log.
                raise NotificationError(f"telegram rejected the message: {exc}") from exc
            except Exception as exc:  # network errors vary in type
                last_error = str(exc)
                logger.warning(
                    "telegram send failed (attempt %d/%d): %s", attempt, self.attempts, exc
                )
                if attempt < self.attempts:
                    self.sleep(self.backoff_seconds * attempt)
                continue

            if response.get("ok"):
                return
            last_error = str(response.get("description") or response)

        raise NotificationError(
            f"telegram send failed after {self.attempts} attempts: {last_error}"
        )

    def _format(self, alert: AlertEvent) -> str:
        rendered = RenderedAlert(title=alert.title, body=alert.body, payload=alert.payload)
        return as_html(rendered) if self.parse_mode == "HTML" else rendered.as_text()

    def _post(self, url: str, body: bytes) -> dict[str, Any]:
        # Classification wraps *both* paths. Putting it only around urllib would
        # mean an injected transport's HTTP errors fall through to the generic
        # retry, so a permanent 400 would be retried three times.
        try:
            if self.transport is not None:
                return self.transport(url, body, self.timeout_seconds)
            request = urllib.request.Request(  # fixed https API base
                url,
                data=body,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            detail = _read_error(exc)
            if exc.code == 429:
                raise _RateLimitedError(_retry_after(detail, self.backoff_seconds)) from exc
            if 400 <= exc.code < 500:
                raise _PermanentError(f"HTTP {exc.code}: {detail}") from exc
            raise NotificationError(f"telegram HTTP {exc.code}: {detail}") from exc


@dataclass(slots=True)
class _RateLimitedError(Exception):
    retry_after: float


@dataclass(slots=True)
class _PermanentError(Exception):
    detail: str

    def __str__(self) -> str:
        return self.detail


def _read_error(exc: urllib.error.HTTPError) -> str:
    try:
        return exc.read().decode("utf-8", errors="replace")[:500]
    except Exception:  # the body may already be consumed
        return exc.reason or ""


def _retry_after(detail: str, default: float) -> float:
    try:
        payload = json.loads(detail)
        return float(payload.get("parameters", {}).get("retry_after", default))
    except (json.JSONDecodeError, TypeError, ValueError):
        return default


def _truncate(text: str) -> str:
    if len(text) <= MAX_MESSAGE_CHARS:
        return text
    keep = MAX_MESSAGE_CHARS - len(TRUNCATION_NOTE)
    return text[:keep] + TRUNCATION_NOTE


@dataclass(slots=True)
class RecordingNotifier:
    """A provider that captures messages. For dry runs and tests."""

    name: str = "recording"
    #: Stands in for a real channel in tests, so it does claim delivery.
    delivers: bool = True
    messages: list[AlertEvent] = field(default_factory=list)

    def send(self, alert: AlertEvent) -> None:
        self.messages.append(alert)
