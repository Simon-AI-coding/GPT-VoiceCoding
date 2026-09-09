# GPT-VoiceCoding

A bridge over every coding-agent Session on the user's Mac: it tells the user what each Session is doing and what it stopped on, carries the user's words into it, and calls the user when a Session needs a decision.

## Language

### Voice side

**Live Call**:
The system-owned realtime voice call — the system's one and only voice surface.
_Avoid_: Live thread, voice chat, call (unqualified)

**Live Toggle**:
The single action that starts a Live Call when none is up, or ends the current one.
_Avoid_: toggle phrase

**Call Keeper**:
The part of Bridge Core that keeps the Live Call's time: when a call is dialled, when it ends, and when the system may ring or speak into it. It knows nothing of what is said — it asks for a fresh reading at the moment it decides to sound.
_Avoid_: interlock (the mechanism it grew from), call manager, scheduler

**Silence Ceiling**:
The automatic hang-up: a call ends after a configured stretch in which neither the user spoke nor the call's own voice sounded. Time spent while the voice is speaking does not count. Handing the voice a Session Brief starts the stretch afresh — words in the voice's hands are the call working, not the call silent — but it is only that: the voice is not treated as speaking, so a voice that never begins still ends the call one full stretch later. Governed by the Auto Hang-up Switch.
_Avoid_: idle timeout, inactivity timer

**Session Brief**:
What the system knows about one Session, structured for telling the user: its name, its agent, its state (waiting for a decision, requesting permission, finished, running, or unreadable), its newest message, and the decision it is waiting on — the question with its options and any recommendation, or the tool awaiting permission with a one-line summary — and, when the user's last reply to it never arrived, that it did not and why. The summary the user hears and the detail they may ask for are one and the same facts.
_Avoid_: 单项目简报, notice (unqualified), stop detail

**Roster Brief**:
The count of Sessions in each state, with one header row per live Session. Spoken when several Sessions need the user, or on request; on the Companion Channel it is the Session list the menu shows, an Anchor whose choices are the live Sessions.
_Avoid_: 多项目简报, overview, summary

**Focus Session**:
The one Session the user last replied to — by Answer Relay or Approval Relay. Its news is spoken first; another Session's news only rings. Cleared when it ends; never set by merely asking about a Session. When there is none, the sole live Session is spoken about in its place: a ring says *another* Session wants the user, and a roster of one has no other — the Session the user is sitting at replies by typing into it, which no surface sees and which therefore never sets the focus (amended 2026-09-09). A held focus still decides alone; the roster is read only for the empty case. A voice-side notion only: on the Companion Channel, where messages lie flat, the newest Anchor takes its place.
_Avoid_: current session, active session, last session

**Detail**:
The full form of a Session Brief's facts, given when the user asks: the newest message whole (content detail) and the decision point whole — question, every option, the recommendation (decision detail).
_Avoid_: stop detail, expansion

**History**:
What one Session said and was told, read on request in pages of a configured size (default five entries, both sides counted, newest first). Asked without a cursor it is the newest page, and that page includes the newest message: every page is complete on its own. A page names each entry's place in the Session's record so the next request can ask for the entries before it; an entry too large to carry is named as omitted, never dropped without a word. The page size is a count; the wire's byte ceiling stays a ceiling.
_Avoid_: progress (the retired verb), transcript, log, tail

**Stop Notice**:
A Session Brief published as text — what the Companion Channel receives whenever a Session stops, whatever it stopped on, with the Session's newest message carried whole when the surface can hold it and cut with a marker that says so when it cannot. It is an Anchor; once the decision it carried can no longer be answered from here it is marked as handled where it was sent, and it stays a reply target for the user's words. The Live Call does not receive text to read out; it receives the Session Brief itself and speaks from it.
_Avoid_: announcement (the act, not the thing)

**Cool-down**:
The minimum interval between two sounds the system makes toward the user on the voice side: after any end of a call — hung up, dropped, or a dial that failed — it does not dial again; during a call it does not ring or speak unbidden again. A Session event inside it only marks that a call, or a word, is owed; when it ends, the system reads the Sessions afresh and dials, speaks, or stays quiet on what it finds — never on replayed events. The user's own Live Toggle is not subject to it.
_Avoid_: debounce, back-off, grace period, rate limit

**Cue**:
A short sound played on the user's own speakers to mark a moment of the Live Call rather than to carry words: the call connected, the call ended, or — mid-call — something happened. The term names the moment; which notes are heard for it belong to the Call adapter, and were chosen by ear. Played by the engine on the configured output device, on its own stream, so the sound for a call that has ended does not depend on that call's audio path still being open.
_Avoid_: tone, beep, chime, earcon, notification sound

