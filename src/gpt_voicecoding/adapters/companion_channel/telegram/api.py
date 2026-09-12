"""The only file in this repository that speaks HTTP to Telegram.

Confined on purpose, the way the audio stack is confined to one module: this
repository takes no third-party dependencies and hand-rolls the little it needs,
so "the Telegram library never reaches Bridge Core" is not a rule a dependency
list can enforce. What can be enforced is that the wire lives in exactly one
file, and `tests/test_architecture.py` asserts it — every other module in this
subpackage is ordinary Python that could not open a socket if it tried.

Bot API calls and the binding handshake live here, with each refusal
**classified by the layer that produced it**. That classification is
what makes ADR 0003's liveness truthful — "Telegram did not answer" is not a
diagnosis, and an operator staring at a status line needs to know whether their
token is wrong, their network is down, or their chat id points nowhere.

The token never appears in an error message. It is in the URL every request is
built from, so an exception that quoted the URL would put a live credential into
the engine's log; failures are named by method instead.

The transport is synchronous and blocking. Both the adapter and binding run
it on daemon workers, so a long poll occupies neither the event loop nor
the default executor that Python must join at shutdown.
"""

from __future__ import annotations

import asyncio
import http.client
import json
import threading
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Any, Protocol

from gpt_voicecoding.adapters.companion_channel.telegram.settings import (
    DEFAULT_API_ROOT,
    DEFAULT_REQUEST_TIMEOUT_SECONDS,
)


class FailureLayer(StrEnum):
    """Which layer of Telegram refused. A `verify` that cannot say this is not a diagnosis."""

    #: The token is wrong, revoked, or not a token at all.
    CREDENTIALS = "credentials"
    #: Nothing reached Telegram: DNS, routing, TLS, a timeout, a captive portal.
    NETWORK = "network"
    #: Telegram answered, and the chat this channel is configured for is not
    #: somewhere this bot can reach — the outage that looks healthiest.
    DESTINATION = "destination"
    #: Telegram answered and refused for a reason that is none of the above.
    API = "api"


class TelegramError(Exception):
    """One refused call, carrying the layer that refused it."""

    def __init__(self, layer: FailureLayer, message: str) -> None:
        super().__init__(message)
        self.layer = layer

    @property
    def detail(self) -> str:
        """What a `verify` result says out loud: the layer, then what happened."""
        return f"{self.layer}: {self}"


class Transport(Protocol):
    """One Bot API method, called and answered. Blocking, and called off the loop."""

    def __call__(self, method: str, payload: dict[str, Any], *, timeout_seconds: float) -> Any:
        """Return the method's `result`, or raise `TelegramError`."""
        ...


async def on_daemon[T](operation: Callable[[], T], *, name: str) -> T:
    """Await blocking Telegram work without holding interpreter shutdown open."""
    loop = asyncio.get_running_loop()
    answer: asyncio.Future[T] = loop.create_future()

    def settle(outcome: T | BaseException) -> None:
        if answer.done():
            return
        if isinstance(outcome, BaseException):
            answer.set_exception(outcome)
        else:
            answer.set_result(outcome)

    def call() -> None:
        try:
            outcome = operation()
        except BaseException as raised:  # noqa: BLE001 - handed back whole, judged there
            outcome = raised
        try:
            loop.call_soon_threadsafe(settle, outcome)
        except RuntimeError:
            pass  # the loop has gone; there is nobody left to tell

    threading.Thread(target=call, name=name, daemon=True).start()
    return await answer


