"""Relay queueing against the Reply Window — the hub's, never an adapter's.

The locked behaviour and the reason for it: unsolicited user text **queues until
the Reply Window is open**. That is not politeness. Delivering the user's words
mid-turn without being asked gets them framed as untrusted and refused —
verified live — so waiting for the window is what makes them arrive with the
user's authority intact. Adapters deliver; they never queue, because a queue
inside an adapter would be a second ledger and a second policy.

Three rules hang off that, all of them here:

- **The receipt is a grade and a reason, and the sentence for it is held once,
  here.** The verb answers with `RelayOutcome`: where the words are, what the
  last attempt proved as the seam's own `DeliveryReceipt` — or nothing, when
  none was made — one `RelayReason` code, and whether the route carried the
  user's authority. Seven English sentences used to live here and be rendered
  verbatim by whatever surface asked, which made Bridge Core the author of words
  the user hears. The Voice re-renders whatever it is handed (#175), so those
  sentences were a second renderer for words the model rewrites anyway; they are
  the Voice's rule now, in the generated instructions. The Companion Channel is
  a surface that prints rather than re-renders, and showing it the three codes
  was refused as the log feel the requirements page rejects (ADR 0021,
  Receipts); so `receipt_sentence` renders the same facts `receipt_line` prints
  into one sentence, **once, beside the codes**, with ADR 0013's clause when the
  words went without the user's authority. `bridgectl relay` keeps the codes.
  Legacy had no scripted acknowledgement to port
  (`legacy@1d32845:skill/SKILL.md:63-68` covers failure only) — **dropped**; its
  synchronous relay reply (`legacy@1d32845:bridge/__main__.py:656-661,683-780`)
  is **adapted** into this structured answer.
- **There is no ceiling.** Queued words wait for the Session's next turn or for
  its end, and nothing else takes them out. A ten-minute wall clock used to,
  and on 2026-09-09 it dropped a Relay ten seconds before the turn it was
  waiting for and said nothing (#321) — a limit of this system's own, enforced
  against words the user had not withdrawn. The user withdraws them by saying
  something else; that is the only clock there is.
- **Route follows the user's explicit intent.** Deliver (between turns) versus
  supplement (mid-turn) is what the user asked for, never what the Session
  happens to be doing — the same "busy" carries both "add this now" and "this
  can wait". An adapter that honestly lacks SUPPLEMENT does not get guessed
  around: the words queue as a DELIVER, which is this module's decision to make
  and not the adapter's.

A non-delivery keeps the words. FAILED, HELD and UNKNOWN all mean "not proven
delivered", so the entry waits — and it waits for the *next Reply Window
transition*, never retrying off the back of its own failure.

**But waiting and being sent again are not the same thing** (P9). A second
attempt is permitted only where non-delivery was **proven**, which is the
reference implementation's own settlement rule
(`legacy@1d32845:bridge/delivery.py:28-75`;
`legacy@1d32845:bridge/coordinator.py:1075-1109`;
`legacy@1d32845:bridge/store.py:964-1035,3394-3555,3653-3874`) — **ported**, with
its durable ledger, its confirmed-request history and its crash enquiry left
behind (#61 R1). So:

- **DELIVERED completes.** The entry leaves the queue and nothing retries it.
- **FAILED may go again** at the next window (#61 R2). Nothing arrived, so
  nothing can arrive twice.
- **UNKNOWN never goes again on this system's own authority.** It is kept, as
  duplicate-risk information, and the user is told plainly that it may already
  have landed — a second attempt is theirs to authorise, by saying the words
  again. #71 makes this concrete: on the Claude inbox route an accepted socket
  write proves nothing, so most UNKNOWNs there are Relays that *did* arrive.
- **HELD never goes again either.** It is parked in front of a person on the far
  side and will settle on its own; sending it a second time is how one decision
  becomes two identical messages waiting for the same human.

**And the receipt itself is a reaction on the user's own message** (ADR 0021,
amended 2026-09-09). A sentence is a message of its own in the chat for news the
user already expects, so the ordinary outcomes are worded in an emoji instead
and only three carry a sentence: the two tables below say which is which, beside
the sentences, because what a receipt *is* is one decision and it is this
module's. Putting the emoji on the message is the surface's act
(`seams/companion_channel.py::CompanionChannel.react`) and which message is the
hub's to remember; the words and the emoji are chosen here.

That is why an entry that has been attempted carries the grade that attempt
produced, and one that has not carries `None`. Re-sending the user's own words
on an UNKNOWN grade is how the reference implementation produced duplicates
before it learned the rule; spelling "nothing was attempted" `UNKNOWN` would
bring the duplicates back by making the two indistinguishable.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass

from gpt_voicecoding.core.briefing import brief_state
from gpt_voicecoding.core.clock import Clock, default_clock
from gpt_voicecoding.core.lifecycle import Lifecycle, RelayAuthority, RelayReason
from gpt_voicecoding.core.relay_queue import PendingRelay, RelayKind, RelayQueue
from gpt_voicecoding.core.sessions import SessionRegistry
from gpt_voicecoding.seams.agent import (
    AgentAdapter,
    RelayRoute,
    ReplyWindow,
    SessionState,
    WaitingKind,
    derive_reply_window,
)
from gpt_voicecoding.seams.companion_channel import BriefState
from gpt_voicecoding.seams.delivery import Delivery, DeliveryReceipt
from gpt_voicecoding.seams.identity import AgentKind, RequestId, SessionTarget, new_request_id

_log = logging.getLogger(__name__)


#: Which code each grade earns while a Relay is still in play. Total over the
#: four grades and the absent attempt, and a function: one grade never produces
#: two codes. The terminal codes are not in here because they are facts about
#: what happened *here*, not about what an attempt proved.
_REASON_BY_GRADE: Mapping[Delivery | None, RelayReason] = {
    None: RelayReason.AWAITING_REPLY_WINDOW,
    Delivery.FAILED: RelayReason.AWAITING_REPLY_WINDOW,
    Delivery.UNKNOWN: RelayReason.DUPLICATE_RISK,
    Delivery.HELD: RelayReason.HELD_FAR_SIDE,
    Delivery.DELIVERED: RelayReason.DELIVERED,
}

#: What the grade field says when there is no attempt to grade. Not `unknown`:
#: that is a positive observation, and this is the absence of one.
NO_GRADE = "none"


def reason_for(receipt: DeliveryReceipt | None) -> RelayReason:
    """The code an attempt earns, or the one that says nothing was attempted."""
    return _REASON_BY_GRADE[None if receipt is None else receipt.outcome]


def receipt_line(*, state: str, grade: str, reason: str) -> str:
    """The receipt as one line of codes — the one format every surface prints.

    Three facts and no sentence. One place, so the CLI and the Companion Channel
    cannot drift into two ways of saying the same receipt, and so a field a
    harness parses is named the same on both.

    The attempt's own evidence (`DeliveryReceipt.reason`) is deliberately absent:
    it travels on the wire and into the log, where a defect is diagnosed, and it
    is the adapter's words rather than anything the user asked for.
    """
    return f"state={state} grade={grade} reason={reason}"


#: ADR 0013's clause, for a receipt on a route that carried the words and not
#: the user's say-so: the Session may act on them or not, and the user is owed
#: knowing that "arrived" is not "accepted as yours".
NO_AUTHORITY_CLAUSE = "The Session may not treat this as your own confirmation."

#: One sentence per reason code — the same three facts `receipt_line` prints,
#: for a surface that shows the user words rather than a line a harness parses.
#: Total over the codes, like `_REASON_BY_GRADE` is over the grades.
_RECEIPT_WORDING: Mapping[RelayReason, str] = {
    RelayReason.DELIVERED: "Your words arrived.",
    RelayReason.AWAITING_REPLY_WINDOW: (
        "Your words are waiting, and go in when the Session next takes a turn."
    ),
    RelayReason.DUPLICATE_RISK: (
        "Nobody can tell whether your words arrived; they are kept and not sent again — "
        "say them again if you want them to go."
    ),
    RelayReason.HELD_FAR_SIDE: (
        "Your words are held on the far side, in front of a person, and will settle there."
    ),
    RelayReason.SESSION_ENDED: "That Session ended before your words could go.",
    RelayReason.QUESTION_UNANSWERABLE: (
        "That question can no longer be answered from here; answer it at the terminal."
    ),
}

#: The one place the grade changes the sentence: words that wait after an
#: attempt **proved** they did not arrive are not words that never went.
_FAILED_AND_WAITING = "The attempt did not arrive; your words wait for the Session's next turn."

#: The emoji each reason wears on the user's own message, or `None` for a reason
#: that wears none. Total over the codes, like the wording above: a receipt is a
#: reaction first, and a reason with no entry here would be a Relay whose
#: standing the user cannot see. `AWAITING_REPLY_WINDOW` covers the
#: failed-attempt-still-waiting case too — the sentence differs there and the
#: standing does not, because in both the words are still going.
#:
#: Spelt in code points rather than pasted, because the surface accepts only the
#: emoji on its own published list and one of these carries a zero-width joiner
#: that an editor or a copy can quietly drop (#321).
_RECEIPT_REACTION: Mapping[RelayReason, str | None] = {
    RelayReason.DELIVERED: "\N{OK HAND SIGN}",
    RelayReason.AWAITING_REPLY_WINDOW: "\N{MAN}\N{ZERO WIDTH JOINER}\N{PERSONAL COMPUTER}",
    RelayReason.HELD_FAR_SIDE: "\N{HEAR-NO-EVIL MONKEY}",
    RelayReason.DUPLICATE_RISK: None,
    RelayReason.QUESTION_UNANSWERABLE: None,
    RelayReason.SESSION_ENDED: "\N{GHOST}",
}

#: The three outcomes whose sentence is sent on its own, whatever the reaction
#: did. News the user must act on: the words are in front of a person, the
#: question can no longer be answered from here, or nobody can tell whether they
#: arrived. Every other reason's sentence is the fallback for a reaction that
#: failed, and is sent only then.
_SENTENCE_ON_ITS_OWN = frozenset(
    {
        RelayReason.HELD_FAR_SIDE,
        RelayReason.QUESTION_UNANSWERABLE,
        RelayReason.DUPLICATE_RISK,
    }
)


def reaction_for(reason: RelayReason) -> str | None:
    """The emoji this standing wears, or `None` for a standing that wears none."""
    return _RECEIPT_REACTION[reason]


def sentence_stands_alone(reason: RelayReason) -> bool:
    """Whether this reason's sentence is sent whatever the reaction did."""
    return reason in _SENTENCE_ON_ITS_OWN