**Playout**:
The span of the Voice's audio still being played out after the model has sent its last frame of it. The jitter buffer, the engine's own playback buffer and the output device all sit between that frame and the last audible sample, so the Voice has not stopped speaking until that audio has been heard — which is the fact the system waits for before saying it has. The span ends when the audio the Voice had generated has had time to be heard, measured from how much of it the engine was still holding when the Voice stopped generating; audio arriving after that is the far side padding its stream, not the Voice speaking. What the engine records about one span, at the moment it ends and again when the wait for it runs out: how much audio the span began with, how many inbound frames arrived, how long ago the last one did, the longest silence between two of them, how much audio is still queued for the device, and whether the span closed on that rule or on the wait's own bound running out.
_Avoid_: drain, playback (the buffer, not the span), audio tail

**Voice**:
The Live Call's speaking half — the model the user hears and talks to. It has no tools: it composes speech from what the engine hands it, and hands anything that reads as a job to the Call Agent. The user speaks to it, and the engine feeds it, in natural language, never in code-like text; the prompt that shapes it is codex's Stock Text with this engine's Overlay after it (ADR 0018, amended 2026-09-08).
_Avoid_: voice model, realtime model, assistant (unqualified), voice thread

**Call Agent**:
The Live Call's acting half — the coding model behind the Voice and the only one on the call with tools. It runs the control-plane verbs the Voice hands it. Not a Delegated Turn, which is work the system hands out on purpose.
_Avoid_: backing Codex model, the agent behind the call, delegate

**Stock Text**:
The instructions codex itself ships for the Voice and for the Call Agent, taken whole from a named codex version. On this engine's calls each half hears its Stock Text first, in codex's own form and wording; a stock line is dropped or changed only when the evidence of this wire falsifies it or a requirement of this system conflicts with it.
_Avoid_: default prompt (unqualified), codex prompt, base prompt

**Overlay**:
What this engine adds after a half's Stock Text: who the Voice is and how it speaks, the engine's verbs and shapes for the Call Agent, and the few rules of this system's own. Where the two meet, a rule about mechanism keeps the Stock Text's wording and a rule about identity is the Overlay's.
_Avoid_: house rules, our prompt, custom instructions

**Delegated Turn**:
Work the system hands to a coding model on the user's behalf, asked for from any surface — during a Live Call, distinct from the call's own speech, or from the Companion Channel. Its model is a user-facing setting, one for every surface.
_Avoid_: side request, background query

### Control side

**Bridge Core**:
The one decision-maker: it owns every policy and holds the system's single source of truth. It decides; the modules around it do.
_Avoid_: Bridge Control Center, supervisor, orchestrator, engine (the process, not the role)

**Duty Switch**:
The master on/off switch: off means the system does not speak, does not ring, does not push, and does not touch the Live Call; events are still recorded. The Silence Ceiling still applies — it is the call's own limit, not an act toward the user. The Voice and Message Switches and every Feature Switch are effective only while it is on; the Auto Hang-up Switch stands beside it, not under it.
_Avoid_: duty mode, pause mode, do-not-disturb

**Voice Switch**:
Whether the system may speak into, open, or otherwise touch the Live Call.
_Avoid_: speech mode, live switch

**Message Switch**:
Whether the system may push messages through the Companion Channel. Independent of the Voice Switch.
_Avoid_: notification switch, push switch

**Auto Hang-up Switch**:
Whether the Silence Ceiling ends a call. On by default. It stands beside the Duty Switch, under nothing: the ceiling is the call's own limit, not an act toward the user, so it holds with Duty off and on calls the user opened.
_Avoid_: auto-end flag, feature switch (it has no parent)

**Feature Switch**:
An independent on/off setting for one capability — a flat boolean under its parent switch.
_Avoid_: mode, profile, sub-mode

**Control Plane**:
Status queries and switch flips, accepted from every surface and never gated by any switch.
_Avoid_: admin commands, management interface

**Control Panel**:
The at-computer surface for seeing the system's current state — the Session roster included — and flipping switches, plus the shell-owned Companion Channel credential. Runtime state and that one write-only credential, not installation settings.
_Avoid_: settings app, preferences window, config tool

