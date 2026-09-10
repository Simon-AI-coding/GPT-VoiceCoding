"""What the three generated instruction sets owe, and how that is proved.

Coverage is asserted against the ids the generators *emit*, never against the
words they chose. That is the whole point of the catalogue: the prose is free to
be rewritten, translated or restructured, and the suite still fails the moment a
rule stops being carried or moves to a set that was never meant to have it.

Three things here are asserted about words rather than ids, and all three are
product decisions rather than preferences about style. Each set is **codex's
Stock Text first, then this engine's Overlay** (ADR 0018 as amended 2026-09-08,
#289): the stock headings come in codex's order, the prefix the Voice is told
is the one the adapter dials with, and the lines the fate table removed are
absent. The Voice is **never told a verb exists**: a Voice handed something it
cannot do invents rather than refuses (#179), so every control-plane command in
its text is an invitation to fabricate. And the Voice's set **dictates no
sentence in any language** — it speaks the engine's grade in the user's own
(#299). None of the three survives as an id, so each is read off the rendered
text.

**codex's Stock Text is the one place words are ids.** Every stock line is a
rule whose `gist` is the line (#300), so those are asserted against the rendered
text by the catalogue rather than by hand — and the refusal itself lives in
`InstructionSet`, because a block carries a whole bullet list and coverage alone
cannot see one bullet go.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from gpt_voicecoding.control_plane.commands import build_request
from gpt_voicecoding.core.instructions import (
    ACTION_GIST,
    AGENT_ACTIONS,
    AGENT_INSTRUCTION_TOKEN_BUDGET,
    BY_ID,
    MAX_AGENT_INSTRUCTION_BYTES,
    MAX_VOICE_INSTRUCTION_BYTES,
    RULES,
    STOCK_TEXT_VERSION,
    VOICE_INSTRUCTION_TOKEN_BUDGET,
    WITHHELD_ACTIONS,
    Audience,
    Block,
    ControlPlaneCli,
    InstructionContext,
    InstructionError,
    InstructionSet,
    Rule,
    Section,
    agent_instructions,
    delegated_instructions,
    generate,
    ids_for,
    voice_instructions,
)
from gpt_voicecoding.core.instructions import agent as agent_module
from gpt_voicecoding.core.instructions import catalogue as catalogue_module
from gpt_voicecoding.core.instructions import delegated as delegated_module
from gpt_voicecoding.core.instructions import voice as voice_module
from gpt_voicecoding.core.instructions.catalogue import STOCK_SOURCE_PREFIX
from gpt_voicecoding.seams.call import CODEX_RESPONSE_ITEM_PREFIX
from gpt_voicecoding.seams.control_plane import READER_FLAG, USAGE, Action, Reader, Request

CLI = ControlPlaneCli(
    command=Path("/Applications/GPT-VoiceCoding.app/Contents/MacOS/bridgectl"),
    version="1.4.2",
    socket_path=Path("/tmp/gpt-voicecoding-501/control.sock"),
)
CONTEXT = InstructionContext(cli=CLI)

#: The words each Overlay paragraph is found by. Named once, because a marker
#: written out at each use is a marker that can drift from its prose.
RECEIPT_MARK = "handed over"
AUTHORITY_MARK = "own confirmation"
DETAILS_MARK = "### Details and History"

#: codex's `backend_prompt.md` headings, in codex's order, as the Voice hears
#: them before anything of this engine's (#289 P1). A heading carries no rule —
#: it states no obligation, and a `Section` title has nowhere to claim an id —
#: so this tuple is the whole of what holds them, which is why `### Policies`
#: belongs in it beside the seven `## ` ones (#300).
STOCK_VOICE_HEADINGS = (
    "## Identity, tone, and role",
    "## Interface and operating model",
    "### Policies",
    "## Backend use and steering",
    "## Backend outputs and user inputs",
    "## Presenting backend results",
    "## Task-level user preferences",
    "## Communication style",
)


def paragraph_with(instructions, mark: str) -> str:
    """The one Voice paragraph carrying `mark`, and a failure when it is not one.

    Module-level rather than a method, because two classes look paragraphs up
    this way and reaching into a sibling class for it makes one of them own a
    helper the other borrows.
    """
    paragraphs = [part for part in instructions.voice.text.split("\n\n") if part.strip()]
    found = [part for part in paragraphs if mark in part]
    assert len(found) == 1, found
    return found[0]


@pytest.fixture(scope="module")
def instructions():
    return generate(CONTEXT)


class TestCatalogue:
    def test_every_rule_id_is_unique(self) -> None:
        ids = [rule.id for rule in RULES]
        assert len(ids) == len(set(ids))

    def test_every_rule_names_where_it_came_from(self) -> None:
        """Provenance survives migration, later product decisions, and codex's own text.

        Three forms, because there are three ways a rule got here: a span of a
        migrated skill file, the ticket that ruled it, and — since #300 — the
        codex template line the Stock Text was taken from.
        """
        for rule in RULES:
            assert rule.source.startswith(("skill/", "issue/", STOCK_SOURCE_PREFIX)), rule.id

    def test_a_rule_carried_by_code_names_where(self) -> None:
        for rule in RULES:
            if rule.audience.is_code:
                assert rule.enforced_by.strip(), rule.id

    def test_a_prose_rule_claims_no_enforcer(self) -> None:
        """Naming code for a rule that is only ever prose would claim what is not there."""
        with pytest.raises(ValueError):
            Rule(
                id="voice.invented",
                audience=Audience.VOICE,
                source="skill/SKILL.md:1-2",
                gist="something",
                enforced_by="core/bridge.py",
            )

    def test_a_code_rule_without_an_enforcer_is_refused(self) -> None:
        with pytest.raises(ValueError):
            Rule(
                id="core.invented",
                audience=Audience.CORE,
                source="skill/SKILL.md:1-2",
                gist="something",
            )

    def test_every_rule_id_is_prefixed_by_the_audience_that_carries_it(self) -> None:
        """A `voice.` id in the Agent set would be a lie about who hears it (#190)."""
        for rule in RULES:
            assert rule.id.startswith(f"{rule.audience}."), rule.id

    def test_nothing_is_recorded_as_dropped_any_more(self) -> None:
        """Retired rules are deleted rows; git history and #173's table are the record."""
        assert not hasattr(Audience, "DROPPED")
        assert not [rule for rule in RULES if rule.id.startswith("dropped.")]


class TestTheTableIsSettled:
    """#173's disposition of the twenty-one `voice.*` rules, one by one.

    Nine survive as Voice rules, one became a Call Agent rule, and eleven were
    deleted. Written down as sets rather than counts, because a count passes
    while the wrong rule is missing. Later decisions add sets of their own — what
    a ticket ruled in, what one retired, and codex's Stock Text (#300) — rather
    than editing #173's reading, which has to keep saying what it said then.
    """

    SURVIVING = frozenset(
        {
            "voice.identity.speak-names",
            "voice.instruction.one-clean-instruction",
            "voice.attribution.judgement-keeps-its-owner",
            "voice.notice.is-natural-speech",
            "voice.notice.invents-no-detail",
            "voice.notice.says-what-could-not-be-read",
            "voice.notice.speaks-in-this-shape",
            "voice.delivery.tells-the-truth-about-arrival",
            "voice.delivery.a-refusal-is-an-answer",
        }
    )

    #: Retired by #173's criterion — the 0901 flow does not ask for them.
    RETIRED = (
        "voice.orientation.no-screen",
        "voice.target.disambiguate-or-ask",
        "voice.conversation.no-action",
        "voice.authority.no-identity-from-the-screen",
        "voice.notice.asks-for-no-decision-nobody-awaits",
        "voice.roster.withheld-sessions-are-real",
        "voice.retry.failed-delivery-is-not-a-retry",
        "voice.retry.queued-is-not-delivered",
        "voice.retry.no-compensating-action",
        "voice.start.an-empty-read-is-not-a-failed-read",
        "voice.start.no-substitute-after-a-failure",
        "voice.notice.reads-progress-when-asked-for-more",
    )

    #: Voice rules decided after #173's table, each by the ticket that ruled it.
    #: A separate set rather than a tenth line in `SURVIVING`, because that set
    #: is a record of one reading and must keep saying what it said then; a rule
    #: added later is a second decision and is written down as one (#234).
    ADDED_SINCE = frozenset(
        {
            "voice.delivery.a-relayed-answer-carries-no-authority",
            "voice.delegation.older-entries-are-not-held",
        }
    )

    #: Voice rules retired after #173's table, each by the ticket that ruled it.
    #: #289 P4 put the shaping of the user's instruction with the Call Agent,
    #: as the Relayed Instruction, and #288 found the Voice never did it on
    #: the wire (13 of 13 hand-offs verbatim) — so the Voice's own tidying
    #: rule was a rule with no behaviour behind it, and #299 deleted the row.
    RETIRED_SINCE = frozenset({"voice.instruction.one-clean-instruction"})

    def test_the_voice_set_is_those_nine_and_what_was_decided_since(self) -> None:
        """The nine #173 kept, less what a later ticket retired, plus what one ruled in.

        And codex's own lines, which are the Voice's rules too since #300 — a
        fourth group rather than entries in the three above, because their
        provenance is a template at a pinned tag rather than a reading or a
        ticket, and they leave together when the tag moves.
        """
        stock = {rule_id for rule_id in self.STOCK if rule_id.startswith(f"{Audience.VOICE}.")}
        assert ids_for(Audience.VOICE) == (
            (self.SURVIVING - self.RETIRED_SINCE) | self.ADDED_SINCE | stock
        )

    def test_every_retired_rule_is_gone_from_the_catalogue(self) -> None:
        known = {rule.id for rule in RULES}
        assert not known & set(self.RETIRED)
        assert not known & self.RETIRED_SINCE

    def test_the_relay_rule_carries_the_relayed_instruction(self) -> None:
        """#289 P4: shaping moved to the half that reads the transcript, under the same id."""
        rule = BY_ID["agent.relay.carries-the-users-words"]
        assert rule.audience is Audience.AGENT
        assert rule.source == "issue/289"
        assert "Relayed Instruction" in rule.gist

    #: Every rule the catalogue still owes, by id. The audit that replaces the
    #: file-line totality #193 retired: a rule deleted without a decision fails
    #: here on its name, and no retired rule's line numbers are written down to
    #: do it — those are a seam into the catalogue's own layout, which is why
    #: the ticket removes their spans rather than parking them.
    RETAINED = frozenset(
        {
            "core.delivery.four-states-and-one-request-identity",
            "core.identity.native-ids-stay-in-calls",
            "core.identity.validates-exact-target",
            "core.notice.owns-the-facts",
            "core.notice.owns-the-stop-detail",
            "core.relay.chooses-the-route",
            "core.relay.owns-the-reply-window-and-the-target",
            "core.retry.holds-the-canonical-notice-state",
            "core.retry.owns-escalation-and-eligibility",
            "core.retry.refusals-name-their-reason",
            "core.retry.requeues-exactly-one",
            "core.roster.is-the-registry",
            "core.roster.rejects-stale-or-ambiguous",
            "core.start.exact-identity-until-a-name-exists",
            "core.start.no-automatic-retry-after-a-terminal-failure",
            "delegated.authority.acts-only-through-the-control-plane",
            "delegated.cli.one-generated-command",
            "delegated.delivery.repeats-nothing-without-consent",
            "delegated.delivery.stays-inside-one-attempt",
            "delegated.identity.exact-structured",
            "delegated.notice.answers-the-exact-request",
            "delegated.notice.reads-before-it-reports",
            "delegated.notice.reports-a-failed-read-as-a-failed-read",
            "delegated.outcome.only-a-successful-call-is-success",
            "delegated.progress.reports-only-what-came-back",
            "delegated.relay.takes-the-route-as-given",
            "delegated.retry.only-a-retryable-notice",
            "delegated.start.distinguishes-empty-from-unread",
            "agent.brief.is-the-session-now",
            "agent.cli.one-generated-command",
            "agent.history.pages-older-on-request",
            "agent.identity.copies-the-address-unchanged",
            "agent.live.ends-the-call",
            "agent.outcome.only-a-successful-call-is-success",
            "agent.output.returns-it-whole",
            "agent.read.now-every-time",
            "agent.relay.carries-the-users-words",
            "agent.verbs.only-the-six-forms",
        }
    )

    #: codex's Stock Text, id by id, each against the template line it was taken
    #: from (#300). A dict rather than a set of ids, because the line number is
    #: half of what a codex upgrade has to re-read: the audit diffs the new
    #: template against exactly these, disposes whatever moved in the fate table
    #: (`scripts/prompts/stock-overlay/review.md`), and only then moves
    #: `STOCK_TEXT_VERSION`. Removed lines are not here — they stay rows in that
    #: table, because the catalogue records what is owed, not what was declined.
    STOCK = {
        "voice.stock.concise-no-fluff": "backend_prompt.md:5",
        "voice.stock.playful-collaborator": "backend_prompt.md:7",
        "voice.stock.user-first-name": "backend_prompt.md:9",
        "voice.stock.talk-like-a-friend": "backend_prompt.md:11",
        "voice.stock.the-user-speaks": "backend_prompt.md:15",
        "voice.stock.backend-executes": "backend_prompt.md:17",
        "voice.stock.one-unified-assistant": "backend_prompt.md:23",
        "voice.stock.pass-execution-through": "backend_prompt.md:24",
        "voice.stock.never-refuse": "backend_prompt.md:25",
        "voice.stock.backend-output-is-authoritative": "backend_prompt.md:26",
        "voice.stock.conversation-supports-execution": "backend_prompt.md:27",
        "voice.stock.always-use-the-backend": "backend_prompt.md:31",
        "voice.stock.answer-directly-only-when-self-contained": "backend_prompt.md:32",
        "voice.stock.never-claim-inability": "backend_prompt.md:33",
        "voice.stock.clarify-only-to-avoid-harm": "backend_prompt.md:34",
        "voice.stock.running-work-is-steerable": "backend_prompt.md:35",
        "voice.stock.running-work-can-be-redirected": "backend_prompt.md:36",
        "voice.stock.both-arrive-as-user-messages": "backend_prompt.md:40",
        "voice.stock.backend-messages-are-prefixed": "backend_prompt.md:41",
        "voice.stock.updates-or-final-outputs": "backend_prompt.md:42",
        "voice.stock.tell-the-takeaway": "backend_prompt.md:48",
        "voice.stock.read-out-no-formatted-content": "backend_prompt.md:49",
        "voice.stock.the-backend-transforms": "backend_prompt.md:50",
        "voice.stock.detail-only-on-request": "backend_prompt.md:51",
        "voice.stock.preferences-are-task-level": "backend_prompt.md:56",
        "voice.stock.preferences-persist": "backend_prompt.md:57",
        "voice.stock.no-silent-revert": "backend_prompt.md:58",
        "voice.stock.proceed-without-framing": "backend_prompt.md:62",
        "voice.stock.no-narration": "backend_prompt.md:63",
        "voice.stock.updates-brief-and-grounded": "backend_prompt.md:64",
        "voice.stock.updates-stay-frequent-on-request": "backend_prompt.md:65",
        "agent.stock.call-started": "realtime_start.md:1",
        "agent.stock.executor-behind-the-voice": "realtime_start.md:3",
        "agent.stock.transcript-decides-whether-to-work": "realtime_start.md:5",
        "agent.stock.speech-is-a-transcript": "realtime_start.md:7",
        "agent.stock.concise-updates": "realtime_start.md:9",
    }

    def test_the_stock_rules_are_exactly_these_codex_lines(self) -> None:
        """A stock rule deleted, added or quietly re-pointed fails here by name and line."""
        assert {
            rule.id: rule.source.removeprefix(f"{STOCK_SOURCE_PREFIX}{STOCK_TEXT_VERSION}:")
            for rule in RULES
            if rule.is_stock
        } == self.STOCK

    def test_the_catalogue_owes_exactly_these_rules_and_no_others(self) -> None:
        """The proof that nothing left unread, carried by ids rather than by lines.

        `SURVIVING` pins the nine Voice rules #173 kept, `ADDED_SINCE` the Voice
        rules ruled in after it and `STOCK` codex's own lines; this pins
        everything else the catalogue still holds. A rule deleted because a diff
        was convenient fails here by name, and adding one is a decision somebody
        writes down in the same commit — in one of the four sets, which is the
        decision.
        """
        assert {rule.id for rule in RULES} == (
            self.RETAINED
            | (self.SURVIVING - self.RETIRED_SINCE)
            | self.ADDED_SINCE
            | set(self.STOCK)
        )

    def test_the_history_rule_moved_to_the_call_agent_under_its_own_name(self) -> None:
        """#190's deferred key: a read belongs to the acting half, so the id says so."""
        moved = next(rule for rule in RULES if rule.id == "agent.history.pages-older-on-request")
        assert moved.audience is Audience.AGENT
        assert moved.source == "issue/151"


class TestEveryRuleStillNamesRealProvenance:
    """The line-accounting audit, over the rules that are still here.

    The full-file totality assertion left with the eleven retired rules: their
    spans are deleted rather than parked, and a test that read a deleted rule's
    line numbers would be a seam into the catalogue's own layout. What stays is
    the direction that still has meaning — a retained rule points at lines that
    really existed in the file it names.
    """

    MIGRATED = {
        "skill/SKILL.md": 96,
        "skill/announcing.md": 121,
        "skill/checking-and-talking.md": 119,
        "skill/closing.md": 60,
        "skill/retrying.md": 57,
        "skill/starting.md": 151,
    }

    #: Lines whose rules left with launch and close (#72). Written down rather
    #: than deleted from `MIGRATED`, for the same reason: they come back with the
    #: actions they describe, and are readable at the `parked/launch-close` tag.
    PARKED = {
        "skill/closing.md": ((1, 60),),
        "skill/starting.md": ((1, 24), (42, 96), (127, 138)),
    }

    @staticmethod
    def _span(source: str) -> tuple[str, int, int]:
        name, _, lines = source.partition(":")
        first, _, last = lines.partition("-")
        return name, int(first), int(last or first)

    def test_every_retained_rule_points_inside_the_file_it_names(self) -> None:
        for rule in RULES:
            if not rule.source.startswith("skill/"):
                continue
            name, first, last = self._span(rule.source)
            assert name in self.MIGRATED, rule.source
            assert 1 <= first <= last <= self.MIGRATED[name], rule.source

    def test_a_parked_span_names_no_line_a_rule_still_claims(self) -> None:
        """Parking is a record of absence, so it may not paper over a live rule."""
        live: dict[str, set[int]] = {name: set() for name in self.MIGRATED}
        for rule in RULES:
            if not rule.source.startswith("skill/"):
                continue
            name, first, last = self._span(rule.source)
            live[name].update(range(first, last + 1))

        for name, spans in self.PARKED.items():
            for first, last in spans:
                overlap = sorted(set(range(first, last + 1)) & live[name])
                assert not overlap, f"{name} lines {overlap} are parked and still claimed"


class TestCoverage:
    def test_each_retained_rule_lands_in_exactly_its_own_set(self, instructions) -> None:
        for rule in RULES:
            assert instructions.carrier_of(rule.id) is rule.audience, rule.id

    def test_every_voice_rule_is_carried_by_the_voice_set(self, instructions) -> None:
        assert instructions.voice.covers == ids_for(Audience.VOICE)

    def test_every_agent_rule_is_carried_by_the_agent_set(self, instructions) -> None:
        assert instructions.agent.covers == ids_for(Audience.AGENT)

    def test_every_delegated_rule_is_carried_by_the_delegated_set(self, instructions) -> None:
        assert instructions.delegated.covers == ids_for(Audience.DELEGATED)

    def test_a_rule_enforced_in_code_wears_no_prose(self, instructions) -> None:
        """Core and adapter rules are proved by their own tests, never by a prompt."""
        for rule in RULES:
            if rule.audience.is_code:
                assert rule.id not in instructions.voice.covers
                assert rule.id not in instructions.agent.covers
                assert rule.id not in instructions.delegated.covers

    @pytest.mark.parametrize(
        ("audience", "prefix"),
        [(Audience.VOICE, "voice"), (Audience.AGENT, "agent"), (Audience.DELEGATED, "delegated")],
    )
    def test_generation_refuses_a_set_that_lost_a_rule(self, audience, prefix, monkeypatch) -> None:
        added = Rule(
            id=f"{prefix}.rule.nobody.wrote",
            audience=audience,
            source="skill/SKILL.md:1-1",
            gist="a rule the generator does not carry",
        )
        monkeypatch.setattr(catalogue_module, "RULES", (*RULES, added))
        with pytest.raises(InstructionError, match=f"{prefix}.rule.nobody.wrote"):
            generate(CONTEXT)


class TestInstructionSet:
    def test_claiming_a_rule_that_does_not_exist_is_refused(self) -> None:
        with pytest.raises(InstructionError, match="not a rule"):
            InstructionSet(
                audience=Audience.VOICE,
                sections=(Section("t", (Block(text="x", covers=("voice.no.such.rule",)),)),),
            )

    def test_claiming_the_same_rule_twice_is_refused(self) -> None:
        rule_id = next(iter(sorted(ids_for(Audience.VOICE))))
        with pytest.raises(InstructionError, match="twice"):
            InstructionSet(
                audience=Audience.VOICE,
                sections=(
                    Section(
                        "t",
                        (Block(text="x", covers=(rule_id,)), Block(text="y", covers=(rule_id,))),
                    ),
                ),
            )

    def test_a_set_may_not_carry_another_audiences_rule(self) -> None:
        rule_id = next(iter(sorted(ids_for(Audience.CORE))))
        with pytest.raises(InstructionError, match="may not carry it"):
            InstructionSet(
                audience=Audience.VOICE,
                sections=(Section("t", (Block(text="x", covers=(rule_id,)),)),),
            )

    def test_there_is_no_instruction_set_for_rules_code_carries(self) -> None:
        with pytest.raises(InstructionError, match="carried by code"):
            InstructionSet(
                audience=Audience.CORE,
                sections=(Section("t", (Block(text="x"),)),),
            )

    def test_connective_prose_may_cover_nothing(self) -> None:
        """A heading sentence is allowed, and proves nothing."""
        built = InstructionSet(
            audience=Audience.VOICE,
            sections=(Section("t", (Block(text="an example"),)),),
        )
        assert built.covers == frozenset()

    def test_a_set_may_be_rendered_without_its_headings(self) -> None:
        """The Voice hears prose; the title is navigation for whoever reads the code."""
        with_headings = InstructionSet(
            audience=Audience.VOICE,
            sections=(Section("A title", (Block(text="a paragraph"),)),),
        )
        without = InstructionSet(
            audience=Audience.VOICE,
            sections=(Section("A title", (Block(text="a paragraph"),)),),
            headings=False,
        )
        assert with_headings.text == "## A title\n\na paragraph\n"
        assert without.text == "a paragraph\n"


class TestTheTwoBudgets:
    """Two budgets, for two reasons, both measured in bytes.

    A byte-level BPE token never costs less than one byte of input, so a byte
    count is an upper bound on a token count — in any script, with no tokenizer
    and no average-characters-per-token figure to be wrong about. The Voice keeps
    this engine's own 8,000, which codex does not impose and which stands as the
    measure of "terse"; the Agent set is capped at the 8,192 codex caps that slot
    at (ADR 0018).
    """

    def test_the_budgets_are_the_two_figures_adr_0018_names(self) -> None:
        assert VOICE_INSTRUCTION_TOKEN_BUDGET == 8_000
        assert MAX_VOICE_INSTRUCTION_BYTES == VOICE_INSTRUCTION_TOKEN_BUDGET
        assert AGENT_INSTRUCTION_TOKEN_BUDGET == 8_192
        assert MAX_AGENT_INSTRUCTION_BYTES == AGENT_INSTRUCTION_TOKEN_BUDGET

    def test_the_voice_set_fits_its_budget(self, instructions) -> None:
        assert instructions.voice.size_in_bytes <= MAX_VOICE_INSTRUCTION_BYTES

    def test_the_agent_set_fits_its_budget(self, instructions) -> None:
        assert instructions.agent.size_in_bytes <= MAX_AGENT_INSTRUCTION_BYTES

    def test_the_budget_is_counted_in_utf8_bytes_not_characters(self) -> None:
        """One CJK character is three bytes, and three bytes is what it may cost."""
        built = InstructionSet(
            audience=Audience.VOICE,
            sections=(Section("t", (Block(text="你好"),)),),
        )
        assert built.size_in_bytes > len(built.text)

    def test_an_oversized_voice_set_stops_generation(self, monkeypatch) -> None:
        monkeypatch.setattr("gpt_voicecoding.core.instructions.MAX_VOICE_INSTRUCTION_BYTES", 100)
        with pytest.raises(InstructionError, match="voice instructions are"):
            generate(CONTEXT)

    def test_an_oversized_agent_set_stops_generation(self, monkeypatch) -> None:
        monkeypatch.setattr("gpt_voicecoding.core.instructions.MAX_AGENT_INSTRUCTION_BYTES", 100)
        with pytest.raises(InstructionError, match="agent instructions are"):
            generate(CONTEXT)


class TestTheStockLinesAreOwedLineByLine:
    """#300: codex's Stock Text is rules too, and the gate can see a line go.

    Every other rule is proved by its id alone, because its prose is free. A
    stock rule is proved by its words as well: #289 P1 decided to send codex's
    own wording at a pinned tag, so the line is the obligation. That is what
    makes a bullet deleted from the middle of a block a failure — the ids the
    block claims are all still there, and only the words are missing.
    """

    def test_every_stock_line_is_rendered_word_for_word(self, instructions) -> None:
        rendered = {
            Audience.VOICE: instructions.voice.text,
            Audience.AGENT: instructions.agent.text,
        }
        for rule in RULES:
            if rule.is_stock:
                assert rule.gist in rendered[rule.audience], rule.id

    def test_every_stock_rule_is_claimed_by_the_set_that_says_it(self, instructions) -> None:
        for rule in RULES:
            if rule.is_stock:
                assert instructions.carrier_of(rule.id) is rule.audience, rule.id

    def test_claiming_a_stock_line_without_saying_it_is_refused(self) -> None:
        """The refusal that block-level ids cannot give: one bullet gone, both ids kept."""
        said = BY_ID["voice.stock.never-refuse"]
        unsaid = BY_ID["voice.stock.one-unified-assistant"]
        with pytest.raises(InstructionError, match="word for word"):
            InstructionSet(
                audience=Audience.VOICE,
                sections=(
                    Section(
                        title="Policies",
                        blocks=(Block(covers=(said.id, unsaid.id), text=said.gist),),
                    ),
                ),
            )

    def test_a_block_that_says_both_of_them_is_fine(self) -> None:
        """The positive control: the refusal above is about the missing line, not the pair."""
        said, also = BY_ID["voice.stock.never-refuse"], BY_ID["voice.stock.one-unified-assistant"]
        built = InstructionSet(
            audience=Audience.VOICE,
            sections=(
                Section(
                    title="Policies",
                    blocks=(Block(covers=(said.id, also.id), text=f"{also.gist}\n{said.gist}"),),
                ),
            ),
        )
        assert built.covers == {said.id, also.id}

    def test_a_stock_bullet_deleted_from_the_generator_stops_generation(self, monkeypatch) -> None:
        """The failure the ticket asks for, on the real Voice set rather than a fixture."""
        stock, victim = voice_module._stock, BY_ID["voice.stock.never-refuse"]

        def thinned() -> tuple[Section, ...]:
            return tuple(
                Section(
                    title=section.title,
                    blocks=tuple(
                        Block(covers=block.covers, text=block.text.replace(f"{victim.gist}\n", ""))
                        for block in section.blocks
                    ),
                )
                for section in stock()
            )

        monkeypatch.setattr(voice_module, "_stock", thinned)
        with pytest.raises(InstructionError, match=victim.id):
            generate(CONTEXT)

    def test_the_codex_tag_is_written_in_exactly_one_place(self) -> None:
        """Two copies of a version pin drift; the audit then reads the wrong template."""
        catalogue_source = Path(catalogue_module.__file__).read_text(encoding="utf-8")
        assert catalogue_source.count(f'"{STOCK_TEXT_VERSION}"') == 1
        for module in (voice_module, agent_module):
            written = Path(module.__file__).read_text(encoding="utf-8")
            assert STOCK_TEXT_VERSION not in written, module.__name__


class TestTheVoiceHearsStockTextThenOverlay:
    """ADR 0018 as amended: codex's `backend_prompt.md` first, this engine's Overlay after."""

    def test_the_engine_hands_the_words_and_the_voice_is_told_how(self, instructions):
        voice = instructions.voice.text
        assert "This person opened the call themselves" not in voice
        assert "Speak what the engine hands you when it hands it" in voice
        assert "otherwise wait to be spoken to" in voice

    def test_the_stock_headings_come_first_and_in_codexs_order(self, instructions) -> None:
        """P1: stock text in its own form and wording, and the Overlay after all of it."""
        voice = instructions.voice.text
        headings = (*STOCK_VOICE_HEADINGS, f"## {voice_module.OVERLAY_TITLE}")
        at = [voice.find(heading) for heading in headings]
        assert all(place >= 0 for place in at), dict(zip(headings, at, strict=True))
        assert at == sorted(at), dict(zip(headings, at, strict=True))
        assert voice.startswith(STOCK_VOICE_HEADINGS[0])

    def test_the_prefix_the_voice_is_told_is_the_one_the_adapter_dials_with(
        self, instructions
    ) -> None:
        """#287 §7 F3: v3 applies neither stock prefix; the seam constant is the one it sees."""
        voice = instructions.voice.text
        assert f"prefixed with `{CODEX_RESPONSE_ITEM_PREFIX}`" in voice
        assert "[USER] " not in voice
        assert "[BACKEND] " not in voice

    #: One phrase from each stock line the fate table removed or corrected
    #: (`scripts/prompts/stock-overlay/review.md`): codex's identity, the two
    #: first-person-ownership lines, the visible-surface lines, and the
    #: completion tool return frameless never sends (#294, #296).
    REMOVED_STOCK_PHRASES = (
        "You are Codex",
        "Present every work as done by you",
        "Present the updates/result as if done by you",
        "sending text directly to the backend",
        "user-visible artifacts",
        "the user can always send requests directly",
        "backend-visible output",
        "tool return indicating completion",
    )

    @pytest.mark.parametrize("phrase", REMOVED_STOCK_PHRASES)
    def test_a_stock_line_the_fate_table_removed_is_absent(self, phrase, instructions) -> None:
        assert phrase not in instructions.voice.text

    #: Every control-plane command the Voice may not be told about, as a word.
    #: `live` has to be a word: `delivered` contains it, and a substring test
    #: would have failed on a sentence about delivery.
    #:
    #: **`brief`, `relay`, `history` and `status` are deliberately not here.**
    #: All four are also ordinary English for what this half hears and says:
    #: the Session Brief and Roster Brief are Briefing's own nouns (#166), "you
    #: relay what a Session said" is the identity paragraph, `History` is the
    #: record's name in `CONTEXT.md`, and `status` is stock's own word for what
    #: to tell the user. What this test can still prove is that no command is
    #: named: no CLI, and none of the words below.
    VERBS = re.compile(r"\b(bridgectl|switch|approve|verify|live)\b", re.IGNORECASE)

    def test_the_voice_is_never_told_a_verb_exists(self, instructions) -> None:
        """A Voice handed something it cannot do invents rather than refuses (#179)."""
        found = self.VERBS.findall(instructions.voice.text)
        assert not found, f"the voice set names control-plane verbs: {sorted(set(found))}"

    def test_the_call_agents_invocation_carries_the_reader_flag(self, instructions) -> None:
        """#302: the Call Agent is told a command line that already says who reads it.

        The engine sets the mark, at the moment it generates these instructions.
        The Call Agent never learns that the mark exists — it copies one
        invocation — which is why nothing in the text explains it.
        """
        assert f"{CLI.invocation} --reader voice <action>" in instructions.agent.text

    def test_the_generated_invocation_is_one_the_parser_accepts(self, instructions) -> None:
        """The flag word is written in Core and read in the control plane (ADR 0001).

        Core may not import the parser, so the two spell `--reader` separately.
        This is what keeps them from drifting: the line that is actually
        generated is handed to the parser that has to accept it, and the reader
        it yields is the one the engine fits an answer for.
        """
        flag, value = CLI.invocation_for_the_voice.rsplit(maxsplit=2)[-2:]
        request = build_request("brief", [], reader=value)

        assert flag == READER_FLAG
        assert Request.of(request.as_document()).reader is Reader.VOICE

    def test_the_delegated_set_carries_the_unflagged_invocation(self, instructions) -> None:
        """A Delegated Turn hands nothing to the Voice, so it fits no return leg.

        `Audience.DELEGATED` is action discipline for work handed to a coding
        model during a call; `Audience.AGENT` is the half that fetches a read and
        hands it back. Only the second crosses codex's cut.
        """
        assert f"{CLI.invocation} <action>" in instructions.delegated.text
        assert "--reader" not in instructions.delegated.text

    def test_the_voice_set_never_learns_the_mark_exists(self, instructions) -> None:
        assert "--reader" not in instructions.voice.text

    def test_the_reader_flag_is_the_only_change_to_the_agents_command_line(
        self, instructions
    ) -> None:
        """The three rendered sets are otherwise byte-for-byte what they were.

        Asserted as a substitution rather than as a pinned blob: putting the flag
        back where it came from must reproduce the previous text exactly, so any
        second edit riding along with this one fails here.
        """
        assert (
            instructions.agent.text.replace(" --reader voice <action>", " <action>").count(
                f"{CLI.invocation} <action>"
            )
            == 1
        )

    def test_the_voice_set_names_no_cli(self, instructions) -> None:
        """The invocation lives with the half that can run it."""
        assert str(CLI.command) not in instructions.voice.text
        assert str(CLI.socket_path) not in instructions.voice.text

    def test_the_voice_text_is_the_same_on_every_machine(self) -> None:
        """The sharper form of "names no CLI": nothing about this install is in it.

        The other two sets differ per machine by design — they name the binary
        they run. A Voice set that varied at all would mean some mechanism had
        got in, and this catches the ones a path check would not, like a version
        string or a socket name worked into a sentence.
        """
        elsewhere = voice_instructions(
            InstructionContext(
                cli=ControlPlaneCli(
                    command=Path("/opt/homebrew/bin/bridgectl"),
                    version="9.9.9",
                    socket_path=Path("/tmp/other.sock"),
                ),
            )
        )
        assert elsewhere.text == voice_instructions(CONTEXT).text

    def test_the_overlay_runs_in_the_order_299_fixed(self, instructions) -> None:
        """Identity, the opening rule, the two Briefs, the receipt, the authority
        clause, and Details and History — all of it after the Overlay heading."""
        voice = instructions.voice.text
        marks = (
            f"## {voice_module.OVERLAY_TITLE}",
            "You are the voice of an engine",
            "Speak what the engine hands you",
            "Session Brief",
            "Roster Brief",
            RECEIPT_MARK,
            AUTHORITY_MARK,
            DETAILS_MARK,
        )
        at = [voice.find(mark) for mark in marks]
        assert all(place >= 0 for place in at), dict(zip(marks, at, strict=True))
        assert at == sorted(at), dict(zip(marks, at, strict=True))

    def test_the_voice_dictates_no_sentence_in_any_language(self, instructions) -> None:
        """#299: the Voice speaks the engine's grade in the user's language.

        The old set spelled two Chinese receipt sentences, and the acceptance
        walk graded delivery by them. Grading what a model *said* is now an
        anti-pattern the acceptance names and never rebuilds
        (`docs/acceptance-design.md` §1), the Live Call is off the machine
        entirely (§2), and the prompt carries no sentence the Voice is to say
        verbatim in any language but its own instructions'. No CJK in the set is
        the checkable form of that.
        """
        assert re.search(r"[\u3400-\u9fff]", instructions.voice.text) is None

    @staticmethod
    def _receipt_paragraph(instructions) -> str:
        """The one paragraph #173 §3.7 fixes, found by the moment it starts at."""
        return paragraph_with(instructions, RECEIPT_MARK)

    def test_the_hand_off_moment_is_spoken_of_before_the_receipt(self, instructions) -> None:
        """#221: the Voice said the delivered word at hand-off, seconds before the relay ran.

        The paragraph reaches the hand-off moment first and says what is known
        there, so the grade is never the only sentence in view when the words
        go out.
        """
        paragraph = self._receipt_paragraph(instructions)
        marks = (RECEIPT_MARK, "say that much or nothing", "grades it", "once, after it returns")
        at = [paragraph.find(mark) for mark in marks]
        assert all(place >= 0 for place in at), dict(zip(marks, at, strict=True))
        assert at == sorted(at), dict(zip(marks, at, strict=True))

    def test_the_receipt_is_the_engines_grade_and_its_reason(self, instructions) -> None:
        """The four truths of `seams/delivery.py`, as the user hears them, and a refusal
        answered the same way (`voice.delivery.a-refusal-is-an-answer`)."""
        paragraph = self._receipt_paragraph(instructions)
        for grade in ("arrived", "next turn", "held", "failed"):
            assert grade in paragraph, grade
        assert "reason for anything but an arrival" in paragraph
        assert "A refusal is answered the same way" in paragraph

    def test_the_voice_stops_after_the_receipt(self, instructions) -> None:
        """One receipt, and then silence unless asked (#198)."""
        paragraph = self._receipt_paragraph(instructions)
        assert "Then stop" in paragraph
        assert "only when they ask" in paragraph

    @staticmethod
    def _authority_paragraph(instructions) -> str:
        """The paragraph #234 added, found by the clause it dictates."""
        return paragraph_with(instructions, AUTHORITY_MARK)

    def test_the_two_answers_that_carry_authority_escape_the_clause(self, instructions) -> None:
        """ADR 0013 §3 as the user hears it: which answers are the user's own.

        Two are: an answer to a question the engine offered with its choices
        (ADR 0015's held hook), and a verdict on a permission (the Approval
        Relay) — either one *and* reported answerable from here. Both halves
        matter in both directions. Choices alone are not the hook — a brief
        carries a question read off the transcript after its writer has gone,
        and mid-turn words take the inbox regardless — and a permission has no
        choices at all, so an exemption written around choices would put the
        clause on the one answer that is unambiguously the user's.
        """
        paragraph = self._authority_paragraph(instructions)
        assert "with its choices" in paragraph
        assert "verdict on a permission" in paragraph
        assert "said they can answer from here" in paragraph

    def test_every_other_answer_gets_the_clause(self, instructions) -> None:
        """Both of the two ways a question can fall outside the hook are named."""
        paragraph = self._authority_paragraph(instructions)
        marks = ("Every other answer", "merely said", "no longer offers from here", AUTHORITY_MARK)
        at = [paragraph.find(mark) for mark in marks]
        assert all(place >= 0 for place in at), dict(zip(marks, at, strict=True))
        assert at == sorted(at), dict(zip(marks, at, strict=True))

    def test_the_authority_clause_predicts_nothing_about_the_session(self, instructions) -> None:
        """#234's evidence: two runs, same product, opposite readings by the Session.

        Which way it goes is the Session's own call, so the Voice says what the
        words are and stops — a guess either way is an invented answer.
        """
        paragraph = self._authority_paragraph(instructions)
        assert "its own call" in paragraph
        assert "never guess" in paragraph


class TestDetailsAndHistory:
    """The Overlay section Simon approved in #299, and the three rules it carries.

    Current detail is explained from what was supplied or fetched; earlier
    records come from the backend a page at a time (#240: the request for a
    Session's older entries reached no rule on three runs of twelve, so whether
    it was handed on was the model's guess); and a partial answer says which
    part is missing and why (`voice.notice.says-what-could-not-be-read`).
    """

    @staticmethod
    def _section(instructions) -> str:
        """The section from its own heading to the end of the set, which it closes."""
        voice = instructions.voice.text
        assert voice.count(DETAILS_MARK) == 1
        return voice[voice.index(DETAILS_MARK) :]

    def test_current_detail_is_explained_from_what_was_supplied_or_fetched(
        self, instructions
    ) -> None:
        section = self._section(instructions)
        marks = (
            "newest message in full",
            "every option with its meaning",
            "ask the backend to obtain the requested detail if it is missing",
            "not earlier records",
        )
        at = [section.find(mark) for mark in marks]
        assert all(place >= 0 for place in at), dict(zip(marks, at, strict=True))
        assert at == sorted(at), dict(zip(marks, at, strict=True))

    def test_earlier_records_come_from_the_backend_a_page_at_a_time(self, instructions) -> None:
        """#240, in the order the Voice meets it: the ask, where it goes, and what
        the hand-over does not hold."""
        section = self._section(instructions)
        marks = (
            "said earlier",
            "ask the backend for History",
            "five entries",
            "next older page",
            "does not supply those earlier records",
        )
        at = [section.find(mark) for mark in marks]
        assert all(place >= 0 for place in at), dict(zip(marks, at, strict=True))
        assert at == sorted(at), dict(zip(marks, at, strict=True))

    def test_a_partial_answer_is_said_to_be_partial(self, instructions) -> None:
        section = self._section(instructions)
        assert "unavailable or truncated" in section
        assert "give the reason supplied with the result" in section

    def test_the_section_is_the_block_that_carries_the_three_rules(self) -> None:
        """Coverage is claimed by the block that says it, not by a neighbour."""
        blocks = [
            block
            for section in voice_instructions(CONTEXT).sections
            for block in section.blocks
            if block.text.startswith(DETAILS_MARK)
        ]
        assert len(blocks) == 1
        assert set(blocks[0].covers) == {
            "voice.notice.invents-no-detail",
            "voice.notice.says-what-could-not-be-read",
            "voice.delegation.older-entries-are-not-held",
        }

    def test_the_rule_is_the_voices_alone(self) -> None:
        """The paging rule is the Call Agent's and correct; this half gains no verb."""
        rule = BY_ID["voice.delegation.older-entries-are-not-held"]
        assert rule.audience is Audience.VOICE
        assert rule.source == "issue/240"


class TestTheAgentSetIsTheActingHalf:
    """#173 §4: six forms, the CLI that runs them, and nothing it may not run."""

    def test_the_action_split_is_total_over_the_closed_set(self) -> None:
        """A ninth action fails the build until somebody decides who may run it."""
        assert set(AGENT_ACTIONS) | set(WITHHELD_ACTIONS) == set(Action)
        assert not set(AGENT_ACTIONS) & set(WITHHELD_ACTIONS)

    def test_it_is_given_exactly_the_five_actions_173_names(self) -> None:
        assert set(AGENT_ACTIONS) == {
            Action.BRIEF,
            Action.HISTORY,
            Action.RELAY,
            Action.APPROVE,
            Action.LIVE,
        }

    def test_the_rendered_set_shows_all_six_forms_173_names(self, instructions) -> None:
        """Six names, five lines — and read off the text, not off the action tuple.

        #173 §4 lists `brief`, `brief <address>`, `history <address> [--before
        N]`, `relay`, `approve` and `live`. Five of those are actions: `brief`
        and `brief <address>` are one action whose address is optional, and its
        one usage line shows both forms, which is why hand-splitting it would be
        retyping a grammar the parser already owns.

        Asserted against `instructions.agent.text` because what is graded is what
        the Call Agent hears. A test that read `AGENT_ACTIONS` would pass on a
        renderer that emitted nothing at all.
        """
        rendered = instructions.agent.text
        for action in AGENT_ACTIONS:
            assert USAGE[action] in rendered, action

        brief = next(line for line in rendered.splitlines() if USAGE[Action.BRIEF] in line)
        assert re.search(r"\bbrief \[.+\]", brief), brief

    def test_the_rendered_set_names_no_action_it_may_not_run(self, instructions) -> None:
        """The voice call neither queries nor flips switches, nor opens a screen (#173, #264).

        Read off the rendered usage lines — one form in backticks under each
        entry's heading — rather than off every word of the prose: `sessions`
        is an ordinary word in the sentence that explains `brief`, and a
        withheld verb is withheld as a *form the agent may run*, not as a word.
        """
        lines = instructions.agent.text.splitlines()
        listed = [line.strip("`") for line in lines if line.startswith("`") and line.endswith("`")]
        for action in WITHHELD_ACTIONS:
            named = [line for line in listed if line.startswith(USAGE[action])]
            assert named == [], f"the agent set names {action}: {named}"
            assert f"### {action}" not in lines, f"the agent set has an entry for {action}"

    def test_it_names_the_cli_the_context_gave_it(self, instructions) -> None:
        assert str(CLI.command) in instructions.agent.text
        assert str(CLI.socket_path) in instructions.agent.text
        assert CLI.version in instructions.agent.text

    def test_a_different_installation_produces_different_text(self, instructions) -> None:
        elsewhere = agent_instructions(
            InstructionContext(
                cli=ControlPlaneCli(
                    command=Path("/opt/homebrew/bin/bridgectl"),
                    version="9.9.9",
                    socket_path=Path("/tmp/other.sock"),
                ),
            )
        )
        assert "/opt/homebrew/bin/bridgectl" in elsewhere.text
        assert str(CLI.command) not in elsewhere.text
        assert elsewhere.text != instructions.agent.text

    @staticmethod
    def _entry(instructions, action: Action) -> str:
        """One tool's entry: from its `### <action>` heading to the next heading or the end."""
        text = instructions.agent.text
        start = text.index(f"### {action}\n")
        following = text.find("\n### ", start + 1)
        return text[start : following if following >= 0 else len(text)]

    def test_the_stock_text_opens_the_set_and_the_tools_follow(self, instructions) -> None:
        """P1: `realtime_start.md` in codex's own words, then one Engine tools section."""
        text = instructions.agent.text
        assert text.startswith("Realtime conversation started.\n")
        marks = (
            "may invoke you even when backend help is not actually needed",
            "Use the transcript to decide whether you should do work",
            "contain recognition errors",
            "## Engine tools",
        )
        at = [text.find(mark) for mark in marks]
        assert all(place >= 0 for place in at), dict(zip(marks, at, strict=True))
        assert at == sorted(at), dict(zip(marks, at, strict=True))

    def test_each_given_action_has_one_entry_in_173s_order(self, instructions) -> None:
        text = instructions.agent.text
        at = [text.find(f"### {action}\n") for action in AGENT_ACTIONS]
        assert all(place >= 0 for place in at), dict(zip(AGENT_ACTIONS, at, strict=True))
        assert at == sorted(at), dict(zip(AGENT_ACTIONS, at, strict=True))
        for action in AGENT_ACTIONS:
            assert f"`{USAGE[action]}`" in self._entry(instructions, action), action

    def test_the_entries_put_the_decision_in_brief_and_the_record_in_history(
        self, instructions
    ) -> None:
        """#277: asked what needed deciding, the Call Agent ran `history` twice.

        `brief <address>` held the parked question and its four options
        throughout; `history` cannot, because a pending question reaches the
        engine by hook before the transcript holds it. So the brief entry owns
        the current decision and says so, and the history entry owns the
        earlier messages. Graded on the rendered text, because that is what the
        Call Agent hears.
        """
        brief = self._entry(instructions, Action.BRIEF)
        history = self._entry(instructions, Action.HISTORY)
        assert "current decision" in brief and "options" in brief, brief
        assert "belong here, not in History" in brief, brief
        assert "earlier messages" in history, history
        assert "`--before` value supplied by the previous page" in history, history
        assert "agent.brief.is-the-session-now" in instructions.agent.covers

    def test_the_relay_entry_carries_the_relayed_instruction(self, instructions) -> None:
        """#289 P4: shaping is this half's, and the receipt's meaning sits beside it."""
        relay = self._entry(instructions, Action.RELAY)
        assert "one complete Relayed Instruction" in relay
        assert "add, expand, decide and choose nothing" in relay
        assert "only `delivered` means the words arrived" in relay

    def test_it_says_which_verb_ends_the_call(self, instructions) -> None:
        """#179, 3 of 3: told this, the Call Agent ran it on every spoken request."""
        live = self._entry(instructions, Action.LIVE)
        assert "run this command to end the call" in live
        assert "A spoken goodbye does not end it" in live

    def test_an_action_nobody_explained_stops_generation(self, monkeypatch) -> None:
        from gpt_voicecoding.core.instructions import agent as agent_module

        thinned = {
            action: gist
            for action, gist in agent_module.AGENT_GIST.items()
            if action is not Action.LIVE
        }
        monkeypatch.setattr(agent_module, "AGENT_GIST", thinned)
        with pytest.raises(InstructionError, match="live"):
            agent_instructions(CONTEXT)


class TestTheDelegatedSetNamesTheRealCli:
    def test_it_names_the_command_the_context_gave_it(self, instructions) -> None:
        assert str(CLI.command) in instructions.delegated.text

    def test_it_names_the_engine_it_reaches(self, instructions) -> None:
        assert str(CLI.socket_path) in instructions.delegated.text

    def test_it_names_the_engines_version(self, instructions) -> None:
        assert CLI.version in instructions.delegated.text

    def test_a_different_installation_produces_different_text(self, instructions) -> None:
        """The CLI is generated, so nothing about it can be a hard-coded string."""
        elsewhere = delegated_instructions(
            InstructionContext(
                cli=ControlPlaneCli(
                    command=Path("/opt/homebrew/bin/bridgectl"),
                    version="9.9.9",
                    socket_path=Path("/tmp/other.sock"),
                ),
            )
        )
        assert "/opt/homebrew/bin/bridgectl" in elsewhere.text
        assert str(CLI.command) not in elsewhere.text
        assert elsewhere.text != instructions.delegated.text

    def test_a_path_with_spaces_survives_a_shell(self) -> None:
        spaced = delegated_instructions(
            InstructionContext(
                cli=ControlPlaneCli(
                    command=Path("/Application Support/GPT-VoiceCoding/bridgectl"),
                    version="1.0",
                    socket_path=Path("/tmp/s.sock"),
                ),
            )
        )
        assert "'/Application Support/GPT-VoiceCoding/bridgectl'" in spaced.text

    def test_a_cli_that_is_not_where_it_really_is_gets_refused(self) -> None:
        """A bare name resolves against a PATH the generated thread may not share."""
        with pytest.raises(InstructionError, match="really is"):
            ControlPlaneCli(
                command=Path("bridgectl"), version="1.0", socket_path=Path("/tmp/s.sock")
            )

    def test_a_cli_without_a_version_is_refused(self) -> None:
        with pytest.raises(InstructionError):
            ControlPlaneCli(
                command=Path("/x/bridgectl"), version="  ", socket_path=Path("/tmp/s.sock")
            )


class TestTheActionSetIsGeneratedFromTheClosedSet:
    def test_every_action_has_a_line(self) -> None:
        assert set(ACTION_GIST) == set(Action)

    def test_every_action_appears_in_the_delegated_set(self, instructions) -> None:
        for action in Action:
            assert str(action) in instructions.delegated.text

    def test_an_action_nobody_explained_stops_generation(self, monkeypatch) -> None:
        """The forcing function: a new action fails the build until someone writes its line."""
        thinned = {
            action: gist for action, gist in ACTION_GIST.items() if action is not Action.LIVE
        }
        monkeypatch.setattr(delegated_module, "ACTION_GIST", thinned)
        with pytest.raises(InstructionError, match="live"):
            delegated_instructions(CONTEXT)


class TestAllThreeSetsAreOrdinaryData:
    def test_generation_touches_no_file(self, tmp_path, monkeypatch) -> None:
        """No skill is installed, no pointer is written, nothing is read from disk."""
        monkeypatch.chdir(tmp_path)
        generate(CONTEXT)
        assert list(tmp_path.iterdir()) == []

    @pytest.mark.parametrize("generator", [voice_instructions, agent_instructions])
    def test_the_same_context_generates_the_same_text(self, generator) -> None:
        assert generator(CONTEXT).text == generator(CONTEXT).text
