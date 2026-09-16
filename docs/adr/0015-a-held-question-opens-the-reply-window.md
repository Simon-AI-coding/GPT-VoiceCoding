# 15. A held question opens the Reply Window

Date: 2026-08-28 · Status: Accepted · Source: [#128](https://github.com/Simon-AI-coding/GPT-VoiceCoding/issues/128)

The public capability is the **Answer Relay**: it carries the user's own words, including an answer to a Session's question. The transport is selected inside the Claude adapter. Ordinary words use the Session inbox; while the listener still holds the exact `AskUserQuestion` prompt, the same `answer_relay` verb writes a framed denial message back through that hook because Claude consumes the message as the tool result. The Approval Relay remains only a permission verdict of `allow`, `deny`, or `ask`; questions never enter its pipeline, Control Plane document, command grammar, or Control Panel.

This supersedes ADR 0014's typed `answer` verdict and fills the question-route slot left by ADR 0013 decision 3. A question is not an approval, and making it one leaked Claude's wire mechanism into Bridge Core and every surface. The adapter now exposes only the live fact Core needs: whether one target's question is still answerable. A main Session's Reply Window is OPEN when it is IDLE, or when it is WAITING on a question and the listener still holds that question's writer. It is CLOSED for running work, permissions, child processes, and questions released by expiry, keyboard EOF, or shutdown. Session state remains WAITING while only the window changes.

Claude Code 2.1.248 does not provide a usable `prompt_id` on the `AskUserQuestion` `PermissionRequest`, although ordinary permission requests do. When the field is absent or empty, the listener mints an opaque correlator for its own parked-entry key and routes the Answer Relay back to that exact writer. The generated value is listener-private: `WaitingFor.approval_id` remains `None`, the roster reports what the wire actually supplied, and no generated id enters Bridge Core, the Control Plane, or the Approval Relay. Answerability is derived from the held writer, never from `approval_id` presence.

The question hold uses the configured `CorePolicy.approval_budget_seconds` as a shared resource ceiling without importing policy into the adapter. `BridgeCore.tick` passes that value to each Agent seam. The Claude listener timestamps parking on its injected clock, pops an expired question before writing `ask`, raises the existing `ReplyWindowChanged(CLOSED)`, and returns the release so Core can issue one terminal-only notice. A late Answer Relay is refused and never falls through to the inbox. Codex has no question hook and returns false and no releases.

The option canonicalisation boundary stays inside the adapter: whitespace and case are normalised only to recognise an offered label, which is then sent with its canonical spelling; any other user text is preserved verbatim inside the frame (`Ruling: …` — see the 2026-09-10 amendment). The hook acknowledgement remains the only positive delivery proof.

Legacy classification is per behaviour, as ruled by the Advisor for #128. Answering a Session's own question through a held hook, including the framed message, `approval_ack` receipt, and engine-minted correlator, is **new**: legacy had no approval transport and never answered a question remotely (`legacy@1d32845:bridge/daemon.py:1901-2052`). Refusing a Relay while its Reply Window is CLOSED is **ported** from `legacy@1d32845:bridge/coordinator.py:392,521-530`; opening that window only for an answerable question is **adapted** from the same boundary using #77's live measurement and the answerability qualification above. Stop Notice content keeps #75 and #77's existing port classification, with the terminal-versus-reply wording **adapted** from the same measurement. The parked-question ceiling reuses the Approval budget's existing classification rather than inventing another policy, while option-label canonicalisation is **new** because legacy has no such behaviour. Live Claude Code 2.1.248 measurements for #128 found that the on-screen question remains visible and keyboard-interactable while the hook is held, and that its question request supplies no usable `prompt_id`; no Claude version pin is introduced.

## Amendment 2026-09-01: the engine keeps no clock on a held hook

Source: [#172](https://github.com/Simon-AI-coding/GPT-VoiceCoding/issues/172), under map [#164](https://github.com/Simon-AI-coding/GPT-VoiceCoding/issues/164).

This ADR gave a parked question a hold ceiling: `CorePolicy.approval_budget_seconds`, passed
through `BridgeCore.tick` into `sweep_question_budget`, so the listener would pop an expired
question and write `ask` before Claude Code's own hook timeout did the same. The Approval Relay's
permission budget was the same number in a second place (`core/approvals.py`, `sweep_expired`),
with a never-deny `ask` fallback and a closing notice on expiry.

Both clocks are withdrawn. A held hook's life is bounded by the wire alone: Claude Code ends the
hook at the timeout the installed block declares (`installation/claude_hooks.py`,
`APPROVAL_TIMEOUT_SECONDS`), or when the human answers the dialog on screen — and the listener's
per-connection task already watches for that end and releases the parked entry
(`adapters/agent/claude/approval.py`, `_serve`). An engine-side timer was a second clock racing
the first, which is the objection the hook process itself records for keeping no clock of its own.
`approval_budget_seconds`, `sweep_question_budget` and `sweep_expired` are removed; nothing in
`CorePolicy` names a hold duration. Codex never needed a ceiling: its dialog stays answerable from
the TUI and the voice alike until one of them answers.

What a release *does* is unchanged: the Reply Window closes, the roster row keeps WAITING, a late
Answer Relay is refused. Only who decides *when* has moved to the wire. The one terminal-only notice
Core issued on an engine-timed release goes with the timer: the next Session Brief reads the row
without its handle and carries `answerable_here=false`, which is what the user is told when they ask.
The never-deny rule survives as a wire fact — a hook that ends without a verdict is silence, and
silence is the on-screen dialog's — not as engine policy.

## Amendment 2026-09-10: the frame is worded for a reader who is told it failed

Source: [#328](https://github.com/Simon-AI-coding/GPT-VoiceCoding/issues/328).

This ADR settled that an answer rides the held hook as a framed denial, because Claude
consumes a denial's message as the `AskUserQuestion` tool result. What it did not record is
what that costs on the surfaces a person and a model actually read: Claude Code renders a hook
denial as an error, so **this route's success is displayed as its failure**. The terminal prints
the delivered answer in red error styling under `Denied by PermissionRequest hook`, and the
Session's model receives the same bytes wrapped as a failed tool result. A refusal
(`DENIED_BY_VOICE`) and a delivered answer reach that renderer identically and come out
indistinguishable.

**There is no other shape to reach for, and that is now measured rather than assumed.** Read out
of the shipped 2.1.267 binary's own rejection text: `PermissionRequest decision must be
{"behavior": "allow"} or {"behavior": "deny", "message": "..."}`. `permissionDecision` remains
documented `(PreToolUse only)`, so #77's finding on 2.1.246 holds; its new fourth value `defer`
never reaches an interactive Session, the build logging `defer in interactive mode; ignoring
(defer is print-mode only)`. The strings `answer:` and `answer question` that sit beside these in
the binary are a pending dialog's display label, not a channel. The framing is Claude Code's and
this repo cannot take it back; the root fix is an upstream output that answers a question without
denying it, and asking for one is the only thing that retires this amendment.

**So the frame's wording is the whole of the remedy that reaches the model**, and it is chosen for
that reader rather than for clarity in the abstract. The prefix is `Ruling: `. It is positive: a
phrasing like *this is not an error* steers by prohibition, which makes the forbidden reading more
available rather than less. It is already this repo's word, carried by these ADRs and by #128's own
ruling, so the model meets it in the code, the docs and the tracker alike. And it is narrow — it
settles the question that was asked and claims nothing about the exchange, where *final* would
suppress the follow-up a Session is sometimes right to ask. It names neither the user nor this
product, because the `AskUserQuestion` call it answers and the channel the words arrived on carry
both already.

**The human is not left with the red line as their only signal**, and the reason is a decision
already taken elsewhere: ADR 0021's receipt is a reaction, and a delivered Answer Relay wears
`RelayReason.DELIVERED`'s 👌 on the user's own message (#321). Someone answering from the
Companion Channel is told it landed on the channel they are looking at. The terminal's red line is
seen by someone sitting at the terminal — who would have answered there. This is recorded because
its absence is what makes the red line look like an unsolved defect; the remaining reader it
misleads is the next person to open this repository, which is who this amendment is written for.

Unchanged: the framed denial itself (#128 — an answer is not a fourth `ApprovalVerdict` kind), the
canonicalisation boundary above, and `approval_ack` as the only positive delivery proof. Nothing
here may be re-graded on what a terminal renders.

## Amendment 2026-09-17: an answer is an `allow` carrying `answers`

Source: [#371](https://github.com/Simon-AI-coding/GPT-VoiceCoding/issues/371). Supersedes the
2026-09-10 amendment, and the "framed denial" wording in this ADR's opening and canonicalisation
paragraphs.

**There is another shape, and it is measured live.** On Claude Code 2.1.273 a throwaway
`PermissionRequest` hook answered an `AskUserQuestion` with `{"behavior": "allow", "updatedInput":
{...the call's input..., "answers": {"<question text>": "<words>"}}}`. The hooks reference documents
this: of `answers` it says *"Claude doesn't set this field; supply it via `updatedInput` to answer
programmatically"*. The transcript records `hook_permission_decision: allow`, the terminal prints
`User answered Claude's questions` and `Allowed by PermissionRequest hook` with no error styling, and
the model reads `The user answered: …` — or `Your questions have been answered: …` when every answer
is an offered label — and acts on free text as readily as on a label. The rejection text the
previous amendment relied on names the two *behaviours*, and an `allow` with `updatedInput` is one
of them. A `strict` permission kind (presumably `--restricted`, not verified) refuses any changed
input, and the dialog then stays on screen for the human.

So the held hook now answers with that shape. The engine's verdict frame carries the user's words
as `answer` beside `verdict: "allow"`; the hook, which already holds the call's input from its
stdin, builds `answers` (`approval.answered_input`). `Ruling: ` and the frame it named are retired:
there is no error wrapper left for a prefix to be read under.

- **Canonicalisation moves to the hook** and stays inside the adapter. For a lone question, words
  that match one of its labels — ignoring case, spacing and the `(recommended)` mark —
  become the label exactly as the Session wrote it, which is the comparison Claude Code makes
  before calling the answers a plain choice. Anything else is sent verbatim.
- **A call with several questions gets the words as its `response`**, and no `answers`. The words
  carry no per-question split, so the Session reads them whole — `The user responded: <words>` —
  and decides which part answers which question. Measured on 2.1.273: a reply of "第一个用 tabs，第二个用
  main" to a two-question call was split correctly by the model. No question is credited with words
  meant for another. A lone question with no text has no key to put the words under; the engine
  refuses that Relay before writing, and the dialog on screen keeps the question.
- **Permission verdicts are unchanged**: plain `allow`, or `deny` with `DENIED_BY_VOICE`, and never
  `updatedInput` or `updatedPermissions`. The hook ignores an `answer` on any frame but an `allow`
  for an `AskUserQuestion`, and prints nothing for it.

Unchanged: an answer is still not a fourth `ApprovalVerdict` kind at the seam (#128),
`approval_ack` is still the only positive delivery proof, and a late answer is still refused rather
than falling through to the inbox. `PROVEN_AGAINST_VERSION` stays where it is; this was a measurement
of the answer shape, not a re-measurement of the whole route.