#: The codes under which the words reached, or may yet reach, the Session — the
#: only ones where saying what the Session may make of them means anything.
_WORDS_MAY_LAND = frozenset(
    {
        RelayReason.DELIVERED,
        RelayReason.AWAITING_REPLY_WINDOW,
        RelayReason.DUPLICATE_RISK,
        RelayReason.HELD_FAR_SIDE,
    }
)


def receipt_sentence(outcome: RelayOutcome) -> str:
    """The receipt as one sentence — the three facts of `receipt_line`, worded once.

    For the surface that prints what it is handed (the Companion Channel, ADR
    0021 Receipts). The reason chooses the sentence, the grade changes it in the
    one case where an attempt proved something the code alone does not say, and
    the clause is added on ADR 0013's test: the words reached, or may reach, the
    Session **and** answered a question it merely said — plain text into an
    inbox, on a Session whose turn ended asking, with no held hook and no
    verdict. Words for a Session that asked nothing get no clause: there is no
    answer for the user to mistake for their own. Nothing is said about
    authority where nothing arrived and nothing will.
    """
    if (
        outcome.reason is RelayReason.AWAITING_REPLY_WINDOW
        and outcome.receipt is not None
        and outcome.receipt.outcome is Delivery.FAILED
    ):
        sentence = _FAILED_AND_WAITING
    else:
        sentence = _RECEIPT_WORDING[outcome.reason]
    if (
        outcome.reason in _WORDS_MAY_LAND
        and outcome.authority is RelayAuthority.WORDS_ON_A_QUESTION
    ):
        return f"{sentence} {NO_AUTHORITY_CLAUSE}"
    return sentence


