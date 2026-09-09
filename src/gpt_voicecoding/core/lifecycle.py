"""Where an Answer Relay stands, and why — the two closed vocabularies about one.

`Lifecycle` says where it is and `RelayReason` says why it is there. They live
together, and here rather than in `core/relays.py`, because both are read by
things the Relay pipeline is built *on*: the queue reads the lifecycle, and the
Session registry carries the reason of the last Relay to a Session that finally
failed (#197). A module that owns the pipeline cannot also own the vocabulary its
own dependencies answer in without inverting the dependency (ADR 0001).


- **RETAINED** — attempted or attempt-less, not delivered, and **still
  retryable**. A Relay waits here for the Session's Reply Window.
- **DELIVERED** — positively proven delivered. Terminal. Leaves the queue, so
  it cannot be re-attempted by anything.
- **REPORTED_FAILED** — reported to the user as terminal. **No automatic retry
  and no substitute action follows.** Also leaves the queue, so the retry
  boundary is structural rather than remembered.

**Three names, and it used to be five.** `PENDING` and `DROPPED` described a
notice waiting in a pipeline and a notice that pipeline gave up on, and no
notice waits anywhere since #195: a Stop reaches the Companion Channel in one
push that is graded and forgotten, and the voice side is the Call Keeper's,
which paces rather than queues. `is_terminal` and `is_retryable` went with them
— the retry boundary is which states leave the queue, and the queue is the one
thing that reads it (`core/relay_queue.py`).

This is deliberately *not* `seams.delivery.Delivery`, and the two must never be
compared. `Delivery` grades **one attempt** — what a single adapter call proved.
`Lifecycle` grades **the thing itself**. The reference implementation's worst
delivery bug was exactly this conflation: it read one attempt's grade as the
item's fate, retried an already-spoken notice, and opened duplicate calls.
"""

from __future__ import annotations

from enum import StrEnum


class Lifecycle(StrEnum):
    """Where a Relay stands. Two of the three are terminal."""

    RETAINED = "retained"
    DELIVERED = "delivered"
    REPORTED_FAILED = "reported_failed"


class RelayReason(StrEnum):
    """Why a Relay stands where it does. Closed, and the only thing said about it.

    This replaces seven English sentences and one inline apology. They were
    written in the Relay pipeline because a surface rendered them verbatim, which made Bridge
    Core the author of words the user hears — a second renderer beside the
    Voice, which re-renders whatever it is handed anyway (#175). A code says the
    fact; composing the sentence is the Voice's rule, in the instructions.

    **The proven/unproven pairs collapsed.** Two of these used to be four,
    because a sentence about a ceiling may not claim non-delivery of an
    `UNKNOWN` — the grade that means the far side may well have the words.
    A code claims nothing about arrival: a terminal code is a fact about what
    happened *here*, and the attempt's grade travels beside it.

    **`CEILING_PASSED` was the seventh and is retired** (#321). It said the
    words had waited past a wall clock of this system's own and been dropped —
    the one way the user's words were lost with nothing said about it. Queued
    words now wait for the Session's next turn or for its end, so the only
    terminal codes left are facts about the Session or the question.
    """

    #: The attempt proved the words reached the model. Nothing else does.
    DELIVERED = "delivered"
    #: The words wait, and may go again when the Session next takes a turn.
    #: Both grades that earn another attempt live here: nothing was sent, or an
    #: attempt **proved** nothing arrived.
    AWAITING_REPLY_WINDOW = "awaiting_reply_window"
    #: An attempt proved nothing either way, so the words are kept and never
    #: sent again on this system's own authority (P9). Saying them again is the
    #: user's to authorise.
    DUPLICATE_RISK = "duplicate_risk"
    #: The far side parked the words in front of a person. It settles on its
    #: own; a second copy is a second decision for the same human.
    HELD_FAR_SIDE = "held_far_side"
    #: Terminal: the Session those words were for ended while they waited.
    SESSION_ENDED = "session_ended"
    #: Terminal, and refused before the wire: the question is no longer
    #: answerable from here, so the words were never queued for an inbox that
    #: cannot take them (#68).
    QUESTION_UNANSWERABLE = "question_unanswerable"


class RelayAuthority(StrEnum):
    """What the route the words took made of them (ADR 0013 §3 and its amendment).

    **Here beside `RelayReason` rather than in the pipeline**, for the reason
    that put `RelayReason` here: a queued Relay holds one, and a queue that had
    to import the pipeline built on it would be a cycle (ADR 0001).

    Three cases, and the third is the one that earns a clause on the receipt.
    An answer through a question hook the lane still held (ADR 0015) and a
    permission verdict are **the user's own**. Words into an inbox are a peer's
    message: whether the Session acts on them is its call. That matters to the
    user only when there was a question to answer — a Session that merely
    *said* it, with no hook behind it — because "arrived" is then heard as
    "accepted as mine". Words for a Session that asked nothing carry no more
    authority, and nobody needs telling so.
    """

    #: The held-hook answer or the verdict: the user's own decision, carried whole.
    AS_THE_USER = "as_the_user"
    #: Plain text into a Session that ended its turn on a question it merely
    #: said. The words travel; the user's say-so does not (ADR 0013 §3).
    WORDS_ON_A_QUESTION = "words_on_a_question"
    #: Plain text into a Session that asked nothing — an instruction, a supplement.
    WORDS = "words"
