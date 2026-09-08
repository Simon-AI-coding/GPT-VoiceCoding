"""How a generated instruction set is put together, and what it owes.

A generator writes prose in blocks, and each block declares the rule ids it
discharges. The set is therefore able to *say* what it covers — coverage is
emitted as data alongside the text, rather than recovered afterwards by
searching the text for words that might mean the right thing. Rewriting a
sentence changes nothing about coverage; deleting a rule changes everything,
and that is the asymmetry the tests need.

Four things are refused at construction rather than left to a test, because
they are the mistakes that would make the coverage claim meaningless:

- covering an id the catalogue does not have — a typo would otherwise read as
  a discharged obligation, and a retired rule's id is exactly such a typo;
- covering the same id twice — "appears once" has to mean once;
- covering an id that belongs to another audience — a Core rule wearing prose,
  or a speaking rule handed to the half with the tools, is what the split of
  audiences exists to prevent;
- claiming one of codex's stock lines without saying it word for word — the one
  place the freedom above does not apply, because there the wording is what was
  decided (#289 P1, #300).

**A set may be rendered without its headings.** The Voice hears prose and
nothing else (ADR 0018: 控制 voice 一定要用自然语言而不是代码语言), so its section
titles stay here as navigation for whoever reads the generator and never reach
the wire. That is a rendering choice rather than a second kind of set: the
blocks, the ids and the coverage claim are identical either way, and only the
`## ` in front of a title is at stake.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from gpt_voicecoding.core.instructions.catalogue import BY_ID, Audience


class InstructionError(Exception):
    """An instruction set cannot be generated, or does not say what it claims."""


@dataclass(frozen=True, slots=True)
class Block:
    """One passage of prose, and the rules it discharges.

    A block with no ids is connective tissue — a heading sentence, an example.
    It is allowed, and it proves nothing.
    """

    text: str
    covers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise InstructionError("an empty block covers nothing and says nothing")


@dataclass(frozen=True, slots=True)
class Section:
    """A titled run of blocks. Structure for the reader, not for the coverage test."""

    title: str
    blocks: tuple[Block, ...]

    def __post_init__(self) -> None:
        if not self.title.strip():
            raise InstructionError("a section needs a title the reader can navigate by")
        if not self.blocks:
            raise InstructionError(f"section {self.title!r} has nothing in it")


@dataclass(frozen=True, slots=True)
class InstructionSet:
    """One generated set of instructions: its prose, and the coverage it claims."""

    audience: Audience
    sections: tuple[Section, ...]
    #: Whether the section titles are rendered. False for a set whose reader is
    #: told to hear prose — the titles stay for whoever reads the generator.
    headings: bool = True
    covers: frozenset[str] = field(init=False)
    text: str = field(init=False)

    def __post_init__(self) -> None:
        if not self.audience.is_spoken:
            raise InstructionError(
                f"{self.audience} rules are carried by code, not by prose; generating a "
                "set for them would claim enforcement that is not there"
            )
        object.__setattr__(self, "covers", frozenset(self._claimed()))
        object.__setattr__(self, "text", self._rendered())
        self._stock_lines_are_there()

    def _claimed(self) -> tuple[str, ...]:
        claimed: list[str] = []
        for section in self.sections:
            for block in section.blocks:
                for rule_id in block.covers:
                    rule = BY_ID.get(rule_id)
                    if rule is None:
                        raise InstructionError(
                            f"{self.audience} instructions claim {rule_id!r}, which is not "
                            "a rule in the catalogue"
                        )
                    if rule.audience is not self.audience:
                        raise InstructionError(
                            f"{rule_id} is a {rule.audience} rule; the {self.audience} set "
                            "may not carry it"
                        )
                    if rule_id in claimed:
                        raise InstructionError(
                            f"{rule_id} is covered twice in the {self.audience} set; a rule "
                            "said twice is a rule that can be deleted once and still pass"
                        )
                    claimed.append(rule_id)
        return tuple(claimed)

    def _stock_lines_are_there(self) -> None:
        """A claimed stock line has to really be in the prose, word for word.

        The three refusals above are about ids, and for every rule this engine
        wrote itself that is the whole contract: the prose is free. codex's Stock
        Text is the exception (#289 P1) — what was decided there was to send
        codex's own wording at a pinned version, so the line *is* the obligation
        and its `gist` is the line.

        It is checked here rather than in a test because ids alone cannot see it:
        a block carries a whole bullet list, so a set with one bullet quietly
        deleted still claims every id it ever did and passes coverage (#300).
        """
        for rule_id in sorted(self.covers):
            rule = BY_ID[rule_id]
            if rule.is_stock and rule.gist not in self.text:
                raise InstructionError(
                    f"the {self.audience} set claims {rule_id} but does not say it: a stock "
                    f"rule's gist is codex's line at {rule.source}, and it is carried word "
                    "for word or not at all"
                )

    def _rendered(self) -> str:
        parts: list[str] = []
        for section in self.sections:
            if self.headings:
                parts.append(f"## {section.title}")
            parts.extend(block.text.strip() for block in section.blocks)
        return "\n\n".join(parts) + "\n"

    @property
    def size_in_bytes(self) -> int:
        """What a budget is measured in. Bytes, because tokens are made of them."""
        return len(self.text.encode("utf-8"))
