"""The menu screens — every one an Anchor with option labels (ADR 0021 §6, #264).

The Companion Channel's command menu opens screens: the roster, a greeting for
one Session, the `Say to <name>:` prompt, the config screen, the switch screen,
and an Assistant Conversation's opening line (ADR 0021 §7, #265), which is a
screen in exactly this sense: a text in Core's words with a row saying what a
reply to it means. Each is one shape — a text in Core's words, the seam's structured brief
beside it for a surface that lays screens out, and the Anchor row the send
registers, whose labels a numeral picks by position. This module builds them
and decides nothing about what a press does: that is `core/bridge.py`'s, which
reads the row's `picks` at the moment the numeral arrives.

**Labels resolve by position, never by text.** A roster's labels are Session
Names, and two Sessions may share one; the row carries the target behind each
position (`Anchor.picks`), and the label of a duplicate carries the address
after the name so the user can tell them apart — a courtesy for the eyes,
not a key Core reads. The switch screen's labels carry each switch's state at
the moment the screen was sent, and a press means "flip" whatever the label
still says; the row carries the switch *name*, and the state is re-read when
the press arrives.

**The text is the screen for a surface that draws nothing.** `bridgectl
sessions` and a channel with no buttons print it: the heading, then the labels
numbered, so a typed numeral works identically (ADR 0021 §6). The words are
Briefing's; this module numbers them and adds none.

Legacy (ADR 0010): `legacy@1d32845` has no menu and no Telegram — **new**.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from gpt_voicecoding.core.anchors import Anchor, AnchorKind, AnchorPick, AnchorTarget, Screen
from gpt_voicecoding.core.briefing import (
    ASSISTANT_OPENING_LINE,
    ASSISTANT_REPLY_PLACEHOLDER,
    DISAMBIGUATED_LABEL_TEMPLATE,
    MENU_WORDING,
    NOTHING_RUNNING_HINT,
    SAY_TO_PLACEHOLDER,
    MenuWord,
    RosterBrief,
    greeting,
    roster_notice,
    say_to,
    switch_label,
)
from gpt_voicecoding.core.briefing import text as brief_text
from gpt_voicecoding.core.sessions import Session, spoken_name
from gpt_voicecoding.core.switches import Switchboard
from gpt_voicecoding.seams.companion_channel import MenuNotice, Notice, RosterNotice

#: The three choices a greeting offers about one Session, in the order shown.
GREETING_CHOICES: tuple[MenuWord, ...] = (MenuWord.BRIEF, MenuWord.HISTORY, MenuWord.SEND_MESSAGE)

#: The three choices the config screen offers, in the order shown.
CONFIG_CHOICES: tuple[MenuWord, ...] = (MenuWord.SWITCH, MenuWord.VERIFY, MenuWord.LIVE)


@dataclass(frozen=True, slots=True)
class MenuScreen:
    """One screen, ready to send: its text, its brief, and the row it registers.

    `notice` and `anchor` are None together, for a screen with nothing to pick
    — the roster with no live Session is text and a hint, not an Anchor.
    """

    text: str
    notice: Notice | None = None
    anchor: Anchor | None = None
    #: What the reply bar shows while this screen waits, on a surface that has
    #: one — Core's words, and the one signal that a screen asks for words
    #: rather than a choice (ADR 0021 §6, §7). Empty opens no reply bar.
    reply_bar: str = ""

    @property
    def options(self) -> tuple[str, ...]:
        """The labels in order, for a surface that carries them beside the text."""
        return self.anchor.options if self.anchor is not None else ()


def roster_screen(brief: RosterBrief) -> MenuScreen:
    """`/sessions`: Briefing's roster, one label per live Session, an Anchor of the roster.

    The rows are the Roster Brief's — live main Sessions only, the Focus Session
    first (`briefing.roster`) — so the label at position N is the row at
    position N on every surface. An empty roster is the roster text and the
    nothing-running hint, with no brief and no row: there is nothing to pick.
    """
    if not brief.rows:
        return MenuScreen(text=f"{brief_text(brief)}\n{NOTHING_RUNNING_HINT}")
    notice = roster_notice(brief)
    labels = _disambiguated(
        [
            (str(row.name), str(target.target))
            for row, target in zip(notice.rows, brief.rows, strict=True)
        ]
    )
    # **The row says what its label says** (#264 review). The address only ever
    # went on the label, and the label is the thing a button cuts — from the
    # end, which is exactly where the address is. So two Sessions sharing a
    # name arrived as two buttons reading alike above a roster that named
    # neither address, and the user could not tell which press reached which
    # Session. Layout uses the whole option label on the row as well as the
    # button; the row's SessionName stays intact across the seam (#274).
    targets: tuple[AnchorPick, ...] = tuple(row.target for row in brief.rows)
    return MenuScreen(
        text=brief_text(brief),
        notice=RosterNotice(rows=notice.rows, counts=notice.counts, options=labels),
        anchor=Anchor(kind=AnchorKind.MENU, target=Screen.ROSTER, options=labels, picks=targets),
    )


def greeting_screen(session: Session, peers: Sequence[Session] = ()) -> MenuScreen:
    """A Session picked off the roster: its headline, and `brief` / `history` / `send message`.

    `peers` is the roster this Session sits in, carried for the one word a row
    cannot supply: a Session waiting on another is named by that Session's own
    Session Name (#320). The heading is the headline every other surface prints,
    so it has to be able to say the same thing they do.
    """
    return _choices(
        heading=greeting(session, peers),
        choices=GREETING_CHOICES,
        anchor_target=session.target,
    )


def prompt_screen(session: Session) -> MenuScreen:
    """`send message`: the `Say to <name>:` prompt, an Anchor of that Session, no options.

    Words replying to it are an Answer Relay to the Session, by the Anchor rule
    alone; a numeral on it picks nothing. It has no expiry of its own — a later
    Stop Notice supersedes it by the newest-Anchor rule (ADR 0021 §6).
    """
    heading = say_to(spoken_name(session))
    return MenuScreen(
        text=heading,
        notice=MenuNotice(heading=heading),
        anchor=Anchor(kind=AnchorKind.PROMPT, target=session.target),
        reply_bar=SAY_TO_PLACEHOLDER,
    )


def assistant_screen(thread_id: str) -> MenuScreen:
    """`assistant`: the fixed opening line, an Anchor of one Codex thread (ADR 0021 §7).

    The line is Core's own words and no model wrote it, so opening a
    conversation costs no turn. The row carries the thread id where a Session's
    row carries a target: that id is the whole of Bridge Core's memory of the
    conversation, and the transcript stays where it belongs, on the coding
    model's own thread. Words replying to it are the conversation's first turn,
    by the Anchor rule alone, and the reply bar is only what shows the user so.

    **No structured brief.** A conversation's messages are prose — this one
    fixed, the answers the coding model's own — and a notice is a laid-out brief
    that a surface cuts rather than splits. What they want from the surface is
    the reply bar, which `send` opens; that is not a layout to fill in.
    """
    return MenuScreen(
        text=ASSISTANT_OPENING_LINE,
        anchor=Anchor(kind=AnchorKind.ASSISTANT, target=thread_id),
        reply_bar=ASSISTANT_REPLY_PLACEHOLDER,
    )


def assistant_answer(thread_id: str) -> Anchor:
    """The row one turn's answer registers: the same conversation, nothing to pick.

    A row rather than a screen, because an answer is the coding model's words
    and this module words nothing about it. Every answer anchors, and a split
    answer anchors every part of itself — the send site enters the row under
    every id the message landed under (ADR 0021 §7).
    """
    return Anchor(kind=AnchorKind.ANSWER, target=thread_id)


def config_screen() -> MenuScreen:
    """`/config`: `switch` / `verify` / `live`, an Anchor of the config screen."""
    return _choices(
        heading=MENU_WORDING[MenuWord.CONFIG],
        choices=CONFIG_CHOICES,
        anchor_target=Screen.CONFIG,
    )


def switches_screen(switches: Switchboard) -> MenuScreen:
    """`switch`: one label per switch carrying its state now; a press means flip.

    In the board's own order — the named switches first, then the Feature
    Switches — because a snapshot sorts by name and would put `auto_hangup`
    above `duty`. The row carries the switch names, not the states: a label
    gone stale after a flip elsewhere still means "flip", read against the
    board as it stands when the press arrives (ADR 0021 §6).
    """
    names: tuple[AnchorPick, ...] = switches.names()
    labels = tuple(switch_label(name, switches.is_set(name)) for name in switches.names())

    heading = MENU_WORDING[MenuWord.SWITCHES]
    return MenuScreen(
        text=menu_text(heading, labels),
        notice=MenuNotice(heading=heading, options=labels),
        anchor=Anchor(kind=AnchorKind.MENU, target=Screen.SWITCHES, options=labels, picks=names),
    )


def menu_text(heading: str, options: Sequence[str]) -> str:
    """The plain rendering of a screen: the heading, then the labels numbered.

    What `bridgectl` prints and a channel with no buttons sends — the numbered
    lines are what a typed numeral is read against, so they are in the text
    on every surface (ADR 0021 §6).
    """
    lines = [heading, *(f"{position}. {label}" for position, label in enumerate(options, 1))]
    return "\n".join(lines)


def _choices(
    *, heading: str, choices: tuple[MenuWord, ...], anchor_target: AnchorTarget
) -> MenuScreen:
    labels = tuple(MENU_WORDING[word] for word in choices)
    picks: tuple[AnchorPick, ...] = choices
    return MenuScreen(
        text=menu_text(heading, labels),
        notice=MenuNotice(heading=heading, options=labels),
        anchor=Anchor(
            kind=AnchorKind.MENU,
            target=anchor_target,
            options=labels,
            picks=picks,
        ),
    )


def _disambiguated(named: Sequence[tuple[str, str]]) -> tuple[str, ...]:
    """Names as labels, the address after any name two rows share."""
    seen = Counter(name for name, _ in named)
    return tuple(
        DISAMBIGUATED_LABEL_TEMPLATE.format(name=name, address=address) if seen[name] > 1 else name
        for name, address in named
    )
