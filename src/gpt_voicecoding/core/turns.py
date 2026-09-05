"""Delegated Turns that run beside the dispatch loop rather than inside it.

A Delegated Turn is a coding model thinking: the top-level `>` and every
Assistant Conversation reply (ADR 0021 §7), bounded only by
`delegated_turn_timeout_seconds` — five minutes by default. Awaiting one inside
`BridgeCore.dispatch` made the hub's one-event-at-a-time loop hold every other
event behind it for those minutes: a Stop Notice for a Session that just hit a
permission prompt, a numeral verdict, an Answer Relay, a `/status` (#268).

So a turn is a task this holds, and the dispatch that started it returns at once.
When the task ends it raises `DelegatedTurnFinished` on Core's own queue, and the
serial loop — the only thing that mutates state (ADR 0001) — sends the answer,
registers the Anchor and draws the reply bar there. Nothing about routing or the
Call seam changes; only *when* Core is holding the loop does. The Anchor Table
gains one thing — it says when it has forgotten a conversation, so a queue held
here goes with the rows rather than outliving them.

**Per conversation the turns stay strictly sequential and in order.** Replies
typed to one conversation while its turn runs are queued here first-in-first-out
and each becomes its own turn as the one before it ends, so this hub never issues
a second `turn/start` on a busy thread and never has to know what codex would say
if it did. No cap, no coalescing and no refusal (Simon, 2026-09-06): the user may
send several messages and each is answered, in the order typed. Different
conversations hold different queues and run at the same time.

**The top-level `>` is one more conversation — the nameless one.** It resumes no
thread, so nothing makes two of them a sequence except the rule that one turn is
enough: each `>` starts a fresh thread with `approvalPolicy = "never"` in
`danger-full-access` (`adapters/call/realtime/adapter.py`), and the dispatch loop
used to be what kept ten of them typed in a row from running as ten simultaneous
approval-free agents on the user's machine. Queueing them behind each other
restores that bound with the rule that is already here, and no cap and no setting
(ruled on #268, 2026-09-06). None is refused and none is dropped; each still runs
on its own fresh thread, one after another.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from gpt_voicecoding.core.router import Classification
from gpt_voicecoding.seams.events import Event, EventSink

_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DelegatedAnswer:
    """What one Delegated Turn came back with, and the one fact its words hide.

    `text` is what the user is shown either way — the coding model's answer, or
    the failure's own words (ADR 0021 §7). `thread_gone` is the one outcome that
    is not words to show: the conversation itself has ended, and Bridge Core
    answers that with its own fixed hint and anchors nothing. Shaped after
    `ControlAnswer` for the same reason it has an `ok`: a fact that cannot be
    recovered from prose has to travel beside it.

    An empty `text` with no `thread_gone` is the turn that raised something
    nobody classified: there is nothing honest to show the user, so nothing is
    sent (`_reply` returns on empty text) and only the log carries it.
    """

    text: str = ""
    thread_gone: bool = False


@dataclass(frozen=True, slots=True)
class DelegatedTurnFinished(Event):
    """A turn that ran off the loop has an answer. Core's own event, on Core's own queue.

    The only event in this system a seam does not raise. It travels the same way
    every other one does deliberately: the answer is sent, the Anchor registered
    and the reply bar drawn by the serial dispatch loop, at a moment when nothing
    else is mutating state — which is what an awaited turn used to get by holding
    the loop, and what a task writing state from beside it would lose.

    It carries the `Classification` the turn was started from, because that is
    where the thread to anchor on and the answer's shape are read (a top-level
    `>` names no thread and registers no row), and the `origin` the request came
    in on, because every answer goes back the way the text came (ADR 0021 §4).
    """

    found: Classification
    origin: str
    answer: DelegatedAnswer


#: The key the top-level `>` queues under. Not a thread id and never sent to a
#: seam: `Classification.thread_id` is empty for a `>`, and this is what that
#: emptiness means here — the one conversation with no thread of its own, whose
#: turns are sequential like any other's (ruled on #268).
THE_NAMELESS_ONE = ""


@dataclass(slots=True)
class _Conversation:
    """One conversation's turn in flight, and the replies typed behind it."""

    running: asyncio.Task[None]
    waiting: deque[tuple[Classification, str]] = field(default_factory=deque)


