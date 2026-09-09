"""Publish what a Session said: a roster summary, or a History page.

This private deep module owns every publication capacity. Agent adapters receive
only its derived capture ceiling at composition; callers never choose a budget.
The projection and canonical Reply encoding live here as later vertical slices
exercise them through the Control Plane action and socket seams (ADR 0016).

**Two publications, and the second is bounded differently.** The roster summary
carries no chat body at all; the History page carries a *count* of entries, with
the encoded Reply's byte limit as a ceiling that omits an entry's text rather
than its slot. The exact-detail publication that stood between them retired with
the `progress` action (#171) — the Session Brief carries the newest entry whole
and a page carries that entry and everything before it.

**Two ceilings, chosen by the wire the reader is on** (#302, ADR 0016 as
amended). The Control Plane's 65,536 bytes bound every reply, as before. A
second and much tighter one bounds the Live Call's *return leg* — the line the
Call Agent's answer crosses on its way to the Voice, where codex cuts anything
over 4,000 bytes and leaves a marker saying how much went but never which part.
A `history` or `brief` request is fitted to it only when the request says the
reader is the Voice; a request that says nothing is answered exactly as it was
before the field existed, which is what keeps the Companion Channel unchanged.

The return-leg fit lives here, beside the wire's, because this module already
owns capacity, whole-reply measurement, omission choice and the final check for
both of these documents — so one owner measures, omits and finally checks every
reply on this seam. The opening hand-over keeps its own fit in Briefing: a
different seam with a different policy, and the two are not merged. What is
measured here is the *rendered* text both surfaces share, not the JSON envelope,
because the Voice never sees an envelope.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from gpt_voicecoding.control_plane import commands, payloads
from gpt_voicecoding.core import briefing
from gpt_voicecoding.core.bridge import Status
from gpt_voicecoding.core.briefing import RosterBrief, SessionBrief
from gpt_voicecoding.core.sessions import Session
from gpt_voicecoding.seams.agent import (
    HistoryPage,
    ProgressAvailability,
    ProgressCapture,
    ProgressEntry,
    ProgressObservation,
    ProgressOmission,
    ProgressRole,
    ReplyWindow,
)
from gpt_voicecoding.seams.call import RETURN_LEG_BUDGET_BYTES
from gpt_voicecoding.seams.control_plane import (
    MAX_REQUEST_BYTES,
    Action,
    ErrorCode,
    Reader,
    Reply,
)
from gpt_voicecoding.seams.identity import AgentKind, SessionTarget


def encode_reply(reply: Reply) -> bytes:
    """The canonical Control Plane wire encoding, including its line delimiter."""
    return json.dumps(reply.as_document(), ensure_ascii=False).encode("utf-8") + b"\n"


@dataclass(frozen=True, slots=True)
class _ProgressCapture(ProgressCapture):
    """Validated source capture policy derived from a complete Reply capacity."""

    max_bytes: int

    def __post_init__(self) -> None:
        if self.max_bytes <= 0:
            raise ValueError("the progress capture capacity must be positive")

    def select(
        self,
        entries: Sequence[ProgressEntry],
    ) -> tuple[tuple[ProgressEntry, ...], ProgressOmission]:
        kept = list(entries)
        while kept and self._encoded_size(kept) > self.max_bytes:
            kept.pop(0)
        if not entries:
            omission = ProgressOmission.NONE
        elif not kept:
            omission = ProgressOmission.NEWEST_OVERSIZE
        elif len(kept) < len(entries):
            omission = ProgressOmission.OLDER
        else:
            omission = ProgressOmission.NONE
        return tuple(kept), omission

    @staticmethod
    def _encoded_size(entries: Sequence[ProgressEntry]) -> int:
        return len(
            json.dumps(
                [{"role": str(entry.role), "text": entry.text} for entry in entries],
                ensure_ascii=False,
            ).encode("utf-8")
        )


#: The widest ordinal this module will assume when it sizes a page's slots.
#: Nineteen digits is every count a 64-bit integer can reach, so a bound computed
#: with it holds for every record any Session will ever have — and the bound is
#: then a pure function of the dial and the ceiling rather than of whichever page
#: happens to be in hand. Sizing on the live ordinal would let one configuration
#: be legal at ordinal 9 and illegal at ordinal 10.
_WIDEST_ORDINAL = 10**18

#: What the Voice is told when a Session's decision alone cannot cross the Live
#: Call's return leg (#302). One **fixed** sentence, and both halves of that
#: matter. It names no Session and carries no free text, because a Session name
#: has no byte bound and a refusal that could itself fail the ceiling would be no
#: bound at all — and the Voice already knows which Session it asked about. Its
#: size is therefore a constant, checked against the ceiling once at
#: construction rather than hoped for at runtime, exactly as the wire's own
#: bounded refusal is.
RETURN_LEG_REFUSAL = "this Session's decision is too large to be carried on the call"


class OversizeDecision(Exception):
    """A Session Brief cannot cross the return leg even with its newest given up.

    Raised rather than returned because the answer is no longer a document: the
    contract for a brief is *whole or refused*, never partial (ADR 0016 — a
    decision with a choice missing is the failure #302 exists to prevent). It
    carries the words it is refused in, so the caller renders and never rephrases.
    """

    def __init__(self, message: str = RETURN_LEG_REFUSAL) -> None:
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class ProgressPublication:
    """The one publication policy for this engine process."""

    max_bytes: int = MAX_REQUEST_BYTES
    #: The Live Call's return leg — the line the Call Agent's answer crosses on
    #: its way to the Voice. A *second* ceiling rather than a replacement: the
    #: Control Plane keeps its own for every reply, and a marked `history` or
    #: `brief` is held to both, the tighter one binding (ADR 0016 as amended).
    #: Named here so a test can move it; derived from codex's own constants, so
    #: nothing chooses it.
    return_leg_bytes: int = RETURN_LEG_BUDGET_BYTES
    _capture: _ProgressCapture = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if self.max_bytes <= 0:
            raise ValueError("the Control Plane publication capacity must be positive")
        if self.return_leg_bytes <= 0:
            raise ValueError("the return leg's capacity must be positive")
        # The bounded refusal is a constant, so whether it fits is a fact about
        # this configuration and is settled here rather than at the moment a
        # decision turns out to be too large.
        if len(RETURN_LEG_REFUSAL.encode("utf-8")) > self.return_leg_bytes:
            raise ValueError("the return leg cannot carry its bounded refusal")
        object.__setattr__(self, "_capture", self._derived_capture())

    @property
    def largest_page(self) -> int:
        """The most entry slots this capacity can carry, measured rather than assumed.

        A slot is an `ordinal`, a `role` and `omission="oversize"` — the shape an
        entry takes when its text could not be carried, which is the largest a
        page can be while still saying something true about every entry on it.
        Measured at the widest ordinal and the longer role name, so the answer is
        a floor and never an optimistic one, and so it is a pure function of this
        capacity rather than of whichever page happens to be in hand.

        **Read at composition, where the dial and the ceiling meet** (#171).
        `[policy] history_page_entries` is `CorePolicy`'s and the capacity is
        this module's; neither can answer alone whether a page can be published,
        so `engine/composition.py` asks this and refuses a configuration whose
        page could only ever be every entry marked `oversize`.
        """
        sizes = [self._slots_size(count) for count in (0, 1, 2)]
        first, each = sizes[1] - sizes[0], sizes[2] - sizes[1]
        if self.max_bytes < sizes[1]:
            return 0
        return 1 + (self.max_bytes - sizes[0] - first) // each

    def _slots_size(self, count: int) -> int:
        """The encoded Reply for a page of `count` omitted slots, at their widest."""
        page = HistoryPage(
            entries=tuple(
                ProgressEntry(
                    ordinal=_WIDEST_ORDINAL - index,
                    role=ProgressRole.ASSISTANT,
                    text="x",
                )
                for index in range(count)
            ),
            older=True,
            read_at=datetime(1970, 1, 1, tzinfo=UTC),
        )
        document = self._history_document(page, omitted={entry.ordinal for entry in page.entries})
        return len(encode_reply(Reply.answered(Action.HISTORY, document)))

    @property
    def capture(self) -> ProgressCapture:
        """The single publication-derived source policy supplied to both adapters."""
        return self._capture

    def status_document(self, status: Status) -> dict[str, Any]:
        """The whole status payload, with progress projected as roster summaries."""
        return payloads.status_document(
            status,
            progress_for=self.summary_document,
        )

    def history_document(
        self, page: HistoryPage, *, reader: Reader | None = None
    ) -> dict[str, Any]:
        """One History page, bounded by its count and ceilinged by the wire (#171).

        **The count is the page and the bytes are the ceiling.** Every entry the
        window selected keeps its slot; an entry that would push the encoded
        Reply past its limit is published as *existing but omitted* — its
        `ordinal`, its `role`, `omission="oversize"` and no text — so the page
        always advances and one large message never blocks the ones before it.
        Text is never cut (ADR 0016).

        The largest entries go first, because dropping the text of the one entry
        that does not fit is the smallest honest edit; ties break oldest-first so
        the newest entry a user is most likely to be reading survives longest.

        **There is no case left where the slots themselves do not fit.** Slots
        are not free, so a `[policy] history_page_entries` dialled past what this
        capacity carries would have no honest publication — every entry marked
        `oversize` when the truth is that the page was dialled too big. That
        configuration is refused where it is composed rather than papered over
        here (`__post_init__`), which is ADR 0016's own rule — capacity decides
        what the source may be asked for — applied to the count instead of to
        the bytes. So the fully-omitted page below always fits.

        **Marked for the Voice, the drop order is the opposite one** (#302). On a
        64 KB line the one entry that does not fit is the honest edit; on a
        4,000-byte line the honest edit is to keep the newest entries whole and
        page the oldest away, because the page is one request away from
        continuing and nothing is lost by it. Both rules are right for their
        wire, and they are not unified. The wire's own pass still runs after the
        return leg's, so both ceilings apply and the tighter one binds.
        """
        omitted: set[int] = set()
        if reader is Reader.VOICE:
            page, omitted = self._fitted_to_the_return_leg(page)
        order = sorted(
            page.entries,
            key=lambda entry: (-len(entry.text.encode("utf-8")), entry.ordinal),
        )
        for entry in order:
            document = self._history_document(page, omitted=omitted)
            if self.fits(Reply.answered(Action.HISTORY, document)):
                return document
            omitted.add(entry.ordinal)
        return self._history_document(page, omitted=omitted)

    def _fitted_to_the_return_leg(self, page: HistoryPage) -> tuple[HistoryPage, set[int]]:
        """The page as the Live Call can carry it: whole entries, oldest paged away.

        Two steps, in this order and for different reasons.

        First, an entry whose slot *alone* is over the ceiling is named omitted
        in place — ordinal, role and `omission="oversize"`, the existing shape
        and the existing words. It can never reach the Voice whole on any
        request, so dropping the page around it would page for ever; it keeps its
        place and says so.

        Then, while the page still does not fit, entries go from the **oldest**
        end. `older` becomes true and the cursor keeps its existing meaning — the
        oldest ordinal still on the page. `--before` is an exclusive bound, so
        naming the oldest *kept* entry is what makes the next page start at the
        first *dropped* one; naming the dropped entry itself would skip it.

        **The last entry left gives up its text rather than overrun.** Paging
        lengthens the cursor line — "that is the whole history" becomes "older
        entries remain — ask again with `--before N`" — so an entry that fitted
        the page it started on can fail the page it is left alone on, with
        nothing older left to drop. It keeps its slot and gives up its text,
        which is the same edit the first step makes and always fits. Without
        this the page overran the ceiling by up to 25 bytes.
        """
        omitted = {
            entry.ordinal
            for entry in page.entries
            if not self._return_leg_fits(
                Action.HISTORY,
                self._history_document(replace(page, entries=(entry,)), omitted=set()),
            )
        }
        kept = list(page.entries)  # newest first, so the oldest is the last
        older = page.older
        while kept and not self._return_leg_fits(
            Action.HISTORY,
            self._history_document(
                replace(page, entries=tuple(kept), older=older), omitted=omitted
            ),
        ):
            if len(kept) > 1:
                kept.pop()
                # Dropping any entry means there are older ones to ask for,
                # whatever the window said: the cursor is what brings them back.
                older = True
            elif kept[0].ordinal not in omitted:
                omitted.add(kept[0].ordinal)
            else:
                # A page of one fully-omitted slot is the smallest honest page
                # there is. If that does not fit, no page of this Session does,
                # and the wire's own final check is what says so.
                break
        return replace(page, entries=tuple(kept), older=older), omitted

    def brief_document(
        self, brief: RosterBrief | SessionBrief, *, reader: Reader | None = None
    ) -> dict[str, Any]:
        """One brief, fitted to every wire it will cross (#302, ADR 0016).

        The wire's own fit first, unchanged: `newest` travels twice — as a field
        and inside `text` — so a message that fits one publication can overflow
        this one, and it is named as omitted rather than sliced.

        Then, when the reader is the Voice, the same rule at the tighter ceiling.
        **The newest message is the only part a brief may give up.** If the whole
        still does not fit once it has, the brief is *refused* with a fixed
        sentence: the state, the question, every option and the recommendation
        are spoken whole or not at all, because a decision the user is asked to
        make is never missing its choices. Nothing is sliced and no option is
        dropped.

        The roster brief is never fitted to the return leg — it carries no
        message bodies, costs ~15 tokens per Session, and would not cross until
        70 concurrent Sessions.
        """
        document = payloads.brief_document(brief)
        if not isinstance(brief, SessionBrief):
            return document
        if not self.fits(Reply.answered(Action.BRIEF, document)):
            document = payloads.brief_document(briefing.omitting_newest(brief))
        if reader is not Reader.VOICE or self._return_leg_fits(Action.BRIEF, document):
            return document
        reduced = payloads.brief_document(briefing.omitting_newest(brief))
        if not self._return_leg_fits(Action.BRIEF, reduced):
            raise OversizeDecision
        return reduced

    def _return_leg_fits(self, action: Action, document: dict[str, Any]) -> bool:
        """Whether the *rendered* answer fits the Live Call's return leg.

        The rendered text and not the JSON envelope, because the envelope is not
        what crosses: the Call Agent prints this and hands the print back, and
        the Voice never sees a wire line. `commands.render` is the one renderer
        both surfaces share, so what is measured here is what is carried.
        """
        rendered = commands.render(Reply.answered(action, document))
        return len(rendered.encode("utf-8")) <= self.return_leg_bytes

    def _history_document(self, page: HistoryPage, *, omitted: set[int]) -> dict[str, Any]:
        return {
            "entries": [
                (
                    {
                        "ordinal": entry.ordinal,
                        "role": str(entry.role),
                        "omission": str(ProgressOmission.OVERSIZE),
                    }
                    if entry.ordinal in omitted
                    else {
                        "ordinal": entry.ordinal,
                        "role": str(entry.role),
                        "text": entry.text,
                    }
                )
                for entry in page.entries
            ],
            "older": page.older,
            "read_at": page.read_at.isoformat() if page.read_at is not None else None,
        }

    def summary_document(self, observation: ProgressObservation) -> dict[str, Any]:
        """The uniform progress fields for a roster row, carrying no chat body."""
        omission = observation.omission
        if observation.availability is ProgressAvailability.READABLE and observation.has_history:
            omission = ProgressOmission.STATUS_SUMMARY
        return self._document(observation, recent=(), omission=omission)

    def final(self, reply: Reply) -> Reply:
        """Return a wire-safe reply, or a small refusal when its skeleton cannot fit."""
        if self.fits(reply):
            return reply
        bounded = Reply.refused(
            reply.action,
            ErrorCode.REFUSED,
            "the complete Control Plane reply exceeds its byte limit",
        )
        if not self.fits(bounded):
            raise ValueError("the Control Plane capacity cannot carry its bounded refusal")
        return bounded

    def fits(self, reply: Reply) -> bool:
        """Whether the complete canonical wire line fits this publication."""
        return len(encode_reply(reply)) <= self.max_bytes

    def _derived_capture(self) -> _ProgressCapture:
        """Subtract the smallest valid exact Reply envelope from the largest capacity."""
        probe = ProgressEntry(ordinal=0, role=ProgressRole.ASSISTANT, text="x")
        observation = ProgressObservation.from_capture(
            recent=(probe,),
            omission=ProgressOmission.NONE,
            read_at=datetime(1970, 1, 1, tzinfo=UTC),
        )
        session = Session(
            target=SessionTarget(agent=AgentKind.CODEX, session_id="x"),
            workspace=Path("."),
            first_seen=0.0,
            progress=observation,
        )
        progress = self._document(
            observation,
            recent=observation.recent,
            omission=observation.omission,
        )
        minimum = Reply.answered(
            Action.BRIEF,
            {
                "session": payloads.session_document(
                    session,
                    progress=progress,
                    reply_window=ReplyWindow.CLOSED,
                )
            },
        )
        envelope_bytes = len(encode_reply(minimum)) - _ProgressCapture._encoded_size((probe,))
        capacities = (max(1, self.max_bytes - envelope_bytes),)
        return _ProgressCapture(max(capacities))

    @staticmethod
    def _document(
        observation: ProgressObservation,
        *,
        recent: tuple[ProgressEntry, ...],
        omission: ProgressOmission,
    ) -> dict[str, Any]:
        return {
            "availability": str(observation.availability),
            "has_history": observation.has_history,
            "omission": str(omission),
            "read_at": (
                observation.read_at.isoformat() if observation.read_at is not None else None
            ),
            "recent": [{"role": str(entry.role), "text": entry.text} for entry in recent],
        }
