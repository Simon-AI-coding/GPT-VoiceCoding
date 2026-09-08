"""What the acting half of a Live Call is told: codex's Stock Text, then this engine's tools.

A codex v3 realtime call is two models, and this is the one with tools (ADR
0018). It hears the user's speech, runs the control plane, and hands what came
back to the Voice. It is told nothing about tone, order or pacing, because it
never speaks to anybody — and that costs nothing, while a speaking rule here
would be a rule in the set that cannot act on it.

**Stock Text first** (#289 P1, ADR 0018 as amended): codex's own
`realtime_start.md` at `catalogue.STOCK_TEXT_VERSION`, whose five content lines
our old set had displaced and two of them dropped (#294) — that a hand-off may
be spurious, and that the words are a transcript with recognition errors. They
are kept in codex's wording, extended in place with the three roles of this call
(Call Agent, Voice, engine), and joined by this engine's response rules in one
paragraph. Each of the five carries an `agent.stock.*` rule id, so a line
deleted here fails the coverage gate (#300). Then one **Engine tools** section:
the invocation and the shared calling rules, and one entry per tool grouping its
purpose, its usage form and the rules that are its own — the structure Simon
chose over a concatenated catalogue (#299).

**Shaping is this half's** (P4). The `relay` entry carries the **Relayed
Instruction** (`CONTEXT.md`): the user's spoken pieces gathered into one
complete instruction in their own meaning, tidied of what speech leaves behind,
with nothing added, decided or chosen. Stock's "use the transcript to decide"
is what makes this the half that can do it — the Voice never rewrote its
hand-off on the wire (#288), and this half did, without a rule either way.

**Five actions, five usage lines, and six the call does not get.** `status`,
the switch flip and the seam report are withheld: the voice call neither
queries the engine's switches nor flips them (#173), and an action in this text
is an action the model will find a reason to run. The three menu verbs —
`sessions`, `config` and `assistant` (#264) — are withheld beside them: a
screen is a surface's way of offering the user choices to press, and this half
has `brief` for the roster and nothing to press on. The split is total over the
closed action set by construction, so a twelfth action fails generation until
somebody decides which side of the line it is on.

**The forms come from the shared vocabulary, never from memory.** `USAGE` sits
in `seams/control_plane.py` beside `Action`, and it is what `bridgectl` prints
as its help and what the Companion Channel's `/` grammar refuses against — so a
form written out here by hand would be a third spelling, free to drift from the
parser that has to accept it. `brief` carries its address in brackets, which is
why #173's six forms are five usage lines: `brief` and `brief <address>` are
one line with an optional argument.

**The budget is 8,192 bytes** because the backend caps what this audience is
given at 8,192 tokens and a byte is the floor on what one token costs — the same
proof the Voice's own cap rests on, against a limit that is somebody else's
rather than ours. Which wire field the cap belongs to is the realtime adapter's
to know; here it is a number about this half.
"""

from __future__ import annotations

from gpt_voicecoding.core.instructions.blocks import (
    Block,
    InstructionError,
    InstructionSet,
    Section,
)
from gpt_voicecoding.core.instructions.catalogue import Audience
from gpt_voicecoding.core.instructions.context import InstructionContext
from gpt_voicecoding.seams.control_plane import USAGE, Action

#: The backend's cap on what the acting half is started with, in tokens
#: (ADR 0018).
AGENT_INSTRUCTION_TOKEN_BUDGET = 8_192

#: The same number in the unit that proves it: one token costs at least one byte.
MAX_AGENT_INSTRUCTION_BYTES = AGENT_INSTRUCTION_TOKEN_BUDGET

#: The actions a Live Call's acting half is given, in the order #173 §4 lists
#: them. Five actions, rendered as that section's six forms.
AGENT_ACTIONS: tuple[Action, ...] = (
    Action.BRIEF,
    Action.HISTORY,
    Action.RELAY,
    Action.APPROVE,
    Action.LIVE,
)

#: The actions it is not given, written down rather than merely absent — the
#: two look identical otherwise, and only one of them is a decision.
#: The three menu verbs (#264) are withheld with them: a screen is a surface's
#: way of offering the user choices to press, and the acting half of a call has
#: `brief` for the roster and no screen to press anything on.
WITHHELD_ACTIONS: tuple[Action, ...] = (
    Action.STATUS,
    Action.SWITCH,
    Action.VERIFY,
    Action.SESSIONS,
    Action.CONFIG,
    Action.ASSISTANT,
)

#: What each given action is for, in one sentence — the first line of its
#: entry. Total over `AGENT_ACTIONS`, so an action nobody explained fails
#: generation. The rules that are the action's own (that history pages
#: backwards, what a relay carries, that nothing but the toggle ends a call)
#: are rules with ids and live in the entry's block below, not here.
AGENT_GIST: dict[Action, str] = {
    Action.BRIEF: (
        "Use this for current Session status, the newest message, or the current "
        "decision with its question, options, recommendation or permission request. With "
        "no address, get the roster; with an address, get that Session's full brief."
    ),
    Action.HISTORY: "Use this when the user asks for earlier messages.",
    Action.RELAY: "Use this to carry the user's words into the addressed Session.",
    Action.APPROVE: (
        "Use this to answer the pending permission request identified by the approval "
        "id, carrying the user's verdict."
    ),
    Action.LIVE: "When the user asks to hang up, run this command to end the call.",
}