def may_be_retried(receipt: DeliveryReceipt | None) -> bool:
    """Whether a waiting Relay may go again on this system's own authority (P9).

    Two cases and no others: nothing was attempted, so nothing can arrive twice;
    or an attempt **proved** it did not arrive. `UNKNOWN` and `HELD` are both
    "it may have", and this system does not gamble the user's own words on a may.
    """
    return receipt is None or receipt.outcome is Delivery.FAILED


@dataclass(frozen=True, slots=True)
class RelayOutcome:
    """Where one Relay stands: the receipt, and the whole of it.

    Four facts and no sentence. `state` is where the words are, `receipt` is
    what the last attempt proved — the seam's own value, evidence included, or
    `None` when nothing has been attempted — `reason` is the one code that says
    why it stands there, and `authority` is what the route made of them: the
    user's own answer, or words — on a question, or not (ADR 0013 §3).

    **"Not attempted" is the absent attempt, never `UNKNOWN`.** The two are the
    difference between "it may already have arrived" and "it never left this
    process", which is the whole of P9's settlement rule; a `None` grade spelt
    `UNKNOWN` would bring back the duplicates that rule exists to prevent.
    """

    request_id: RequestId
    target: SessionTarget
    state: Lifecycle
    route: RelayRoute
    #: Why the Relay stands where it does. One code, always present.
    reason: RelayReason
    #: The last attempt, whole, or `None` when nothing was attempted.
    receipt: DeliveryReceipt | None = None
    #: What the route made of the words (`RelayAuthority`): the user's own, or
    #: a peer's message — and if the latter, whether there was a question they
    #: will be heard as answering. A surface that reports arrival owes the user
    #: that last difference (`receipt_sentence`).
    authority: RelayAuthority = RelayAuthority.WORDS
    #: The channel's opaque id of the user's own message these words came from,
    #: empty when they came from anywhere else — a button, the CLI, the Voice.
    #: What the receipt's reaction is put on, carried on the outcome because
    #: every settlement re-renders it and the queue row is gone by the last one.
    message_id: str = ""

    def __post_init__(self) -> None:
        if (self.state is Lifecycle.DELIVERED) is not (
            self.receipt is not None and self.receipt.is_delivered
        ):
            raise ValueError(
                "a Relay is DELIVERED exactly when an attempt proved it: "
                f"{self.state} beside {self.receipt}"
            )

    @property
    def grade(self) -> str:
        """The attempt's grade as a surface prints it, or `none` when there was none."""
        return NO_GRADE if self.receipt is None else str(self.receipt.outcome)

    @property
    def line(self) -> str:
        """This receipt in the one format every surface prints."""
        return receipt_line(state=str(self.state), grade=self.grade, reason=str(self.reason))


