"""The generic Telegram Companion Channel — text reach when no Live Call is up.

Mechanism only. *When* this channel is chosen, and what happens to a notice it
could not deliver, are Bridge Core's policy and live nowhere in this file. What
lives here is one link to one chat, in both directions, and an honest answer
about whether it works.

**A long poll, not a webhook.** `getUpdates` hangs open on a worker thread and
the answer comes back to the event loop; a webhook would need a public TLS
endpoint and an inbound listener on a laptop behind NAT, for no capability this
does not already have. There is deliberately no transport option, no enum and no
dormant parameter for the road not taken.

**The backlog is discarded once, at the first contact after start.** Telegram
holds undelivered updates for about a day, so an engine that has been off since
morning would otherwise wake up and act on "turn duty off" from three hours ago,
against a state that has moved — and because inbound text is unclassified by
contract, this adapter could not be selective about it even if it wanted to be.
The accepted cost is recorded rather than hidden: **a message sent while the
engine was not running is lost.** A blip *during* a run is not the same thing —
the cursor is still in memory, the engine's state never reset, and those messages
arrive when connectivity returns.

**Unreachable is not fatal.** `connect` opens the reader and raises nothing, so a
laptop that boots with no network still gets its voice path and its control
plane; the reader retries on a bounded backoff, and `verify` says FAIL out loud
for as long as the outage lasts. A missing *token*, by contrast, refuses the
start — a variable that is not set never heals on its own, and that refusal
happens in the factory, before this class exists.

**Every Bot API call runs on a daemon thread this adapter owns, and that is
measured rather than preferred.** `asyncio.run` joins the default executor before
it returns, so anything parked on `asyncio.to_thread` holds the *process* open
long after the engine let go of it — measured at 0.20s to close the adapter and
3.01s to leave `asyncio.run`, with a 3s request in flight. At the default 25s
poll that is a quit which visibly hangs, in a process the menu-bar shell spawns
as its own child (ADR 0005). A daemon thread cannot hold an exit open, and what
it loses when the interpreter takes it mid-flight is one HTTP request: the cursor
lives in memory and dies with the process either way, and Telegram re-serves
anything it was never acknowledged for.

This rule used to apply to the *reader* alone. `send` and `verify` stayed on
`asyncio.to_thread` on the ground that "a request timeout bounds them", and that
sentence was wrong in a way worth recording: the request timeout bounds the
**call**, not the **process exit**. A `sendMessage` parked on the network when
SIGTERM arrives holds the interpreter for the rest of its 30s
`request_timeout_seconds` — measured at 28.04s after SIGTERM on the bundle's own
Python 3.12.14 — while whoever stopped the engine is holding a grace of 20s
(`tests/acceptance/support.py`) and then sends SIGKILL. A bound that is longer
than the grace is not a bound; it is the same hang with a number on it (#96).

**An abandoned call reports nothing.** A `send` whose await is cancelled on the
way out raises through `send`, so no `DeliveryReceipt` is invented for it —
neither a success for words that may never have left, nor a failure for words
that may already have arrived. What became of a Relay the engine did not live to
hear about is Bridge Core's question, not this adapter's, and #77 settles it.

**Inbound is filtered to the configured chat, and a stranger is met with
silence.** Bridge Core routes inbound text into the control-plane command set,
and `origin` is opaque to it by design, so the only component that can tell the
user's chat from a passer-by's is this one. A reply — even a refusal — would
confirm to a prober that the bot is alive and attended, so the drop is silent
and goes to the log.

**A button never crosses the seam** (ADR 0021 §4). A press arrives from Telegram
as a `callback_query`; it leaves here as the numeral the button stood for, in a
reply to the message the button was under — `InboundText(text=<data>,
in_reply_to=<that message's id>)` — with an `origin` only this adapter can read,
carrying the callback's id. When Bridge Core replies to that origin, the words
are shown as a **toast** through `answerCallbackQuery`, which is no chat message
and so lands under no id. A callback can be answered once and a toast holds 200
characters, so a longer reply, a second one, or one to a callback this process
never saw goes as an ordinary message; a toast Telegram refuses is logged and the
reply goes as a message too. Core never learns the word "button".

**The adapter reports facts about a message, never what it means.** Which message
the user replied to (`reply_to_message.message_id`), and which ids a push landed
under (`sendMessage`'s `message_id`, one per part), are read off the wire and
handed up as opaque strings. A message from the user's chat that carries no text
at all — a voice note, a photo — is handed up as empty text, so that Core's
own cannot-classify path answers it; this channel is text only.
"""