class TelegramBinding:
    """One binding: validate, wait for Start, confirm, and release the reader.

    The transport is the same one a configured bot uses. The two lifecycle
    callbacks are supplied only when an existing Telegram reader must pause;
    an unbound engine needs neither a Companion Channel nor a second wire.
    """

    def __init__(
        self,
        *,
        confirmation: str,
        transport_for: Callable[[str], Transport] | None = None,
        pause: Callable[[], Awaitable[bool]] | None = None,
        resume: Callable[[], Awaitable[None]] | None = None,
        timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        self._transport_for = transport_for or (
            lambda token: http_transport(token=token, api_root=DEFAULT_API_ROOT)
        )
        self._confirmation = confirmation
        self._pause = pause
        self._resume = resume
        self._timeout = timeout_seconds
        self._transport: Transport | None = None
        self._bot: dict[str, Any] = {}
        self._offset: int | None = None
        self._paused = False
        self._lock = asyncio.Lock()

    async def step(self, *, token: str | None = None, cancel: bool = False) -> dict[str, Any]:
        """A token begins; no token reads the next step; cancel releases the binding."""
        async with self._lock:
            if cancel:
                await self._finish()
                return {"cancelled": True}
            try:
                if token is not None:
                    await self._finish()
                    if self._pause is not None:
                        self._paused = await self._pause()
                    self._transport = self._transport_for(token)
                    bot = await self._ask("getMe", {})
                    self._bot = {
                        "bot_name": bot.get("first_name") or bot["username"],
                        "username": bot["username"],
                    }
                    backlog = await self._ask(
                        "getUpdates", {"offset": -1, "limit": 1, "timeout": 0}
                    )
                    self._offset = backlog[-1]["update_id"] + 1 if backlog else None
                    return {**self._bot, "chat_id": None, "chat_type": None}

                if self._transport is None:
                    raise TelegramError(
                        FailureLayer.API, "no Telegram binding is waiting for Start"
                    )
                payload: dict[str, Any] = {"timeout": 0, "allowed_updates": ["message"]}
                if self._offset is not None:
                    payload["offset"] = self._offset
                updates = await self._ask("getUpdates", payload)
                for update in updates:
                    self._offset = update["update_id"] + 1
                    message = update.get("message", {})
                    words = message.get("text", "").split()
                    if not words or words[0].split("@", 1)[0] != "/start":
                        continue
                    chat = message["chat"]
                    await self._ask(
                        "sendMessage", {"chat_id": chat["id"], "text": self._confirmation}
                    )
                    # Acknowledge Start before returning the polling slot to the adapter.
                    await self._ask(
                        "getUpdates", {"offset": self._offset, "timeout": 0, "limit": 1}
                    )
                    result = {**self._bot, "chat_id": str(chat["id"]), "chat_type": chat["type"]}
                    await self._finish()
                    return result
                return {**self._bot, "chat_id": None, "chat_type": None}
            except BaseException:
                await self._finish()
                raise

    async def _ask(self, method: str, payload: dict[str, Any]) -> Any:
        assert self._transport is not None
        transport = self._transport
        return await on_daemon(
            lambda: transport(method, payload, timeout_seconds=self._timeout),
            name=f"telegram-{method}",
        )

    async def _finish(self) -> None:
        self._transport = None
        self._bot = {}
        self._offset = None
        paused, self._paused = self._paused, False
        if paused and self._resume is not None:
            await self._resume()


def http_transport(*, token: str, api_root: str) -> Transport:
    """The real wire, bound to one bot. The only place a URL is built."""

    root = api_root.rstrip("/")

    def call(method: str, payload: dict[str, Any], *, timeout_seconds: float) -> Any:
        try:
            request = urllib.request.Request(
                url=f"{root}/bot{token}/{method}",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                body = response.read()
            return _answered(method, body)
        except urllib.error.HTTPError as refused:
            # Read before the handle closes: the body is where Telegram says why.
            failure = refused_by(method, refused.code, _description(refused.read()))
        except (urllib.error.URLError, TimeoutError, OSError) as unreachable:
            failure = TelegramError(
                FailureLayer.NETWORK, f"{method} never reached Telegram: {unreachable}"
            )
        except (ValueError, http.client.InvalidURL):
            failure = TelegramError(FailureLayer.CREDENTIALS, f"{method} received an invalid token")
        except TelegramError as refused:
            failure = refused
        raise TelegramError(failure.layer, str(failure).replace(token, "[redacted]")) from None

    return call


def refused_by(method: str, code: int, description: str) -> TelegramError:
    """Name the layer behind one refusal, from the only two facts Telegram gives.

    `401`/`403` are the token: rejected, revoked, or blocked. `404` joins them
    because the token is part of the URL path, so a malformed one makes the
    method itself not exist. A `400` mentioning the chat is the destination —
    the bot is fine and the address is not. Everything else is the API refusing
    for its own reasons, which is a real answer and must not be dressed up as
    one of the three diagnoses above.
    """
    said = description or f"HTTP {code}"
    if code in (401, 403, 404):
        return TelegramError(FailureLayer.CREDENTIALS, f"{method} was refused: {said}")
    if code == 400 and "chat" in description.lower():
        return TelegramError(FailureLayer.DESTINATION, f"{method} could not reach the chat: {said}")
    return TelegramError(FailureLayer.API, f"{method} was refused: {said}")


def _answered(method: str, body: bytes) -> Any:
    """Read one 200 response. Telegram refuses inside a 200 as readily as with a code."""
    try:
        document = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise TelegramError(
            FailureLayer.API, f"{method} answered with something that is not JSON"
        ) from None
    if not isinstance(document, dict):
        raise TelegramError(
            FailureLayer.API, f"{method} answered with something that is not a result"
        )
    if not document.get("ok"):
        try:
            code = int(document.get("error_code") or 0)
        except (TypeError, ValueError):
            raise TelegramError(
                FailureLayer.API, f"{method} answered with an invalid error code"
            ) from None
        raise refused_by(method, code, str(document.get("description") or ""))
    return document.get("result")


def _description(body: bytes) -> str:
    """Telegram's own words for a refusal, when the error body carries them."""
    try:
        document = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ""
    return str(document.get("description") or "") if isinstance(document, dict) else ""
