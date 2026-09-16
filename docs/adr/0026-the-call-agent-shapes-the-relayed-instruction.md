# 26. The Call Agent shapes the Relayed Instruction, in prompt alone; the engine carries words and adds no guard

Date: 2026-09-09 · Status: Accepted · Source: [#289](https://github.com/Simon-AI-coding/GPT-VoiceCoding/issues/289) on map [#283](https://github.com/Simon-AI-coding/GPT-VoiceCoding/issues/283); evidence [#284](https://github.com/Simon-AI-coding/GPT-VoiceCoding/issues/284), [#285](https://github.com/Simon-AI-coding/GPT-VoiceCoding/issues/285), [#287](https://github.com/Simon-AI-coding/GPT-VoiceCoding/issues/287), [#288](https://github.com/Simon-AI-coding/GPT-VoiceCoding/issues/288), [#294](https://github.com/Simon-AI-coding/GPT-VoiceCoding/issues/294), [#299](https://github.com/Simon-AI-coding/GPT-VoiceCoding/issues/299)

A Live Call is two models (ADR 0018), and a spoken instruction passes through both
before a Relay puts it into a Session. The map asked where it is shaped into the
**Relayed Instruction** (`CONTEXT.md`): by the Voice, the Call Agent, the engine, or
some combination. Seven tracer runs answered the first half before any rule was
written: the Voice authored `<input>` verbatim in 35 of 35 hand-offs (#288, #299; 28 of
28 on #287's wider corpus), while the Call Agent reworked what it relayed with no rule
either way — 4 of 4 Relays on the 09:54 baseline, one of them a byte-identical second
delivery. Our prompts, meanwhile, had displaced codex's own text outright and dropped
the lines codex wrote for exactly these failures (#294).

## Decision

**Shaping is the Call Agent's, and the rule is the Relayed Instruction.** The Voice is
told nothing about tidying and passes the user's words through; the Call Agent, which
stock already tells to read the transcript, gathers them into one complete instruction
in the user's own meaning and adds, expands, decides and chooses nothing. The engine
shapes nothing: `relay` carries the words it is given. In the catalogue this is one
rule, `agent.relay.carries-the-users-words` (source `issue/289`), and one retirement,
`voice.instruction.one-clean-instruction`. Both prompts are Stock Text with this
engine's Overlay after it, under the four principles recorded on #289 (2026-09-08) and
ADR 0018's amendment of the same day. Grading is Simon's, reading heard beside relayed
on the tracer; no grader model.

**Measured before written** (#299, 2026-09-09 10:48 NZST, the first run on the text that
ships, against the 09:54 baseline): hand-offs 6 for 6 distinct texts where the baseline
had 10 for 6; no text handed off twice; the Voice reworked 0 of 6; five Relays, none
repeated. One residual: the first Relay dropped a clause the user's self-correction
had left ambiguous (tennis, then badminton; the tennis was real). The rule already
forbids choosing on the user's behalf, so no clause is added for one sample; a second
loss of this shape in a graded run reopens the rule's wording, not the seam.

**`UserSpeech` keeps reading `input_transcript`.** Both fields arrive on the same
`handoff_request` notification, so the choice moves no token. `active_transcript` is a
delta since the last hand-off that carries ASR revisions as repeated lines and, on v3,
codex's own double append of a finished utterance (#285 §3.3); reading it means
writing revision-collapsing code for a consumer that does not exist — the three readers
of `UserSpeech` (the engine record, the unread-activity count, the Silence Ceiling's
activity stamp) parse none of its words. The one observed loss went the other way: a
hand-off whose `<input>` began mid-sentence while `active_transcript` was whole (#299,
12:28 run); the Call Agent, holding the transcript, relayed the whole. The drift risk
is real and outside the pin — the instruction that has the Voice author `<input>` is
server-side, in neither codex template (#294) — and its guard is the tracer's
Voice-reworked column, re-run whenever `STOCK_TEXT_VERSION` moves (#300's audit). A
non-zero count there, or a reader that parses the words, reopens this.

**No Relay dedupe in the engine.** #289's decision 3 reserved one: same Session, same
words, inside a configured window, refused with a reason. The evidence does not buy
it. Duplicate hand-offs are the backend's schedule — a sentence delegated as a prefix
before its own `turn.done` and whole after it, with no client lever (#285) — and
`relay` runs after the Call Agent's turn is spent, so an engine guard saves none of the
25–28k input tokens a hand-off costs; it saves only a Session turn. The one duplicate
Relay ever recorded was on the pre-stock text, `--supplement` to a `claude:` target,
which that adapter refuses before the wire; the stock text's three runs put eleven
Relays through without a repeat. The window would be an unmeasured number, and a
genuine repeat inside it would be refused. It remains the only guard on the route that
needs no model, so it returns the day a duplicate Relay appears on the shipping text in
a graded run — and that run's gap is the window.

**No Overlay line for a cut.** Stock's three supersession lines stand (#294); the
backend interrupts on voice onset unasked, and what fails is the next turn resuming a
report that sits in session context and cannot be retracted on v3 (#291, #292, #296).
The one observation on the shipping text — the user still speaking at 10:51:01, the
Voice answering the earlier words at 10:51:02 — is ungraded, and the long-read,
neutral-cut and spoken-hang-up scenarios were never run. P2 admits a line on evidence;
one datapoint is recorded here, not ruled on. Criterion 5 stays open on the map.

## Consequences

The cost lever is the number of hand-offs, not prompt length: a Call Agent turn is
25–28k input tokens, 90% or more cached, and this engine's rules are under 3% of it
(#289, #299). The Voice's own usage is unmeasurable on `rust-v0.153.4`, which parses no
realtime usage fields. What reopens this ADR is written beside each decision: a second
self-correction loss, a non-zero Voice-reworked count, a duplicate Relay on the
shipping text, or a graded cut.