from __future__ import annotations

import asyncio
import logging
import threading

from gpt_voicecoding.adapters.companion_channel.telegram.api import (
    TelegramError,
    Transport,
)
from gpt_voicecoding.adapters.companion_channel.telegram.layout import (
    lay_out,
    prefix_within,
    utf16_length,
)
from gpt_voicecoding.adapters.companion_channel.telegram.settings import (
    MESSAGE_LIMIT_UTF16_UNITS,
    TelegramSettings,
)
from gpt_voicecoding.seams.companion_channel import ChannelReceipt, InboundText, Notice
from gpt_voicecoding.seams.delivery import Delivery
from gpt_voicecoding.seams.events import EventSink
from gpt_voicecoding.seams.identity import RequestId
from gpt_voicecoding.seams.verify import VerifyOutcome, VerifyResult

_log = logging.getLogger(__name__)

#: The two update kinds this channel is about: the user's messages and their
#: button presses. Asking for them by name keeps every other thing Telegram
#: might invent out of the reader loop entirely — and the list *persists on the
#: bot* once set (#247): the one that named `message` alone was what had been
#: keeping presses from ever arriving.
ALLOWED_UPDATES = ["message", "callback_query"]

#: How a press's origin is spelled: this prefix, then the callback query's id.
#: Adapter-private. Bridge Core echoes the string back and never reads it.
CALLBACK_ORIGIN = "callback:"

#: What a toast may hold — `answerCallbackQuery`'s own cap on `text`, "0-200
#: characters" in the API's words and counted as characters here. A longer
#: reply goes as a message; so does one Telegram refuses at its own ruler.
TOAST_LIMIT_CHARACTERS = 200

#: How many unanswered presses this adapter remembers, newest kept. A callback
#: Telegram itself forgets within seconds cannot be answered anyway, so a press
#: that fell off this list gets what it would have got: a message.
UNANSWERED_PRESSES_KEPT = 64

#: Telegram's own words for an edit that changed nothing. That is success: the
#: message already says what it was asked to say (ADR 0021 §8).
NOT_MODIFIED = "message is not modified"

#: What `getUpdates` is passed to make it hand back the last pending update and
#: nothing else, which is how the backlog's far end is found in one call.
LAST_UPDATE = -1

#: How long `aclose` gives the reader to notice it was stopped. Short on purpose:
#: it covers the ordinary case, where the reader is between polls, and it is
#: never the thing that lets the process exit — the daemon flag is.
JOIN_SECONDS = 0.2


def split_message(text: str, *, limit: int = MESSAGE_LIMIT_UTF16_UNITS) -> tuple[str, ...]:
    """Cut one message into parts the API will accept, losing not one character.

    Truncation was considered and rejected for the messages that come this way:
    silently amputating the tail of a reply the user is meant to read is a worse
    failure than sending two messages. The cut prefers the last line break, then
    the last space, then falls where it must — and it walks code points, so a
    surrogate pair is never split down the middle.

    **A notice never comes this way** (ADR 0021 §5). A structured brief is laid
    out as one message by `layout.lay_out`, which cuts the folded original with
    a marker rather than splitting; this function is for everything else — a
    receipt, a control-plane answer, a delegated reply.
    """
    if utf16_length(text) <= limit:
        return (text,) if text else ()

    parts: list[str] = []
    rest = text
    while rest:
        if utf16_length(rest) <= limit:
            parts.append(rest)
            break
        hard = prefix_within(rest, limit)
        window = rest[:hard]
        boundary = max(window.rfind("\n"), window.rfind(" "))
        cut = boundary + 1 if boundary > 0 else hard
        parts.append(rest[:cut])
        rest = rest[cut:]
    return tuple(parts)


