"""One request, one Bridge Core verb, one reply.

This is the whole engine side of the control plane's meaning, and it is
deliberately thin: it validates a payload, calls the hub, renders the answer.
It holds **no policy and no state** — the same rule `bridgectl` is held to,
for the same reason. Anything here that started deciding would be a second
decision-maker beside the hub.

Every action calls a **hub verb**, never a pipeline inside it. Outsiders see one
Bridge Core (ADR 0001), so a surface that knew which of the five pipelines owned
which decision would be a surface that has to change when the hub rearranges
itself — and one that could reach a pipeline the hub would have guarded.

Telegram binding is the explicit #359 exception: configuration uses the existing
Telegram wire directly, without teaching a null Companion Channel about Telegram.

**ADR 0002 is honoured by omission.** Nothing in this file consults switch
state, and there is no branch that could. The reference implementation gated
seven actions — the Session roster and six more — behind the Duty Switch, so a user
away from the computer with Duty off could see nothing and do nothing; that
dispatch behaviour is dropped, not ported, and
`tests/test_control_plane_actions.py` proves every action still answers with
every switch off.

**A refusal keeps its identity.** Bridge Core's refusals map onto the closed
error set by type, and the message that travels is the refusal's own words —
surfaces render it verbatim rather than rephrasing it, so honest wording lives
in one place.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from gpt_voicecoding.adapters.companion_channel.telegram.api import TelegramBinding, TelegramError
from gpt_voicecoding.control_plane import payloads
from gpt_voicecoding.control_plane.payloads import InvalidPayload, NothingPending
from gpt_voicecoding.control_plane.progress_publication import (
    OversizeDecision,
    ProgressPublication,
)
from gpt_voicecoding.core.bridge import BridgeCore
from gpt_voicecoding.core.errors import (
    BridgeCoreError,
    StaleSessionError,
    UnknownRelayError,
    UnknownSessionError,
    UnknownSwitchError,
)
from gpt_voicecoding.seams.control_plane import Action, ErrorCode, Reader, Reply, Request

#: Every handler takes the reader as well as the payload, whether or not it
#: consults it (#302). A uniform signature keeps the dispatch below a single
#: call: a table where two entries were invoked differently from the other
#: nine would be a table that has to be read to be used.
Handler = Callable[[Mapping[str, Any], "Reader | None"], Awaitable[dict[str, Any]]]

#: Bridge Core's refusals, in the order they are tested — most specific first.
_CODES: tuple[tuple[type[BridgeCoreError], ErrorCode], ...] = (
    (UnknownSwitchError, ErrorCode.UNKNOWN_SWITCH),
    (StaleSessionError, ErrorCode.STALE_SESSION),
    (UnknownSessionError, ErrorCode.UNKNOWN_SESSION),
    (UnknownRelayError, ErrorCode.UNKNOWN_PENDING),
)


def code_for(refusal: BridgeCoreError) -> ErrorCode:
    """Which code a refusal travels under. Unmapped refusals still travel."""
    for kind, code in _CODES:
        if isinstance(refusal, kind):
            return code
    return ErrorCode.REFUSED


class ControlPlane:
    """The engine side of the control plane. Translation, never decision."""

    def __init__(
        self,
        core: BridgeCore,
        *,
        progress_publication: ProgressPublication | None = None,
        telegram_binding: TelegramBinding | None = None,
    ) -> None:
        self._core = core
        self._telegram_binding = telegram_binding
        self._progress_publication = progress_publication or ProgressPublication()
        self.handlers: dict[Action, Handler] = {
            Action.STATUS: self._status,
            Action.MODELS: self._models,
            Action.FORGET_CALL_AGENT: self._forget_call_agent,
            Action.BIND_TELEGRAM: self._bind_telegram,
            Action.SWITCH: self._switch,
            Action.BRIEF: self._brief,
            Action.HISTORY: self._history,
            Action.LIVE: self._live,
            Action.RELAY: self._relay,
            Action.APPROVE: self._approve,
            Action.VERIFY: self._verify,
            Action.SESSIONS: self._sessions,
            Action.CONFIG: self._config,
            Action.ASSISTANT: self._assistant,
        }

    @property
    def commands(self) -> frozenset[str]:
        """The command words this engine answers to, for the inbound-text grammar.

        One command set: `/status` on the Companion Channel and `bridgectl
        status` are the same action, dispatched here, with no second table to
        drift from this one.
        """
        return frozenset(str(action) for action in self.handlers)

    async def handle(self, request: Request) -> Reply:
        """Answer exactly one request. Never raises; a refusal is a reply."""
        handler = self.handlers.get(request.action)
        if handler is None:  # a closed action set with a hole in it
            return self._progress_publication.final(
                Reply.refused(
                    request.action,
                    ErrorCode.UNKNOWN_ACTION,
                    f"this engine has no handler for {request.action}",
                )
            )
        try:
            reply = Reply.answered(request.action, await handler(request.payload, request.reader))
        except InvalidPayload as unusable:
            reply = Reply.refused(request.action, ErrorCode.INVALID_PAYLOAD, str(unusable))
        except OversizeDecision as unspeakable:
            # Whole or refused, never partial: a decision that cannot cross
            # the call is refused in the publication's own fixed words rather
            # than handed over with its options missing (#302, ADR 0016).
            reply = Reply.refused(request.action, ErrorCode.REFUSED, str(unspeakable))
        except NothingPending as gone:
            reply = Reply.refused(request.action, ErrorCode.UNKNOWN_PENDING, str(gone))
        except BridgeCoreError as refusal:
            reply = Reply.refused(request.action, code_for(refusal), str(refusal))
        except TelegramError as refusal:
            if request.action is not Action.BIND_TELEGRAM:
                raise
            reply = Reply.refused(
                request.action, ErrorCode(f"telegram_{refusal.layer}"), refusal.detail
            )
        return self._progress_publication.final(reply)

    # ------------------------------------------------------------------
    # The actions. Each one is a payload read, a hub call, and a render.
    # ------------------------------------------------------------------

    async def _status(
        self, payload: Mapping[str, Any], reader: Reader | None = None
    ) -> dict[str, Any]:
        return self._progress_publication.status_document(self._core.status())

    async def _bind_telegram(
        self, payload: Mapping[str, Any], reader: Reader | None = None
    ) -> dict[str, Any]:
        if self._telegram_binding is None:
            raise BridgeCoreError("Telegram binding is not configured on this control plane")
        token = payloads.read_text(payload, "token") if "token" in payload else None
        cancel = payloads.read_flag(payload, "cancel") if "cancel" in payload else False
        return await self._telegram_binding.step(token=token, cancel=cancel)

    async def _forget_call_agent(
        self, payload: Mapping[str, Any], reader: Reader | None = None
    ) -> dict[str, Any]:
        await self._core.forget_call_agent()
        return {}

    async def _models(
        self, payload: Mapping[str, Any], reader: Reader | None = None
    ) -> dict[str, Any]:
        return {
            "models": [
                {"model": item.model, "efforts": list(item.efforts)}
                for item in await self._core.models()
            ]
        }

    async def _brief(
        self, payload: Mapping[str, Any], reader: Reader | None = None
    ) -> dict[str, Any]:
        """The Roster Brief, or one Session Brief with Detail for one address.

        One action with an optional address, mirroring the one hub verb behind
        it. The rendered `text` travels beside the structure because
        `Briefing.text` is the only renderer there is (#166 B6): a surface that
        composed its own line from the fields would be a second voice describing
        one Session, which is the thing Briefing exists to end.

        **What may be carried is the publication's to decide, not this file's**
        (#302). Fitting a brief to a wire used to be done here; it moved into
        `ProgressPublication` so that one owner measures, omits and finally
        checks every reply on this seam. This method reads a payload, calls the
        hub and renders — which is all it was ever meant to do.
        """
        brief = await self._core.brief(payloads.read_optional_target(payload))
        return self._progress_publication.brief_document(brief, reader=reader)

    async def _history(
        self, payload: Mapping[str, Any], reader: Reader | None = None
    ) -> dict[str, Any]:
        """One page of what an exact Session said and was told (#171).

        A separate read, published on its own shape rather than as a roster row:
        a page is not a roster fact, and the row's `progress` summary keeps
        saying what it said. The count comes from `[policy]
        history_page_entries` and never from the caller, so no surface can ask
        this engine for a page it did not size.
        """
        page = await self._core.history(
            payloads.read_target(payload),
            before=payloads.read_optional_ordinal(payload, "before"),
        )
        return self._progress_publication.history_document(page, reader=reader)

    async def _switch(
        self, payload: Mapping[str, Any], reader: Reader | None = None
    ) -> dict[str, Any]:
        name = payloads.read_text(payload, "name")
        on = payloads.read_flag(payload, "on")
        previous = await self._core.flip_switch(name, on)
        return {"name": name, "on": on, "previous": previous}

    async def _live(
        self, payload: Mapping[str, Any], reader: Reader | None = None
    ) -> dict[str, Any]:
        """The Live Toggle. One action, and every surface calls this one."""
        if "cancel_dial" in payload:
            return {
                "cancelled": await self._core.cancel_dial(
                    payloads.read_text(payload, "cancel_dial")
                )
            }
        attempt_id = payloads.read_text(payload, "attempt_id") if "attempt_id" in payload else None
        return payloads.call_document(await self._core.live_toggle(attempt_id=attempt_id))

    async def _relay(
        self, payload: Mapping[str, Any], reader: Reader | None = None
    ) -> dict[str, Any]:
        """An Answer Relay: the user's own words, carrying the user's authority.

        There is deliberately no action for system-authored words: a surface
        asking for one would be a surface claiming to be the system.
        """
        outcome = await self._core.relay(
            payloads.read_target(payload),
            payloads.read_text(payload, "text"),
            route=payloads.read_route(payload),
        )
        return payloads.relay_document(outcome)

    async def _approve(
        self, payload: Mapping[str, Any], reader: Reader | None = None
    ) -> dict[str, Any]:
        """An Approval Relay: the user's verdict on one pending permission.

        Two refusals, and they read differently because the user acts on them
        differently. Nothing on the roster carrying the handle means the hook
        ended — the dialog went back to the screen, and that is where it is
        answered now. A Child Process carrying it is refused in Bridge Core's
        own words (`ChildSessionError`), which say why it is never spoken to.
        """
        approval_id = payloads.read_text(payload, "approval_id")
        verdict = payloads.read_verdict(payload)
        outcome = await self._core.answer_approval(approval_id, verdict)
        if outcome is None:
            raise NothingPending(
                f"no live Session is waiting under {approval_id!r} — the dialog was answered "
                "or its hook ended; if it is still on screen, answer it there"
            )
        return payloads.approval_document(approval_id, verdict, outcome)

    async def _verify(
        self, payload: Mapping[str, Any], reader: Reader | None = None
    ) -> dict[str, Any]:
        return payloads.verification_document(await self._core.verify())

    async def _sessions(
        self, payload: Mapping[str, Any], reader: Reader | None = None
    ) -> dict[str, Any]:
        """The Session list as a screen: Briefing's roster text and its labels (ADR 0021 §6).

        The text is `brief`'s own roster rendering — the one renderer there is —
        and `options` are the labels a surface that draws choices draws, one
        per live Session, in row order. A surface that draws none prints the
        text and ignores them; nothing is lost, because the rows are the text.
        """
        return payloads.screen_document(await self._core.sessions_screen())

    async def _config(
        self, payload: Mapping[str, Any], reader: Reader | None = None
    ) -> dict[str, Any]:
        """The configuration screen: `switch`, `verify` and `live` as labels."""
        return payloads.screen_document(self._core.config_screen())

    async def _assistant(
        self, payload: Mapping[str, Any], reader: Reader | None = None
    ) -> dict[str, Any]:
        """Open an Assistant Conversation and answer with its opening line (ADR 0021 §7).

        The same screen the Companion Channel's menu opens, in the same words.
        A surface reached through here registers no Anchor — only a message
        actually sent to the user can be replied to — so `bridgectl assistant`
        opens a thread the chat cannot continue. That is the shape `sessions`
        and `config` already have: the screen is the answer, and the row belongs
        to the send.
        """
        return payloads.screen_document(await self._core.open_assistant())
