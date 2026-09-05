"""The Companion Channel seam — reaching the user when no Live Call is up.

Verbs Bridge Core calls: `send(message)` and `verify` (liveness — ADR 0003).

Events raised upward: inbound user text, **unclassified**. Deciding whether
inbound text is a control-plane command, an Answer Relay, or a delegation is
Bridge Core's job and never the channel's — which is why the event is named for
what it is (text that arrived) and not for what it might mean, and why it has no
field an adapter could use to volunteer an opinion.

**The adapter reports facts about a message, never an opinion about meaning**
(ADR 0021 §4). Which message the user's text answered (`in_reply_to`), where it
came from (`origin`) and which provider ids a push landed under
(`ChannelReceipt.message_ids`) are things the adapter can *see*; what any of them
means — which Session a reply was for, whether a numeral picks an option — is
read by Bridge Core against its own tables. Every one of them is an opaque
string Core matches by equality and never parses; a channel with no reply
concept emits empty and gets Core's default rule.

The null implementation is a real implementation. It reports the empty module
string from `verify` and returns a positive non-delivery from `send` — a
`ChannelReceipt` with no ids — never something Bridge Core could mistake for
delivery. It never emits.

Adapters: Telegram is the generic public one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from gpt_voicecoding.seams.delivery import DeliveryReceipt
from gpt_voicecoding.seams.events import Event
from gpt_voicecoding.seams.identity import RequestId
from gpt_voicecoding.seams.verify import VerifyResult


@dataclass(frozen=True, slots=True)
class InboundText(Event):
    """Text the user sent. What it *means* is Bridge Core's to decide.

    `origin` is the adapter's own opaque reference to where it came from — a
    chat id, a socket path. Bridge Core does not parse it; it exists so a reply
    can go back the way the text came, echoed into `send(origin=...)`.

    `in_reply_to` is the adapter's opaque id of the message this text answered,
    and empty when it answered nothing. Of the same nature as `origin`: a fact
    the adapter saw, matched by Core by string equality against the ids earlier
    `ChannelReceipt`s reported. An adapter with no reply concept leaves it empty.
    """

    text: str
    origin: str = ""
    in_reply_to: str = ""


@dataclass(frozen=True, slots=True)
class ChannelReceipt(DeliveryReceipt):
    """One push's classification, plus the provider ids of every part that landed.

    A `DeliveryReceipt` first: every existing `send` site keeps reading
    `is_delivered`. `message_ids` is in sending order, and an UNKNOWN receipt from
    a split send that failed after a part landed still lists the parts that did —
    those messages exist and the user can reply to them. Empty when nothing
    landed, and when what was sent is not a message (the null channel, a toast).
    """

    message_ids: tuple[str, ...] = ()


#: The closed set of events this seam raises. Nothing else may appear.
CompanionChannelEvent = InboundText


@runtime_checkable
class CompanionChannel(Protocol):
    """Text reach when there is no call. Mechanism only — routing policy is Core's."""

    async def send(
        self,
        text: str,
        *,
        request_id: RequestId,
        origin: str = "",
        revises: tuple[str, ...] = (),
    ) -> ChannelReceipt:
        """Push one message to the user, and say which ids it landed under.

        `origin` is the inbound event's `origin`, echoed when this is a reply to
        it; empty for an unbidden push. `revises` is the ids from an earlier
        receipt: non-empty means "replace those messages' content with this
        text" rather than send a new one. An adapter that cannot edit ignores
        it and reports as it always did.
        """
        ...

    async def verify(self) -> VerifyResult:
        """Report which implementation this is and whether its far side answers."""
        ...