class TelegramCompanionChannel:
    """One bot, one chat, both ways. Implements `CompanionChannel` and `Connectable`."""

    def __init__(
        self, *, sink: EventSink | None, settings: TelegramSettings, transport: Transport
    ) -> None:
        self._sink = sink
        self._settings = settings
        self._transport = transport
        #: The next update to ask for. In memory only: it has value for the life
        #: of this process and none beyond it, and a cursor on disk would widen
        #: the durable state to something no restart should trust.
        self._offset: int | None = None
        #: Whether this process has already thrown the backlog away. Once, at
        #: first contact — never again on a mid-run reconnect.
        self._joined = False
        self._reader: threading.Thread | None = None
        #: How the reader is told to stop, and the only thing `aclose` waits on.
        self._stop = threading.Event()
        #: The loop the sink belongs to, learned at `connect`. The reader never
        #: touches anything of Bridge Core's directly — one hand-off, through
        #: `call_soon_threadsafe`, and nothing else is shared.
        self._loop: asyncio.AbstractEventLoop | None = None
        #: Presses handed up and not yet answered, oldest first. Written and
        #: read on the loop only — registered in `_surface`, consumed in `send`
        #: — so the reader thread never touches it.
        self._unanswered: dict[str, None] = {}

    # -- what the composition root opens and closes ------------------------

    async def connect(self) -> None:
        """Start listening. Idempotent, and never fails over an unreachable network."""
        if self._reader is not None:
            return
        self._loop = asyncio.get_running_loop()
        self._stop.clear()
        self._reader = threading.Thread(
            target=self._polling, name="telegram-companion-channel", daemon=True
        )
        self._reader.start()

    async def aclose(self) -> None:
        """Stop listening. Idempotent, and never waits out a poll that is still open.

        The stop is a signal and a **bounded** join, not a wait: a poll can be
        parked on the network for half a minute, and an engine shutting down
        must not be held there. What guarantees the process can still leave is
        the thread being a daemon, not this join finishing — the join exists so
        that in the ordinary case, where the reader is between polls, it is
        really gone before this returns.
        """
        reader, self._reader = self._reader, None
        self._stop.set()
        if reader is None:
            return
        reader.join(timeout=JOIN_SECONDS)

    # -- the seam ---------------------------------------------------------

    async def send(
        self,
        text: str,
        *,
        request_id: RequestId,
        origin: str = "",
        revises: tuple[str, ...] = (),
        notice: Notice | None = None,
    ) -> ChannelReceipt:
        """Push one message, in as many parts as the API's cap requires.

        One request id, one receipt, whatever the message was cut into. The
        classification is the whole point of this method:

        - every part landed → **DELIVERED**;
        - the first part failed, so nothing reached anyone → **FAILED**, with the
          layer that refused;
        - a part failed *after* an earlier one landed → **UNKNOWN**, naming how
          much arrived. Never FAILED: words did reach the user, and FAILED means
          a positive reason to believe they did not.

        The receipt names the id every landed part got, in order — an UNKNOWN
        one included, because those parts exist and can be replied to.

        A failure **stops the send**. A later part delivered on top of a missing
        one is a message with a hole in the middle, which reads as a different
        message rather than as a broken one.

        **A notice is one part, always** (ADR 0021 §5). When `notice` is given
        the structured brief is laid out here — plain text plus `entities`, the
        original cut with Core's marker where the cap demands — and `text` is
        not sent: it is the same words in the shape a surface with no layout
        prints. `split_message` is never reached by a notice.

        Three variations, all decided by the caller's arguments and none by
        anything this adapter infers: `revises` names messages to edit in place
        rather than send (`_revise`); an `origin` naming an unanswered press
        makes a short reply a toast (`_toast`) — never a notice, which clears
        the press and goes as a message; `notice` chooses the layout. Everything
        else — a chat's origin, an empty one — is an ordinary message to the one
        chat.

        Nothing is queued here. An unreachable network is a classified failure
        returned at once — the engine's loop is never held. Bridge Core decides
        whether a later outlet transition should reconcile the current Session
        state; this adapter never replays the notice object.
        """
        parts = _parts(text, notice)
        if revises:
            return await self._revise(parts, request_id=request_id, revises=revises)
        if not parts:
            return ChannelReceipt(
                request_id=request_id,
                outcome=Delivery.FAILED,
                reason="there were no words to send",
            )
        callback = self._press_to_answer(origin)
        if callback is not None and await self._toast(callback, text if notice is None else None):
            return ChannelReceipt(request_id=request_id, outcome=Delivery.DELIVERED)

        landed: list[str] = []
        for part in parts:
            try:
                result = await self._ask(
                    "sendMessage",
                    {"chat_id": self._settings.chat_id, **part},
                    timeout_seconds=self._settings.request_timeout_seconds,
                )
            except TelegramError as refused:
                if not landed:
                    return ChannelReceipt(
                        request_id=request_id, outcome=Delivery.FAILED, reason=refused.detail
                    )
                return ChannelReceipt(
                    request_id=request_id,
                    outcome=Delivery.UNKNOWN,
                    reason=(
                        f"{len(landed)} of {len(parts)} parts reached the chat, "
                        f"then {refused.detail}"
                    ),
                    message_ids=tuple(landed),
                )
            landed.append(_message_id_of(result))
        return ChannelReceipt(
            request_id=request_id, outcome=Delivery.DELIVERED, message_ids=tuple(landed)
        )

    async def _revise(
        self,
        parts: tuple[dict[str, object], ...],
        *,
        request_id: RequestId,
        revises: tuple[str, ...],
    ) -> ChannelReceipt:
        """Replace the content of messages sent earlier, one part per id (ADR 0021 §8).

        Pairwise and whole: the message is cut exactly as a fresh send would cut
        it, and it must fall into as many parts as there are messages to
        revise. A mismatch edits nothing and says so — a notice half-rewritten
        is a message with a hole in it. `editMessageText` keeps whatever the
        message already had that this call does not name, and a refusal that
        says the content is unchanged is the outcome that was wanted.

        A notice is laid out for an edit exactly as for a send, entities
        included, so a closed notice keeps its fold and its bold question and
        changes only the words Core changed. A notice is one part, so it can
        revise exactly one message. The inline markup drawn from the labels is
        #264's; the issue that edits a closed notice to `handled` (#266) is what
        calls this with one.
        """
        if len(parts) != len(revises):
            return ChannelReceipt(
                request_id=request_id,
                outcome=Delivery.FAILED,
                reason=(
                    f"{len(revises)} message(s) cannot be revised into {len(parts)} part(s); "
                    "nothing was edited"
                ),
            )
        edited: list[str] = []
        for message_id, part in zip(revises, parts, strict=True):
            try:
                await self._ask(
                    "editMessageText",
                    {
                        "chat_id": self._settings.chat_id,
                        "message_id": _message_id_on_the_wire(message_id),
                        **part,
                    },
                    timeout_seconds=self._settings.request_timeout_seconds,
                )
            except TelegramError as refused:
                if NOT_MODIFIED not in str(refused).casefold():
                    if not edited:
                        return ChannelReceipt(
                            request_id=request_id, outcome=Delivery.FAILED, reason=refused.detail
                        )
                    return ChannelReceipt(
                        request_id=request_id,
                        outcome=Delivery.UNKNOWN,
                        reason=(
                            f"{len(edited)} of {len(parts)} messages were revised, "
                            f"then {refused.detail}"
                        ),
                        message_ids=tuple(edited),
                    )
            edited.append(message_id)
        return ChannelReceipt(
            request_id=request_id, outcome=Delivery.DELIVERED, message_ids=tuple(edited)
        )

    def _press_to_answer(self, origin: str) -> str | None:
        """The callback id this reply answers, if it is a press still waiting for one.

        Consumed on the way out whatever happens next: a callback is answered
        once, so the second reply to the same press — and every reply to a
        press this process never saw — is an ordinary message. A press that
        fell past `UNANSWERED_PRESSES_KEPT` is one Telegram has also forgotten
        by then; nothing could answer it, and its reply goes as a message too.
        """
        if not origin.startswith(CALLBACK_ORIGIN):
            return None
        callback = origin[len(CALLBACK_ORIGIN) :]
        if callback not in self._unanswered:
            return None
        del self._unanswered[callback]
        return callback

    async def _toast(self, callback: str, text: str | None) -> bool:
        """Answer one press, and say whether the words were shown as the toast.

        **A held press is answered exactly once, whatever the words are.** When
        they fit a toast, they are the toast. When they do not — or when there
        are none to show, because the reply is a notice with a layout of its
        own — the callback is answered with no text — that is what clears the
        loading indicator the client draws on a pressed button — and the caller
        sends the reply as a message. A refusal either way is logged and nothing
        more: Telegram treats a refused callback as answered, so the reply still
        goes as a message and the user is never left with a spinning button.

        Core answering a press with *no* words is not a case here by design:
        the router fails closed and every inbound is answered
        (`core/bridge.py::_inbound_text`), and the only text `_reply` skips is
        empty, which no classification produces.
        """
        fits = text is not None and len(text) <= TOAST_LIMIT_CHARACTERS
        answer: dict[str, object] = {"callback_query_id": callback}
        if fits:
            answer["text"] = text
        try:
            await self._ask(
                "answerCallbackQuery",
                answer,
                timeout_seconds=self._settings.request_timeout_seconds,
            )
        except TelegramError as refused:
            _log.warning(
                "answering a press was refused, so the reply goes as a message: %s",
                refused.detail,
            )
            return False
        return fits

    async def verify(self) -> VerifyResult:
        """Prove reachability positively, or name the layer that stopped it.

        Both halves are asked, because they fail differently and an operator
        needs to know which: `getMe` proves the token, `getChat` proves this bot
        can actually reach the chat it is configured for. A valid token pointed
        at a chat the bot was never added to is precisely the outage that looks
        healthiest from the outside. Both are read-only.

        `loaded` is this implementation's module string whatever the answer is —
        something real *is* loaded even when its far side is unreachable, so this
        is never MANUAL. MANUAL belongs to the null implementation alone.
        """
        loaded = f"{type(self).__module__}:{type(self).__name__}"
        try:
            await self._ask("getMe", {}, timeout_seconds=self._settings.request_timeout_seconds)
            await self._ask(
                "getChat",
                {"chat_id": self._settings.chat_id},
                timeout_seconds=self._settings.request_timeout_seconds,
            )
        except TelegramError as refused:
            return VerifyResult(outcome=VerifyOutcome.FAIL, loaded=loaded, detail=refused.detail)
        return VerifyResult(
            outcome=VerifyOutcome.PASS,
            loaded=loaded,
            detail=f"the bot answers and chat {self._settings.chat_id} is reachable",
        )

    # -- the reader -------------------------------------------------------

    def _polling(self) -> None:
        """The reader thread's whole life. Nothing raised in here may end it.

        Everything in this method runs off the event loop, including the cursor
        it advances — which is why the cursor needs no lock: it is read and
        written by this thread alone. The one thing that crosses back is a piece
        of text, and it crosses the only way it may.
        """
        while not self._stop.is_set():
            try:
                if not self._joined:
                    self._skip_backlog()
                    self._joined = True
                updates = self._transport(
                    "getUpdates", self._poll(), timeout_seconds=self._patience()
                )
            except TelegramError as unreachable:
                _log.warning("the companion channel is not reachable: %s", unreachable.detail)
                self._stop.wait(self._settings.retry_seconds)
                continue
            except Exception:  # a reader that dies is a channel that went deaf silently
                _log.exception("the companion channel's reader raised")
                self._stop.wait(self._settings.retry_seconds)
                continue
            if self._stop.is_set():
                # A poll that was already in flight when `aclose` returned still
                # comes back with a batch. Handing it up would mean an adapter
                # that said it had stopped listening putting control-plane text
                # into Bridge Core afterwards, which is worse than losing it:
                # Telegram never had these acknowledged and re-serves them to
                # whatever listens next.
                return
            for update in updates or ():
                self._heard(update)

    def _skip_backlog(self) -> None:
        """Throw away whatever accumulated while this engine was not running.

        One call, not a drain loop: asking for the last update alone gives the
        far end of the backlog, and starting the cursor past it confirms every
        update before it in the same motion Telegram already uses for that.

        This happens once per process, at the first *successful* contact. A
        network blip mid-run does not bring it back: the engine was alive
        throughout and its state never reset, so what arrives afterwards is not
        stale in the way a message from before the start is.
        """
        pending = self._transport(
            "getUpdates",
            {"offset": LAST_UPDATE, "timeout": 0, "allowed_updates": ALLOWED_UPDATES},
            timeout_seconds=self._settings.request_timeout_seconds,
        )
        for update in pending or ():
            self._advance(update)
        if pending:
            _log.info(
                "discarded %d message(s) that arrived before this engine started", len(pending)
            )

    def _poll(self) -> dict[str, object]:
        """One long poll's request, carrying the cursor when there is one."""
        asked: dict[str, object] = {
            "timeout": int(self._settings.poll_timeout_seconds),
            "allowed_updates": ALLOWED_UPDATES,
        }
        if self._offset is not None:
            asked["offset"] = self._offset
        return asked

    def _patience(self) -> float:
        """How long the *transport* waits: the poll's own hang, plus one request's."""
        return self._settings.poll_timeout_seconds + self._settings.request_timeout_seconds

    def _heard(self, update: dict) -> None:
        """One update: move the cursor, then decide whether it is the user's."""
        self._advance(update)
        pressed = update.get("callback_query")
        if isinstance(pressed, dict):
            self._pressed(pressed)
            return
        message = update.get("message")
        if not isinstance(message, dict):
            return
        if not self._in_the_configured_chat(message):
            return
        text = message.get("text")
        self._hand_up(
            InboundText(
                text=text if isinstance(text, str) else "",
                origin=self._settings.chat_id,
                in_reply_to=_replied_to(message),
            )
        )

    def _pressed(self, query: dict) -> None:
        """A button press becomes the numeral it stood for, in a reply to its message.

        The origin carries the callback's id under a prefix only this adapter
        reads, so Bridge Core's reply to it can be shown as a toast (`send`).
        A press on a message this bot did not send has no `message` and is
        nothing this channel can answer, so it is dropped like any stranger.
        """
        message = query.get("message")
        if not isinstance(message, dict) or not self._in_the_configured_chat(message):
            return
        data, callback = query.get("data"), query.get("id")
        if (
            not isinstance(data, str)
            or not isinstance(callback, str | int)
            or isinstance(callback, bool)
        ):
            return
        self._hand_up(
            InboundText(
                text=data,
                origin=f"{CALLBACK_ORIGIN}{callback}",
                in_reply_to=_message_id_of(message),
            )
        )

    def _in_the_configured_chat(self, message: dict) -> bool:
        """Whether this message sits in the configured chat — and log the ones that do not.

        The chat is the test, not the sender: on the press path this is the
        bot's own notice, and it is in the user's chat that makes it answerable.

        Silence, deliberately: a refusal sent back would tell whoever is probing
        that this bot is alive and attended.
        """
        chat = message.get("chat")
        origin = str(chat.get("id", "")) if isinstance(chat, dict) else ""
        if origin == self._settings.chat_id:
            return True
        _log.warning("dropped an inbound message from %s, which is not this channel's chat", origin)
        return False

    def _hand_up(self, event: InboundText) -> None:
        """The one thing that crosses from the reader to the engine, the one legal way.

        A loop that has already closed is not an error worth raising on a
        thread nobody is watching: it means the engine went away while this poll
        was in flight, which is exactly the case the daemon flag exists for.
        """
        if self._sink is None or self._loop is None or self._stop.is_set():
            return
        try:
            self._loop.call_soon_threadsafe(self._surface, event)
        except RuntimeError:
            _log.debug("inbound text arrived after the engine stopped listening")

    def _surface(self, event: InboundText) -> None:
        """Emit, on the loop — and read the stop signal *there*, which is what makes it hold.

        The reader's own check above is an early exit and nothing more: between
        a check on one thread and a hand-off it schedules, `aclose` can run to
        completion, and the event would then reach Bridge Core after this
        adapter had said it stopped listening. Read here, the check is ordered
        against `aclose` itself — both run on this loop, so one of them is
        first and there is no gap between them to lose.
        """
        if self._sink is None or self._stop.is_set():
            return
        if event.origin.startswith(CALLBACK_ORIGIN):
            self._remember_press(event.origin[len(CALLBACK_ORIGIN) :])
        self._sink.emit(event)

    def _remember_press(self, callback: str) -> None:
        """Hold a press until Bridge Core answers it, forgetting the oldest past the cap."""
        self._unanswered[callback] = None
        while len(self._unanswered) > UNANSWERED_PRESSES_KEPT:
            del self._unanswered[next(iter(self._unanswered))]

    def _advance(self, update: dict) -> None:
        """Move the cursor past one update, whatever this adapter did with it."""
        update_id = update.get("update_id")
        if isinstance(update_id, int) and not isinstance(update_id, bool):
            self._offset = max(self._offset or 0, update_id + 1)

    async def _ask(self, method: str, payload: dict, *, timeout_seconds: float) -> object:
        """One Bot API call, on a thread of this adapter's own.

        Its own rather than `asyncio.to_thread`'s, for the reason in the module
        docstring: the default executor is joined by `asyncio.run` on the way
        out, so a request still in flight there holds the whole process past
        whatever grace it was given. A daemon thread is abandoned instead, and
        the caller awaiting this is cancelled with the rest of the shutdown.
        """
        loop = asyncio.get_running_loop()
        answer: asyncio.Future[object] = loop.create_future()

        def call() -> None:
            try:
                outcome: object = self._transport(method, payload, timeout_seconds=timeout_seconds)
            except BaseException as raised:  # noqa: BLE001 - handed back whole, judged there
                outcome = raised
            try:
                loop.call_soon_threadsafe(_settle, answer, outcome)
            except RuntimeError:
                pass  # the loop has gone; there is nobody left to tell

        threading.Thread(target=call, name=f"telegram-{method}", daemon=True).start()
        return await answer