def agent_instructions(context: InstructionContext) -> InstructionSet:
    """The Call Agent's rules, for this engine and this machine."""
    _every_action_is_placed()
    # Rendered without section titles: stock `realtime_start.md` opens on a
    # sentence, not a heading, and the one heading this set does carry — Engine
    # tools — is written where it stands. The titles stay for the reader.
    return InstructionSet(audience=Audience.AGENT, sections=_sections(context), headings=False)


def _every_action_is_placed() -> None:
    """Every control-plane action is given or withheld, and every given one explained."""
    unsorted = set(AGENT_ACTIONS) | set(WITHHELD_ACTIONS)
    if unsorted != set(Action) or set(AGENT_ACTIONS) & set(WITHHELD_ACTIONS):
        raise InstructionError(
            "every control-plane action is either given to the Call Agent or withheld "
            "from it, and these are on neither side or on both: "
            + ", ".join(sorted(str(action) for action in set(Action) ^ unsorted))
        )
    missing = [action for action in AGENT_ACTIONS if not AGENT_GIST.get(action, "").strip()]
    if missing:
        raise InstructionError(
            "the Call Agent is given actions nothing here explains: "
            + ", ".join(str(action) for action in missing)
        )


def _entry(action: Action, *rules: str) -> str:
    """One tool's entry: its heading, its usage form, what it is for, and its own rules."""
    heading, usage = f"### {action}", f"`{USAGE[action]}`"
    return "\n\n".join((heading, usage, " ".join((AGENT_GIST[action], *rules))))


def _sections(context: InstructionContext) -> tuple[Section, ...]:
    return (
        Section(
            title="Stock Text",
            blocks=(
                Block(
                    covers=("agent.stock.call-started",),
                    text="Realtime conversation started.",
                ),
                Block(
                    covers=("agent.stock.executor-behind-the-voice",),
                    text=(
                        "You are the Call Agent, the backend executor behind the Voice, which "
                        "has no tools. You use this engine to act on the user's requests. The "
                        "user does not talk to you directly. Any response you produce will be "
                        "consumed by the Voice and may be summarized before the user hears it."
                    ),
                ),
                Block(
                    covers=("agent.stock.transcript-decides-whether-to-work",),
                    text=(
                        "When invoked, you receive the latest conversation transcript and any "
                        "relevant mode or metadata. The Voice may invoke you even when backend "
                        "help is not actually needed. Use the transcript to decide whether you "
                        "should do work. If backend help is unnecessary, avoid verbose "
                        "responses that add user-visible latency."
                    ),
                ),
                Block(
                    covers=("agent.stock.speech-is-a-transcript",),
                    text=(
                        "When user text is routed from realtime, treat it as a transcript. It "
                        "may be unpunctuated or contain recognition errors."
                    ),
                ),
                # Stock L9 and this engine's two output rules in one paragraph
                # (#299): the stock sentence keeps its own id, so the merge
                # cannot swallow it (#300).
                Block(
                    covers=(
                        "agent.stock.concise-updates",
                        "agent.output.returns-it-whole",
                        "agent.outcome.only-a-successful-call-is-success",
                    ),
                    text=(
                        "For updates without an engine result, keep responses concise and "
                        "action-oriented so the Voice can respond to the user. Return an engine "
                        "result whole, in its original order; the Voice handles its spoken "
                        "presentation. Report a refused or failed command and stop: no retry, "
                        "alternate Session or compensating action. Claim an action succeeded "
                        "only after its command returns successfully."
                    ),
                ),
            ),
        ),
        Section(
            title="Engine tools",
            blocks=(
                Block(
                    covers=(
                        "agent.cli.one-generated-command",
                        "agent.verbs.only-the-six-forms",
                        "agent.identity.copies-the-address-unchanged",
                        "agent.read.now-every-time",
                    ),
                    text=(
                        "## Engine tools\n\n"
                        f"Invoke the engine with `{context.cli.invocation} <action> "
                        f"[arguments]` (engine version {context.cli.version}). Pass arguments "
                        "as arguments; never build a shell string from the user's words or "
                        "edit the invocation path. Use only the engine forms below. Copy "
                        "Session addresses unchanged from engine replies. For a requested "
                        "read, query now rather than reuse an earlier answer."
                    ),
                ),
                # #277: asked what a Session needed decided, the Call Agent ran
                # `history` twice and reported no options, while `brief <address>`
                # held the question and all four. The entry says which read
                # answers which ask.
                Block(
                    covers=("agent.brief.is-the-session-now",),
                    text=_entry(
                        Action.BRIEF,
                        "Current decision details belong here, not in History.",
                    ),
                ),
                Block(
                    covers=("agent.history.pages-older-on-request",),
                    text=_entry(
                        Action.HISTORY,
                        "Request the latest page without `--before`. When the user asks to "
                        "continue further back, use the `--before` value supplied by the "
                        "previous page. Fetch further pages only on request.",
                    ),
                ),
                Block(
                    covers=("agent.relay.carries-the-users-words",),
                    text=_entry(
                        Action.RELAY,
                        "Gather the spoken pieces into one complete Relayed Instruction in the "
                        "user's own meaning, removing fillers, stutters, overruled "
                        "self-corrections and framing addressed to the Voice; add, expand, "
                        "decide and choose nothing on the user's behalf. The receipt "
                        "determines the delivery outcome: only `delivered` means the words "
                        "arrived; otherwise report the returned status and reason.",
                    ),
                ),
                Block(text=_entry(Action.APPROVE)),
                Block(
                    covers=("agent.live.ends-the-call",),
                    text=_entry(Action.LIVE, "A spoken goodbye does not end it."),
                ),
            ),
        ),
    )
