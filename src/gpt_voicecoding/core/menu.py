"""The menu screens — every one an Anchor with option labels (ADR 0021 §6, #264).

The Companion Channel's command menu opens screens: the roster, a greeting for
one Session, the `Say to <name>:` prompt, the config screen and the switch
screen. Each is one shape — a text in Core's words, the seam's structured brief
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
    DISAMBIGUATED_LABEL_TEMPLATE,
    MENU_WORDING,
    NOTHING_RUNNING_HINT,
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
            (row.name, str(target.target))
            for row, target in zip(notice.rows, brief.rows, strict=True)
        ]
    )
    targets: tuple[AnchorPick, ...] = tuple(row.target for row in brief.rows)
    return MenuScreen(
        text=brief_text(brief),
        notice=RosterNotice(rows=notice.rows, counts=notice.counts, options=labels),
        anchor=Anchor(kind=AnchorKind.MENU, target=Screen.ROSTER, options=labels, picks=targets),
    )


def greeting_screen(session: Session) -> MenuScreen:
    """A Session picked off the roster: its headline, and `brief` / `history` / `send message`."""
    return _choices(
        heading=greeting(session),
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
        notice=MenuNotice(heading=heading, expects_words=True),
        anchor=Anchor(kind=AnchorKind.PROMPT, target=session.target),
    )


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
    picks: tuple[AnchorPick, ...] = tuple(str(word) for word in choices)
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