def _parts(text: str, notice: Notice | None) -> tuple[dict[str, object], ...]:
    """What goes on the wire, as `sendMessage` bodies without the chat: one per part.

    A notice is laid out and is one part; anything else is the text, cut to the
    cap into as many parts as it needs (`split_message`).
    """
    if notice is not None:
        return (lay_out(notice).payload(),)
    return tuple({"text": part} for part in split_message(text))


def _message_id_of(message: object) -> str:
    """The provider's id of one message, as the opaque string the seam carries."""
    if isinstance(message, dict):
        message_id = message.get("message_id")
        if message_id is not None and not isinstance(message_id, bool):
            return str(message_id)
    return ""


def _replied_to(message: dict) -> str:
    """The id of the message this one answered, empty when it answered nothing."""
    return _message_id_of(message.get("reply_to_message"))


def _message_id_on_the_wire(message_id: str) -> int | str:
    """Give an id back to the API in the shape it came: its ids are integers."""
    return int(message_id) if message_id.lstrip("-").isdigit() else message_id


def _settle(answer: asyncio.Future[object], outcome: object) -> None:
    """Hand one call's result back, unless nobody is waiting for it any more.

    Runs on the event loop, put there by the worker. A future that was already
    cancelled is the ordinary shutdown case rather than an error: the engine let
    go of this call, and setting a result on it would raise where nothing is
    listening.
    """
    if answer.done():
        return
    if isinstance(outcome, BaseException):
        answer.set_exception(outcome)
    else:
        answer.set_result(outcome)