**Installation**:
Everything the system places in files the **user** owns so the coding agents can reach it, and takes back byte for byte when asked. Done at first launch and reconciled at every launch after (ADR 0012), never by hand and never by the Control Panel.
_Avoid_: setup, configuration (that is the user's own file, which the system only reads), provisioning

### Reach and sessions

**Companion Channel**:
The pluggable text surface: it pushes the system's messages to the user and accepts their inbound text, saying which of its own messages that text answered when the user replied to one. It reports facts about a message — which one it answered, which ids it landed under — and never an opinion about what the text means.
_Avoid_: Telegram (one adapter, not the concept)

**Anchor**:
One message the system sent through the Companion Channel that names a single target — a Session or an Assistant Conversation — so that a reply to it, or plain text typed after it, reaches that target with no prefix and no Session Name. An Anchor may offer choices in order; a numeral picks one by position, and a surface may draw them as buttons, which are nothing more than that numeral.
_Avoid_: thread (Telegram has none), context message, reply target

**Anchor Table**:
Bridge Core's memory of the Anchors still worth replying to: each Session's newest few, gone when the Session goes, never written to disk. The message id is its only key; the words Telegram echoes back are never read.
_Avoid_: reply map, message cache, conversation state

**Assistant Conversation**:
A conversation with a coding model opened from the Companion Channel's menu and continued by replying to any of its messages. Each turn is a Delegated Turn; its memory is the model's own thread, which Bridge Core names but never stores. Not a Session, and not the Call Agent.
_Avoid_: assistant (unqualified), bot, chat, Session-level assistant

**Codex Runtime**:
The one codex the machine already has, as the three facts everything on this side is given about it: the **executable** the user gets when they type `codex`, the **control socket** it listens on when this product's login job starts it (derived from `CODEX_HOME`, never asked of a running process), and the **launch environment** that job needs to start it at all. The product starts that codex as a shared app-server the user's own terminals join; it never installs one, wraps one, or names a binary the user does not get (ADR 0022).
_Avoid_: daemon as the name of the subcommand family this replaced — in code and the ADRs it is the defined short name for the shared app-server (ADR 0020, vocabulary amendment), and new prose says "shared app-server"; managed binary, standalone, bundled codex

**Session**:
One interactive terminal run of Claude Code or Codex. The system sees every Session on the machine, reads what it stopped on, and Relays into it. It sees one by recognising it from what the machine already shows — never by wrapping or instrumenting it — so a Session it cannot recognise is under-reported and said to be, never invented (ADR 0020).
_Avoid_: task, job, window, launched Session (the system launches nothing)

**Child Process**:
A process a Session spawns — a subagent, a review crew, a named in-process teammate. Which of those an agent builds it as is the agent's own mechanism and changes nothing here: it appears in the roster under its Session and nothing more, with no Relay, no Stop Notice and no Session Name. A child the agent addresses by a name of its own is still nameless in this sense — that address is the agent's handle on it, never something the user says to reach it.
_Avoid_: child Session, subagent and teammate (the agent's mechanism words), crew

**Session Name**:
What the user and the system call one Session: `<project> · <task>`, where the project is the
Git repository the Session is working in — its own directory when it is in none — and the task
says what the Session is doing, in words the user can understand. The task is read from the
best source the agent has already written (a name the user gave it, the agent's own generated
title, the user's first words to it, the agent's derived name as the floor), so every main
Session the agent has identified has one, none is asked to supply one, and none is invented. It climbs to a better source as one appears
and never falls back; a change is not announced. A Session Name is for recognising a Session,
never for addressing it — that is the address's job (ADR 0024).
_Avoid_: label, title, session label

**Relay**:
Carrying words *into* a Session — the agent-ward direction.
_Avoid_: injection (a mechanism, not the capability), push, channel (reserved for the Companion Channel)

**Answer Relay**:
A Relay of the user's own words — their instructions and their answers to a Session's questions. It always carries the words; whether it also carries the user's authority is the route's to say. An answer to the question a Session Brief offers with its choices, when it says that question can be answered from here, arrives as the user's own (ADR 0015) — as does an Approval Relay, which is nothing but their verdict. Every other Relay arrives as another session's words, with none of their authority, so whether the Session acts on it is that Session's own call (ADR 0013 §3). When the words were an answer to a question, the Voice says so on the receipt rather than leaving the user to assume otherwise. A Relay is the user's words to every reader that asks for them — it appears in History and can name a Session — and its inbox delivery is never evidence of progress to Stop analysis: a peer-message record does not move the tail boundary and cannot end a stop, so a Session held on one is still held on it. What ends a held question is the hook route (ADR 0015), which settles the call by writing its result rather than by being read as words (#306).
_Avoid_: MCP Channel (one adapter, not the capability)

**Relayed Instruction**:
The user's spoken words as a Relay puts them into a Session: one complete instruction, in the user's own meaning, that the Session can act on as a task. Tidied of what speech leaves behind — fillers, stutters, the versions a self-correction overruled, and the framing the user addressed to the Voice — and gathered from the pieces it was spoken in. Nothing is added, expanded, decided or chosen on the user's behalf, and the meaning does not move. Graded by the user reading what was heard beside what was relayed.
_Avoid_: tidied instruction, cleaned transcript, rewritten request

**Approval Relay**:
A Relay of the user's verdict on a Session's pending permission request — one decision for one request, carrying the user's authority. It carries and nothing more: the request is briefed as the Session's PERMISSION state like any other, the hook's own life bounds how long a verdict can land, and the outcome is the receipt the verb returns.
_Avoid_: auto-approve (the user decides, the system only carries), permission bypass, approval budget (the engine keeps none), closing notice (retired)

**Reply Window**:
The state in which a Session will act on the next Relay as its next turn. While it is closed, Relays wait.
_Avoid_: idle state (a Session can be busy yet accepting), input prompt