class RelayPipeline:
    """Carries the user's own words in, or holds them until they can go."""

    def __init__(
        self,
        *,
        agents: Mapping[AgentKind, AgentAdapter],
        sessions: SessionRegistry,
        relays: RelayQueue,
        clock: Clock = default_clock,
    ) -> None:
        self._agents = dict(agents)
        self._sessions = sessions
        self._relays = relays
        self._clock = clock

    async def relay(
        self,
        target: SessionTarget,
        text: str,
        *,
        route: RelayRoute = RelayRoute.DELIVER,
        request_id: RequestId | None = None,
        message_id: str = "",
    ) -> RelayOutcome:
        """Take the user's words for one Session. Delivers now, or queues them.

        Fails closed on the target: an unknown or stale identity raises rather
        than queueing words for a Session that will never take them.

        `message_id` is the channel's id of the message the user typed these
        words in, opaque here and carried onto every outcome this Relay ever
        produces, because the receipt for them is a reaction on that message
        (ADR 0021). Empty from every caller that has no message — the CLI, the
        Voice, a button press — and those receipts stay sentences.
        """
        session = self._sessions.resolve(target)
        adapter = self._adapter(target)
        chosen = self._honest_route(adapter, route)
        rid = request_id or new_request_id()

        try:
            question_answerable = adapter.question_answerable(target)
        except Exception:  # noqa: BLE001 - a level query fails closed, never the Relay call
            _log.exception("the %s lane could not report its question route", target.agent)
            question_answerable = False
        window = derive_reply_window(
            session.state,
            session.waiting_for,
            session.child,
            question_answerable=question_answerable,
        )
        if (
            chosen is RelayRoute.DELIVER
            and session.state is SessionState.WAITING
            and session.waiting_for.kind is WaitingKind.QUESTION
            and not question_answerable
        ):
            # Terminal before the wire, and therefore **ungraded**: nothing was
            # attempted, so there is no attempt to classify. The code is the
            # whole answer — where the question can still be answered is the
            # Voice's sentence to compose, not this module's (#175).
            _log.info("relay %s refused: %s is no longer answerable from here", rid, target)
            return RelayOutcome(
                request_id=rid,
                target=target,
                state=Lifecycle.REPORTED_FAILED,
                route=chosen,
                reason=RelayReason.QUESTION_UNANSWERABLE,
                message_id=message_id,
            )
        may_go_now = chosen is RelayRoute.SUPPLEMENT or window is ReplyWindow.OPEN
        # ADR 0015's route, and the only one here that carries the user's own
        # say-so: the Session asked through a hook the lane still holds, and the
        # words go into that hook now. Mid-turn words and words for an idle
        # Session go over the inbox as a peer's message (ADR 0013 §3) — and
        # whether that Session had *asked* is Briefing's reading of the row,
        # because a Codex turn that ended on a question records no wait.
        held = (
            chosen is RelayRoute.DELIVER
            and session.waiting_for.kind is WaitingKind.QUESTION
            and question_answerable
            and may_go_now
        )
        if held:
            authority = RelayAuthority.AS_THE_USER
        elif brief_state(session) is BriefState.DECISION:
            authority = RelayAuthority.WORDS_ON_A_QUESTION
        else:
            authority = RelayAuthority.WORDS
        if may_go_now:
            attempt = await adapter.answer_relay(target, text, request_id=rid, route=chosen)
            if attempt.is_delivered:
                return RelayOutcome(
                    request_id=rid,
                    target=target,
                    state=Lifecycle.DELIVERED,
                    route=chosen,
                    reason=RelayReason.DELIVERED,
                    receipt=attempt,
                    authority=authority,
                    message_id=message_id,
                )
            _log.info(
                "relay %s not proven delivered (%s: %s); it waits",
                rid,
                attempt.outcome,
                attempt.reason,
            )
            receipt: DeliveryReceipt | None = attempt
        else:
            # Nothing went on the wire, so there is no attempt to grade and no
            # duplicate to risk. `None` is that fact, and it is what makes this
            # entry retriable where an attempted-but-unproven one is not.
            receipt = None

        # Anything that did not prove delivery waits for the next window, and a
        # SUPPLEMENT that could not go mid-turn waits as an ordinary DELIVER.
        self._enqueue(
            rid, target, text, receipt=receipt, message_id=message_id, authority=authority
        )
        return RelayOutcome(
            request_id=rid,
            target=target,
            state=Lifecycle.RETAINED,
            route=RelayRoute.DELIVER,
            reason=reason_for(receipt),
            receipt=receipt,
            authority=authority,
            message_id=message_id,
        )

    async def reply_window_opened(self, target: SessionTarget) -> tuple[RelayOutcome, ...]:
        """The Session will take a user turn. Flush what may still be sent to it.

        This is the *only* retry trigger. Nothing here fires off the back of a
        failed attempt, so a Session that keeps refusing cannot be hammered with
        the same words.

        **And it flushes only what `may_be_retried` allows** (P9). An entry whose
        attempt proved nothing either way is passed over, every time this fires,
        for as long as it is held: the window opening is news about the Session,
        not evidence that the earlier attempt failed. It leaves on a late
        receipt, on the Session's end, or when the user says the words again.
        """
        adapter = self._agents.get(target.agent)
        if adapter is None:
            return ()

        flushed: list[RelayOutcome] = []
        for waiting in self._waiting_answers(target):
            if not may_be_retried(waiting.receipt):
                _log.info(
                    "relay %s is held rather than retried: an attempt graded %s may already "
                    "have arrived, and a second one would duplicate the user's words",
                    waiting.request_id,
                    None if waiting.receipt is None else waiting.receipt.outcome,
                )
                continue
            receipt = await adapter.answer_relay(
                target, waiting.text, request_id=waiting.request_id, route=waiting.route
            )
            if not receipt.is_delivered:
                _log.info(
                    "relay %s not proven delivered on Reply Window retry (%s: %s); it waits",
                    waiting.request_id,
                    receipt.outcome,
                    receipt.reason,
                )
            self._relays.classify(waiting.request_id, receipt)
            flushed.append(
                RelayOutcome(
                    request_id=waiting.request_id,
                    target=target,
                    state=(Lifecycle.DELIVERED if receipt.is_delivered else Lifecycle.RETAINED),
                    route=waiting.route,
                    reason=reason_for(receipt),
                    receipt=receipt,
                    message_id=waiting.message_id,
                    # What the route made of the words when they were taken
                    # (ADR 0013 §3). A receipt sent minutes later owes the user
                    # the same clause as one sent at once, and re-deriving it
                    # here would read a Session that has moved on since.
                    authority=waiting.authority,
                )
            )
        return tuple(flushed)

    def session_ended(self, target: SessionTarget) -> tuple[RelayOutcome, ...]:
        """That Session is gone. Words still waiting for it can never arrive."""
        dropped = [
            waiting
            for waiting in self._relays.pending_for(target)
            if waiting.kind is RelayKind.ANSWER
        ]
        return tuple(self._report_failed(waiting, RelayReason.SESSION_ENDED) for waiting in dropped)

    def waiting_for(self, target: SessionTarget) -> tuple[PendingRelay, ...]:
        """The user's words still queued for one Session, oldest first."""
        return self._waiting_answers(target)

    def _adapter(self, target: SessionTarget) -> AgentAdapter:
        adapter = self._agents.get(target.agent)
        if adapter is None:
            raise KeyError(f"no adapter is loaded for {target.agent} Sessions")
        return adapter

    @staticmethod
    def _honest_route(adapter: AgentAdapter, route: RelayRoute) -> RelayRoute:
        """The route the user asked for, unless the adapter honestly lacks it."""
        if route in adapter.supported_routes():
            return route
        return RelayRoute.DELIVER

    def _waiting_answers(self, target: SessionTarget) -> tuple[PendingRelay, ...]:
        return tuple(
            waiting
            for waiting in self._relays.pending_for(target)
            if waiting.kind is RelayKind.ANSWER
        )

    def _enqueue(
        self,
        request_id: RequestId,
        target: SessionTarget,
        text: str,
        *,
        receipt: DeliveryReceipt | None,
        message_id: str = "",
        authority: RelayAuthority = RelayAuthority.WORDS,
    ) -> PendingRelay:
        return self._relays.enqueue(
            PendingRelay(
                request_id=request_id,
                target=target,
                kind=RelayKind.ANSWER,
                text=text,
                queued_at=self._clock(),
                route=RelayRoute.DELIVER,
                receipt=receipt,
                message_id=message_id,
                authority=authority,
            )
        )

    def _report_failed(self, waiting: PendingRelay, reason: RelayReason) -> RelayOutcome:
        """Take the entry out and say why, in the code and the last attempt's grade.

        The two used to be one sentence in two spellings, because a rendered
        sentence may not claim non-delivery of an attempt that proved nothing.
        The code says what happened here and the receipt says what was proved,
        so neither has to hedge on the other's behalf.
        """
        released = self._relays.release(waiting.request_id)
        _log.info(
            "relay %s is terminal: reason=%s grade=%s",
            released.request_id,
            reason,
            None if released.receipt is None else released.receipt.outcome,
        )
        return RelayOutcome(
            request_id=released.request_id,
            target=released.target,
            state=Lifecycle.REPORTED_FAILED,
            route=released.route,
            reason=reason,
            receipt=released.receipt,
            message_id=released.message_id,
            authority=released.authority,
        )


__all__ = [
    "NO_AUTHORITY_CLAUSE",
    "NO_GRADE",
    "RelayAuthority",
    "RelayOutcome",
    "RelayPipeline",
    "RelayReason",
    "may_be_retried",
    "reaction_for",
    "reason_for",
    "receipt_line",
    "receipt_sentence",
    "sentence_stands_alone",
]
