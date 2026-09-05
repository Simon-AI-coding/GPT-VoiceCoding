"""The Anchor Table — what a reply to one of our own messages means (ADR 0021 §2).

Every outbound Companion Channel message that names one target is an **Anchor**
(`CONTEXT.md`): a reply to it, or plain text typed after it, reaches that target
with no prefix and no Session Name. This module is Bridge Core's memory of them,
keyed by the provider's message id — every part of a split send against one row.

What a row holds is exactly what a reply needs and nothing a reply could be
misread from: the anchor's kind, its target, its option labels in order, the
pending approval id when it carried a permission, and when it was sent. It holds
neither Telegram's echo of our words nor the Session Name — the name is
re-read from the roster, and the words Telegram echoes back are never read (ADR
0021 §2). The brief Core itself composed for that message *is* held, because the
edit that closes a notice re-fills the brief as sent (ADR 0021 §8, #266) and
Core's own record of what it composed is the only place that survives. The
message id is the only key.

Three rules, all structural:

- **Memory only.** Nothing here reaches `state.json`. Sessions are recognised
  afresh at every start (ADR 0020), so a persisted row pointing at a Session the
  engine has not yet re-recognised would be half-reliable; an empty table after a
  restart is one plain rule instead.
- **Rows go with their target**, and each target keeps only its newest N rows —
  a Session's, a menu screen's (#264), or an Assistant Conversation's (#265). No
  time-based expiry and no timer: a day-old notice is a legitimate thing to
  reply to, and a screen that fell out past the cap is an unknown Anchor whose
  numeral gets the one fixed hint.
- **Newest means last registered.** `sent_at` is carried as a fact about the
  message; the order this table answers "the newest Anchor" from is the order
  rows were registered in, which is the order they were sent. A row that is
  revised in place (ADR 0021 §8, #266) is not re-registered and so never becomes
  the newest again.

Legacy (ADR 0010): `legacy@1d32845` has no reply anchoring — **new**.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass, field
from enum import StrEnum

from gpt_voicecoding.core.briefing import MenuWord
from gpt_voicecoding.seams.companion_channel import Notice
from gpt_voicecoding.seams.identity import SessionTarget


class Screen(StrEnum):
    """A menu screen that is nobody's: about the engine, not about one Session (#264).

    The roster, the config screen and the switch screen offer choices about
    the whole engine, so their rows point at the screen itself. Each screen is
    its own target: it keeps its own newest N rows, it is never trimmed to the
    roster, and words replying to one are words that replied to nothing —
    there is no Session for them to be for. A greeting or a prompt about one
    Session is that Session's row, and is not here.
    """

    ROSTER = "roster"
    CONFIG = "config"
    SWITCHES = "switches"


#: What a row points at: a Session; a menu screen that is nobody's (#264); or —
#: for an Assistant Conversation (ADR 0021 §7, #265) — the coding model's own
#: thread id, which Bridge Core names but never stores anywhere else.
AnchorTarget = SessionTarget | Screen | str

#: What one position on a menu screen stands for, when the label is not itself
#: the meaning: the Session a roster label names, the menu word behind a
#: greeting's or the config screen's label, or the switch a switch label names.
#: The menu word keeps its own type rather than travelling as a bare string
#: that the hub parses back (#264 review) — a screen's picks are written by
#: this package, so the type they were written as is the type to read.
AnchorPick = SessionTarget | MenuWord | str


class AnchorKind(StrEnum):
    """What kind of message the row was. Decides what a numeral on it may pick."""

    #: A Stop Notice, in any state. Its options are the question's labels, or
    #: the two verdicts when it carried a permission.
    NOTICE = "notice"
    #: The `Say to <name>:` prompt (#264). No options.
    PROMPT = "prompt"
    #: A relay's receipt. No options.
    RECEIPT = "receipt"
    #: An Assistant Conversation's answer (#265). No options.
    ANSWER = "answer"
    #: A menu screen — roster, greeting, config, switches (#264). Options are the
    #: screen's labels, resolved by position and never by text.
    MENU = "menu"
    #: One History page (#264). No options.
    HISTORY = "history"
    #: The opening line of an Assistant Conversation (#265).
    ASSISTANT = "assistant"


@dataclass(frozen=True, slots=True)
class Anchor:
    """One row of the table. What a reply to that message means."""

    kind: AnchorKind
    target: AnchorTarget
    #: The labels a numeral picks from, in the order they were shown. Empty when
    #: the message offered nothing to pick.
    options: tuple[str, ...] = ()
    #: What each label stands for, by position, on a menu screen whose labels
    #: are not their own meaning (#264): a roster's Sessions, a switch screen's
    #: switch names, a greeting's menu words. One per label, or none at all —
    #: a notice's labels mean themselves. Never read against the label's text.
    picks: tuple[AnchorPick, ...] = ()
    #: The pending permission's handle when this row is a permission notice, so
    #: a numeral resolves to a verdict on the right dialog. Empty otherwise.
    approval_id: str = ""
    #: When the message was sent. A fact carried, not the ordering key.
    sent_at: float = 0.0
    #: The structured brief this message was sent as, when it was sent as one
    #: (ADR 0021 §8, #266). Not for a reply: it is what the notice must be
    #: re-filled from when its decision closes, because the edit keeps the
    #: question, the numbered lines and the fold **as sent** — a brief taken
    #: fresh at that moment would describe a different Session state and
    #: rewrite the record rather than close it. The seam's own carrier,
    #: unchanged, and never re-rendered here.
    notice: Notice | None = None

    def __post_init__(self) -> None:
        if self.picks and len(self.picks) != len(self.options):
            raise ValueError(
                f"a menu row carries one pick per label; {len(self.picks)} pick(s) cannot "
                f"stand for {len(self.options)} label(s)"
            )


@dataclass(frozen=True, slots=True)
class SentNotice:
    """One notice still on screen with its decision open: the row, and where it landed.

    What the closing edit is addressed to (ADR 0021 §8, #266). The ids are the
    edit's target and the row carries the brief to re-fill, so the caller needs
    both and the table hands them over together rather than by two lookups.
    """

    ids: tuple[str, ...]
    anchor: Anchor


@dataclass(slots=True)
class _Row:
    """A row and every id it landed under. Private: callers see `Anchor`."""

    anchor: Anchor
    ids: tuple[str, ...] = field(default_factory=tuple)
    #: Whether this notice has already been edited closed (#266). A row is
    #: closed once: two facts can close one notice — a permission settling and
    #: the Session ending a moment later — and the second edit would rewrite a
    #: message that already says what it has to say.
    handled: bool = False


class AnchorTable:
    """The Anchors still worth replying to. Memory only; see the module docstring."""

    def __init__(self, *, rows_per_target: int, conversations: int) -> None:
        # The dials' spelling is `CorePolicy`'s to check; these guard the two
        # values that would make the table forget each row as it was entered.
        if rows_per_target < 1:
            raise ValueError(
                f"rows_per_target must keep at least one row; {rows_per_target!r} would forget "
                "every notice as it was sent"
            )
        if conversations < 1:
            raise ValueError(
                f"conversations must keep at least one conversation; {conversations!r} would "
                "forget an Assistant Conversation the moment it was opened"
            )
        self._cap = rows_per_target
        self._conversation_cap = conversations
        #: Oldest first. The last is the newest Anchor.
        self._rows: list[_Row] = []
        self._by_id: dict[str, _Row] = {}

    def register(self, message_ids: tuple[str, ...], anchor: Anchor) -> None:
        """Enter one sent message under every id it landed under.

        No ids, no row: a toast and a failed send are not messages the user can
        reply to. Entering the row evicts the oldest of the same target past the
        cap — the newest N are kept, for a Session and for a thread alike.
        """
        ids = tuple(message_id for message_id in message_ids if message_id)
        if not ids:
            return
        row = _Row(anchor=anchor, ids=ids)
        self._rows.append(row)
        for message_id in ids:
            self._by_id[message_id] = row
        self._evict_past_cap(anchor.target)
        self._evict_past_conversation_cap()

    def lookup(self, message_id: str) -> Anchor | None:
        """The row that message id belongs to, or None when Core no longer holds one."""
        if not message_id:
            return None
        row = self._by_id.get(message_id)
        return None if row is None else row.anchor

    def open_notices(self, target: AnchorTarget) -> tuple[SentNotice, ...]:
        """Every notice of one target still on screen with its decision open, oldest first.

        Notices only: a receipt, a prompt and a menu screen carry no decision to
        close. Whether a given row's brief *had* a decision is the caller's
        reading of the brief it carries — this table holds no opinion about what
        is inside a notice. Order is the order they were sent, so the ids the
        caller edits go in the order the user saw them.
        """
        return tuple(
            SentNotice(ids=row.ids, anchor=row.anchor)
            for row in self._rows
            if row.anchor.target == target and row.anchor.kind is AnchorKind.NOTICE
            if not row.handled
        )

    def mark_handled(self, ids: tuple[str, ...]) -> None:
        """Record that the notice landing under those ids has been edited closed.

        Said once, whatever the edit's outcome: a failure is logged and dropped
        with no retry (ADR 0021 §8), so a row that was attempted is done either
        way, and a second fact about the same Session leaves it alone.
        """
        for message_id in ids:
            row = self._by_id.get(message_id)
            if row is not None:
                row.handled = True

    def sent_under(self, message_id: str) -> tuple[str, ...]:
        """Every id the message with this id landed under — its whole row.

        What an edit addressed to a message the user just acted on needs: a
        split send landed under several ids, and the row is edited whole or not
        at all. Empty for an id this table no longer holds. With no id — a
        numeral typed rather than pressed, resolved by the newest-Anchor rule —
        the newest row's ids, which is the message that rule picked.
        """
        row = (
            self._by_id.get(message_id) if message_id else (self._rows[-1] if self._rows else None)
        )
        return () if row is None else row.ids

    def newest(self) -> Anchor | None:
        """The last Anchor sent — what a message that replies to nothing is for."""
        return self._rows[-1].anchor if self._rows else None

    def drop(self, target: AnchorTarget) -> None:
        """Forget every row of one target. A Session that left the roster; a thread that ended."""
        for row in [row for row in self._rows if row.anchor.target == target]:
            self._forget(row)

    def keep_sessions(self, live: Collection[SessionTarget]) -> None:
        """Forget every Session row whose target is not among `live`.

        The roster's identities move under it: a Codex row is re-keyed when its
        Session takes its first turn and gains a thread id, and again on `/new`
        (#73, #77), and discovery reports neither as an ending. A row left under
        the old identity would name a target the roster no longer holds, so
        after each discovery pass the table is trimmed to the roster. Rows that
        are not a Session's — an Assistant Conversation's (#265) — are untouched.
        """
        held = set(live)
        for row in [
            row
            for row in self._rows
            if isinstance(row.anchor.target, SessionTarget) and row.anchor.target not in held
        ]:
            self._forget(row)

    def __len__(self) -> int:
        return len(self._rows)

    def _evict_past_conversation_cap(self) -> None:
        """Keep the newest N Assistant Conversations and drop the rest whole (ADR 0021 §7).

        The row cap above bounds the messages of one conversation; this bounds
        how many conversations are held at all. It is the chat's cap, not a
        Session's — assistant rows belong to no Session and so are never
        trimmed to the roster — and this engine serves one configured chat, so
        per chat and per engine are the same count.

        A conversation is as new as its newest row, which is the order the rows
        stand in: the oldest conversation is the one whose newest message is
        older than every other's. Evicted whole, so a reply to any of its
        messages is an unknown Anchor and takes that path (#265).
        """
        newest_of: dict[str, int] = {}
        for position, row in enumerate(self._rows):
            thread_id = conversation_of(row.anchor)
            if thread_id is not None:
                newest_of[thread_id] = position
        for thread_id, _ in sorted(newest_of.items(), key=lambda held: held[1])[
            : max(0, len(newest_of) - self._conversation_cap)
        ]:
            self.drop(thread_id)

    def _evict_past_cap(self, target: AnchorTarget) -> None:
        held = [row for row in self._rows if row.anchor.target == target]
        for row in held[: max(0, len(held) - self._cap)]:
            self._forget(row)

    def _forget(self, row: _Row) -> None:
        self._rows.remove(row)
        for message_id in row.ids:
            if self._by_id.get(message_id) is row:
                del self._by_id[message_id]


def conversation_of(anchor: Anchor) -> str | None:
    """The thread an Assistant Conversation's row names, or None for any other row.

    The one test of "is this row a conversation's", so the router and the table
    agree on what a conversation is without either re-deriving it.

    A `Screen` is a `StrEnum` and so is a `str`; it is tested for first, because
    a screen is the engine's own and not a conversation. A Session's target is
    not a string at all.
    """
    target = anchor.target
    if isinstance(target, Screen) or not isinstance(target, str):
        return None
    return target