class DelegatedTurns:
    """Every Delegated Turn in flight, and what waits behind each conversation.

    Memory only, and deliberately so: a queue of replies is worth exactly as
    much as the process that holds the threads they resume, and a restart has
    neither.
    """

    def __init__(
        self,
        *,
        run: Callable[[Classification], Awaitable[DelegatedAnswer]],
        events: EventSink,
    ) -> None:
        self._run = run
        self._events = events
        #: Keyed by thread id, with `THE_NAMELESS_ONE` for the top-level `>`.
        self._threads: dict[str, _Conversation] = {}

    def submit(self, found: Classification, origin: str) -> None:
        """Start this turn now, or queue it behind the one already on its conversation."""
        thread_id = found.thread_id
        conversation = self._threads.get(thread_id)
        if conversation is None:
            self._threads[thread_id] = _Conversation(running=self._task(found, origin))
            return
        conversation.waiting.append((found, origin))
        _log.info(
            "a reply is queued behind the turn running on that conversation (%d waiting)",
            len(conversation.waiting),
        )

    def ended(self, thread_id: str) -> None:
        """That thread's answer has been sent. Start the next reply waiting, if any.

        Called from the dispatch loop rather than from the task itself, so the
        next `thread/resume` is issued only once the answer before it has left —
        which is what makes the answers arrive in the order the replies were
        typed, and not merely the turns.
        """
        conversation = self._threads.pop(thread_id, None)
        if conversation is None or not conversation.waiting:
            return
        found, origin = conversation.waiting.popleft()
        conversation.running = self._task(found, origin)
        self._threads[thread_id] = conversation

    def drop(self, thread_id: str) -> None:
        """The conversation is gone: whatever was typed behind it goes with it.

        The same event that drops the Anchor rows (`AnchorTable.drop`). A reply
        still waiting here is a reply to a thread that cannot be resumed, and
        running it would earn the same start-again hint again, once per queued
        message (#268).
        """
        self._threads.pop(thread_id, None)

    def in_flight(self) -> int:
        """How many turns are running right now. Nothing in the engine reads it.

        A turn whose task has finished is not one of them, even while its row is
        still here: the row stands until the answer has been dispatched, and a
        caller that counted it would be waiting on something already ended.
        """
        return sum(1 for task in self._in_flight() if not task.done())

    async def settle(self) -> None:
        """Wait for every turn in flight to end. The test harness's clock-free wait.

        The engine never calls this: its dispatch loop is running, so a turn's
        answer arrives on its own. A test that drives `drain` by hand has no such
        loop, and waiting on the tasks is how it runs a turn to its end without
        sleeping on a real one.
        """
        await self._all(self._in_flight())

    async def aclose(self) -> None:
        """Cancel every turn in flight and discard every queue. No answer is sent.

        Cancellation is what reaches the Call adapter's own teardown — it
        interrupts the turn and unsubscribes from the thread in its `finally`
        (`adapters/call/realtime/adapter.py::_retire`) — so a turn abandoned by a
        shutdown does not go on acting on the user's machine. A cancelled task
        raises no `DelegatedTurnFinished`, and the loop that would dispatch one
        is already gone.
        """
        running = self._in_flight()
        self._threads.clear()
        for task in running:
            task.cancel()
        await self._all(running)

    def _in_flight(self) -> tuple[asyncio.Task[None], ...]:
        return tuple(held.running for held in self._threads.values())

    async def _all(self, tasks: tuple[asyncio.Task[None], ...]) -> None:
        """Wait for these, together rather than one after another.

        Together, because each one unwinding into the Call adapter's teardown
        costs up to two request timeouts against an app-server that may be the
        reason this engine is stopping — and a shutdown that paid them in series
        would sit for that many multiples before the socket and the adapters
        were even reached, which is the phase-by-phase hang the log lines around
        `Engine.aclose` exist to name. `return_exceptions` is what makes a
        cancelled task an ordinary result here; a cancellation delivered to
        *this* caller still comes out, which is how `Engine.run` keeps the one
        it is unwinding from.
        """
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _task(self, found: Classification, origin: str) -> asyncio.Task[None]:
        return asyncio.create_task(
            self._turn(found, origin), name=f"delegated-turn-{found.thread_id or 'top-level'}"
        )

    async def _turn(self, found: Classification, origin: str) -> None:
        """One turn, off the loop, ending in the one event that puts it back on it.

        **A turn that raises something nobody classified is still an ending.**
        The failures the design names come back as words in a `DelegatedAnswer`
        (the composition root's `delegate` closure); what reaches here is
        whatever that closure did not classify — including a wire failure out of
        the one-shot `thread/start`, which is unguarded today
        (`adapters/call/realtime/adapter.py::delegate`). There is nothing honest
        to show for one, so nothing is sent and the log carries it — which is
        what the awaited turn did too, by letting it reach the dispatch loop's
        own handler. What is new is only that the thread moves on: a queue that
        stopped here would leave every reply behind it unanswered, with no way
        to notice.
        """
        try:
            answer = await self._run(found)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("a Delegated Turn raised; nothing is sent and the thread moves on")
            answer = DelegatedAnswer()
        self._events.emit(DelegatedTurnFinished(found=found, origin=origin, answer=answer))
