# 23. The engine decides whether and when the Voice speaks; prose decides how

Date: 2026-09-07 · Status: Accepted · Amended 2026-09-09 (below) · Source: #274

System-dialled calls connected and then stayed silent: the dial-time hand-over
is silent background (ADR 0018), while the Voice's prose said the person had
opened the call and it should wait. That paragraph was itself a fix for an
earlier silence (#194). Rewording who opened the call leaves the decision with
the model; Bridge Core owns that decision (ADR 0001).

The Call Keeper remembers whether it dialled for the system. On `CallStarted`
it plays the connected Cue and asks Briefing for the opening, once, under the
Duty and Voice Switches. A user-opened call gets the Cue only. Briefing reads
the live roster at that moment (ADR 0017): one waiting Session gets its brief;
several get the Focus Session's brief followed by the Roster Brief, or the
Roster Brief alone without a waiting Focus Session; none gets silence. Mid-call
the existing Focus-only speech and non-Focus EVENT Cue rules stand.

One Briefer reading names the occasion: silent hand-over, opening, or mid-call.
The Call seam carries ordered briefs in one `speak`, mapped to one append so a
second append cannot truncate the first. Only its delivered receipt restarts
the Silence Ceiling (#225). `DialReason` is retired; the Voice gets one rule on
every call: speak what the engine hands it, in the given shape and order, and
otherwise wait to be spoken to. The retired paragraph covered no catalogue
rule; the Session Brief, Roster Brief and language rules remain where they were.

Both lanes' stopped turns use Briefing's existing question heuristic, after
structured questions and permissions. Claude has no message phases, so its
newest assistant message within the turn supplies the words; Codex keeps its
final-answer requirement. Missing words remain a decision, statements finish,
and prose never creates a question/options/recommendation payload.

The shared naming function chooses the task from each lane's existing facts.
The identity seam's `SessionName` crosses the Call and Companion Channel seams
whole, with the address fallback unchanged. Renderers retain its own spelling;
the Voice additionally receives labelled project and task facts. Menu labels
still carry addresses for duplicate names, separately from the name itself.
Task choice, frozen-name/rename rules, Telegram layout and spoken-name matching
are unchanged; changing what the task should mean belongs to #273.

Legacy (ADR 0010): **adapted** from `legacy@1d32845:bridge/host.py:226-234`'s
finished-turn reading to the common question rule. Legacy did not dial codex
realtime (`bridge/livecall.py:77-109`, ADR 0018), so it has no opening act of
this kind to port. This change leaves real-environment acceptance untouched;
the user explicitly excluded running it from this implementation.

## Amendment 2026-09-09: a roster of one has no other Session to ring about

Source: `engine.log` 2026-09-09 09:54:18–09:56:23 on the reference machine; fixed in `f634d00`.

"Mid-call the existing Focus-only speech and non-Focus EVENT Cue rules stand" held on one reading
only. The rules are two and the flag that selects them is one boolean, so a roster with **no Focus
Session at all** took the Cue branch exactly as a roster whose focus is some *other* Session does.
On the reference machine that roster held one Session: it stopped at 09:54:18 waiting for a
decision, the EVENT Cue played at 09:54:20, and the call stayed up until 09:56:23 without a word
said into it. Neither an Answer Relay nor an Approval Relay ran that day — the user was answering
by typing into that Session's own terminal, which no surface sees and which therefore never sets
the focus. So the ring named a Session the user could not tell apart from the one in front of
them, and the word owed waited on a reply they had no reason to send.

Mid-call speech now reads the Session **spoken first**: the Focus Session when there is one, and
otherwise the sole live Session — Child Processes not counted, because a Session that spawned a
subagent has not become two Sessions to choose between (#68). A held focus still decides alone;
the roster is read only for the empty case. The Focus Session itself is unchanged and settable by
nothing but a reply (#165 Q2); what parted from it is what the voice *does* with that record. Both
halves of the decision read the one notion — the Keeper's wake, which arms the word, and the
Briefer, which pays it — because arming one without the other leaves the word owed and never said,
which is the shape the first attempt at this fix had.

One consequence, and it follows from the Silence Ceiling rather than from anything decided here:
on a roster of one a Stop is now spoken, and a Session Brief handed to the voice starts the silent
stretch afresh, so a call that used to hang up on that news is held open one stretch longer.
