"""Measure real `brief` and `history` renderings against codex's 1,000-token cut (#297).

No call. Renders through the engine's own code — `briefing.text` for a brief,
`control_plane.commands._history_lines` for a history page — over the real
Claude transcripts on this machine, and counts tokens with codex's own
estimator (`ceil(utf8_bytes / 4)`, `codex-rs/utils/string/src/truncate.rs:71-74`).
"""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from gpt_voicecoding.adapters.agent._progress import page
from gpt_voicecoding.adapters.agent.claude import transcript_tail
from gpt_voicecoding.control_plane.commands import _history_lines
from gpt_voicecoding.control_plane.progress_publication import ProgressPublication
from gpt_voicecoding.core import briefing
from gpt_voicecoding.core.policy import DEFAULT_HISTORY_PAGE_ENTRIES
from gpt_voicecoding.core.sessions import Session
from gpt_voicecoding.seams.agent import (
    HistoryPage,
    ProgressEntry,
    ProgressObservation,
    ProgressOmission,
    ProgressRole,
    SessionState,
)
from gpt_voicecoding.seams.identity import AgentKind, SessionTarget

# codex's estimator, byte for byte.
APPROX_BYTES_PER_TOKEN = 4
BUDGET_TOKENS = 1_000
# `realtime_backend_item` prepends `codexResponseItemPrefix` as "{prefix}\n\n{text}".
ITEM_PREFIX = "[AGENT] \n\n"


