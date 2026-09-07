# 23. The engine decides whether and when the Voice speaks; prose decides how

Date: 2026-09-07 · Status: Accepted · Source: #274

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