def tokens(text: str) -> int:
    return -(-len(text.encode("utf-8")) // APPROX_BYTES_PER_TOKEN)


def _truncate_with_byte_estimate(text: str, max_bytes: int) -> str:
    """`truncate_with_byte_estimate(.., use_tokens=true)`, truncate.rs:37-67."""
    data = text.encode("utf-8")
    if not data:
        return ""
    if max_bytes == 0:
        return f"…{-(-len(data) // APPROX_BYTES_PER_TOKEN)} tokens truncated…"
    if len(data) <= max_bytes:
        return text
    left_budget = max_bytes // 2
    right_budget = max_bytes - left_budget
    tail_start_target = len(data) - right_budget
    prefix_end, suffix_start, started = 0, len(data), False
    index = 0
    for char in text:
        width = len(char.encode("utf-8"))
        char_end = index + width
        if char_end <= left_budget:
            prefix_end = char_end
        elif index >= tail_start_target:
            if not started:
                suffix_start, started = index, True
        index = char_end
    suffix_start = max(suffix_start, prefix_end)
    removed = -(-(len(data) - max_bytes) // APPROX_BYTES_PER_TOKEN)
    marker = f"…{removed} tokens truncated…"
    return data[:prefix_end].decode("utf-8") + marker + data[suffix_start:].decode("utf-8")


def codex_truncate(text: str, budget: int = BUDGET_TOKENS) -> str:
    """`truncate_realtime_text_to_token_budget`, realtime_context.rs:318-338."""
    truncation_budget = budget
    while True:
        candidate = _truncate_with_byte_estimate(text, truncation_budget * APPROX_BYTES_PER_TOKEN)
        if tokens(candidate) <= budget:
            return candidate
        excess = tokens(candidate) - budget
        next_budget = max(truncation_budget - max(excess, 1), 0)
        if next_budget == 0:
            candidate = _truncate_with_byte_estimate(text, 0)
            return candidate if tokens(candidate) <= budget else ""
        truncation_budget = next_budget


def records(path: Path) -> list[dict]:
    out = []
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def brief_text(entry, *, name: str, workspace: Path) -> str:
    observation = ProgressObservation.from_capture(
        recent=(entry,),
        omission=ProgressOmission.NONE,
        read_at=datetime.now(UTC),
    )
    session = Session(
        target=SessionTarget(agent=AgentKind.CLAUDE, session_id="probe", pid=1234),
        workspace=workspace,
        first_seen=0.0,
        name=name,
        state=SessionState.IDLE,
        progress=observation,
    )
    return briefing.text(briefing.session(session))


def roster_text(pairs) -> str:
    sessions = tuple(
        Session(
            target=SessionTarget(
                agent=AgentKind.CLAUDE, session_id=f"probe-{index}", pid=1000 + index
            ),
            workspace=path.parent,
            first_seen=float(index),
            name=f"probe session {index}",
            state=SessionState.IDLE,
            progress=ProgressObservation.from_capture(
                recent=(entry,),
                omission=ProgressOmission.NONE,
                read_at=datetime.now(UTC),
            ),
        )
        for index, (entry, path) in enumerate(pairs)
    )
    focus = sessions[0].target if sessions else None
    return briefing.text(briefing.roster(sessions, focus))


def main() -> int:
    publication = ProgressPublication()
    transcripts = sorted(Path.home().joinpath(".claude/projects").rglob("*.jsonl"))
    print(f"transcripts on this machine: {len(transcripts)}")

    brief_tokens: list[int] = []
    page_tokens: list[int] = []
    real_entries: list = []
    widest_brief = (0, "", None)
    widest_page = (0, "", None)
    widest_entry = (0, None, None)
    scanned = 0

    for path in transcripts:
        try:
            entries = transcript_tail.visible(records(path))
        except Exception:
            continue
        if not entries:
            continue
        scanned += 1

        newest = entries[-1]
        if len(newest.text.encode("utf-8")) > widest_entry[0]:
            widest_entry = (len(newest.text.encode("utf-8")), path, newest)

        real_entries.append((newest, path))
        rendered = brief_text(newest, name="the probe session", workspace=path.parent)
        brief_tokens.append(tokens(ITEM_PREFIX + rendered))
        if tokens(rendered) > widest_brief[0]:
            widest_brief = (tokens(rendered), rendered, path)

        window = page(
            entries,
            before=None,
            count=DEFAULT_HISTORY_PAGE_ENTRIES,
            read_at=datetime.now(UTC),
        )
        document = publication.history_document(window)
        page_text = "\n".join(_history_lines(document))
        page_tokens.append(tokens(ITEM_PREFIX + page_text))
        if tokens(page_text) > widest_page[0]:
            widest_page = (tokens(page_text), page_text, document)

    print(f"transcripts with at least one visible entry: {scanned}")
    print()
    print("== largest observed ==")
    print(f"widest newest entry, bytes: {widest_entry[0]}  ({widest_entry[1]})")
    print(
        f"widest brief:        {widest_brief[0]} tokens "
        f"({len(widest_brief[1].encode('utf-8'))} bytes)  {widest_brief[2]}"
    )
    print(f"  + item prefix:     {tokens(ITEM_PREFIX + widest_brief[1])} tokens")
    print(f"  crosses {BUDGET_TOKENS}? {tokens(ITEM_PREFIX + widest_brief[1]) > BUDGET_TOKENS}")
    print(
        f"widest 5-entry page: {widest_page[0]} tokens "
        f"({len(widest_page[1].encode('utf-8'))} bytes)"
    )
    print(f"  + item prefix:     {tokens(ITEM_PREFIX + widest_page[1])} tokens")
    print(f"  crosses {BUDGET_TOKENS}? {tokens(ITEM_PREFIX + widest_page[1]) > BUDGET_TOKENS}")
    print()

    def share(values: list[int]) -> str:
        over = sum(1 for value in values if value > BUDGET_TOKENS)
        median = sorted(values)[len(values) // 2]
        return (
            f"{over}/{len(values)} over budget "
            f"({100 * over / len(values):.1f}%), median {median} tokens"
        )

    print("== how often a real one crosses ==")
    print(f"single-session brief: {share(brief_tokens)}")
    print(f"five-entry history:   {share(page_tokens)}")
    print()

    print("== roster brief against the number of Sessions ==")
    pool = sorted(real_entries, key=lambda pair: -len(pair[0].text.encode("utf-8")))
    for count in (1, 2, 3, 5, 8, 13, 21):
        text = roster_text(pool[:count])
        print(
            f"  {count:>3} Sessions: {tokens(ITEM_PREFIX + text):>6} tokens "
            f"({len(text.encode('utf-8')):>7} bytes)  crosses? "
            f"{tokens(ITEM_PREFIX + text) > BUDGET_TOKENS}"
        )
    print()

    print("== what the cut leaves, on the widest observed page ==")
    cut = codex_truncate(ITEM_PREFIX + widest_page[1])
    print(f"  after the cut: {tokens(cut)} tokens, {len(cut.encode('utf-8'))} bytes")
    print(f"  head, first 200 bytes: {cut.encode('utf-8')[:200]!r}")
    marker_at = cut.find(" tokens truncated…")
    start = cut.rfind("…", 0, marker_at)
    print(f"  marker: {cut[start : marker_at + len(' tokens truncated…')]!r}")
    print(f"  tail, last 200 bytes:  {cut.encode('utf-8')[-200:]!r}")
    print()

    print("== the engine's own ceilings ==")
    print(f"Control Plane reply ceiling, bytes: {publication.max_bytes}")
    print(f"largest publishable page, entry slots: {publication.largest_page}")
    print(f"capture ceiling, bytes: {publication.capture.max_bytes}")

    # A brief whose `newest` fills the capture ceiling exactly.
    ceiling_entry = replace(widest_entry[2], text="x" * publication.capture.max_bytes)
    ceiling_brief = brief_text(ceiling_entry, name="the probe session", workspace=Path("."))
    print(f"largest possible brief:   {tokens(ITEM_PREFIX + ceiling_brief)} tokens")

    # A five-entry page filled until the Reply stops fitting.
    size = publication.capture.max_bytes // DEFAULT_HISTORY_PAGE_ENTRIES
    filled = tuple(
        ProgressEntry(ordinal=index, role=ProgressRole.ASSISTANT, text="x" * size)
        for index in range(DEFAULT_HISTORY_PAGE_ENTRIES)
    )
    window = HistoryPage(entries=tuple(reversed(filled)), older=True, read_at=datetime.now(UTC))
    ceiling_page = "\n".join(_history_lines(publication.history_document(window)))
    print(f"largest possible 5-entry page: {tokens(ITEM_PREFIX + ceiling_page)} tokens")

    # Where a roster brief would cross, if this machine ever ran that many.
    count = 1
    while tokens(ITEM_PREFIX + roster_text(pool[:1] * count)) <= BUDGET_TOKENS:
        count += 1
    print(f"a roster brief crosses {BUDGET_TOKENS} tokens at {count} Sessions")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
