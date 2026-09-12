# The control plane

The interface Bridge Core exposes: JSON over a Unix domain socket, one object per
line. Every surface speaks it — the menu-bar shell, `bridgectl`, the Companion
Channel, and spoken commands inside a Live Call — and no surface has a private
protocol beside it.

**It is never gated by any switch** ([ADR 0002](adr/0002-the-control-plane-is-never-gated-by-switches.md),
absolute). Every action below answers with the Duty, Voice and Message Switches
all off. The reference implementation gated seven actions behind the Duty
Switch; that behaviour is dropped, not ported, and
`tests/test_control_plane_actions.py` is the regression test.

This document is the contract the Swift shell (#11) implements against. The
Python vocabulary is `gpt_voicecoding.seams.control_plane`; the mechanism is
`gpt_voicecoding.control_plane`.

## Where things live

| What | Where | Why |
| --- | --- | --- |
| The socket | `/tmp/gpt-voicecoding-<uid>/control.sock` | Darwin caps an `AF_UNIX` path at 103 bytes, and the application-support path is already 76 of them before a long home directory is considered. The runtime root is short, per-uid, created `0700` at engine start (`/tmp` is cleared on reboot, which is correct for a socket), ownership-checked with `lstat` before adoption, and the socket itself is `0600`. |
| The durable state | `~/Library/Application Support/GPT-VoiceCoding/engine/state.json` | It outlives reboots, and nothing but Bridge Core may read it. |
| The configuration | `~/Library/Application Support/GPT-VoiceCoding/engine/config.toml` | The user owns it; the engine only reads it. |

So the socket path is **not** derivable from the state path. A surface reads it
from configuration, or is told it directly (`bridgectl --socket`). Both paths are
overridable, and a configured socket path longer than 103 bytes is refused at
start with a named error rather than an `OSError` from inside asyncio.

## The wire

- One request per line, UTF-8, `\n`-terminated. One reply per request, on the
  same connection, in order.
- A line may not exceed **65536 bytes** in either direction. A request that
  overruns it is answered with `malformed_request` and that connection is closed
  — there is no honest way to resync inside a line.
- Reply capacity is measured and written with one canonical UTF-8 JSON-line
  encoder, including JSON escaping and the terminating newline. The server
  performs a final outbound check. If an honest response skeleton cannot fit,
  it sends a bounded `refused` reply; it never drops Session rows silently.
- A connection may carry any number of requests. Connections are independent: two
  surfaces can neither wedge nor read each other.
- Malformed input costs one request, never the server.
- The socket is refused by both sides unless it is owned by the current user and
  private (`0600`). A live engine is never displaced; debris from a dead one is
  cleared.

### Request

```json
{"action": "switch", "payload": {"name": "duty", "on": true}}
```

`payload` may be omitted when an action takes nothing.

**`reader`** is an optional field beside `action`, naming who the answer is for
when that changes what may be carried. Its only value is `"voice"`: the answer
will cross the Live Call's return leg, where the Call Agent hands it to the
Voice and codex cuts anything over 4,000 UTF-8 bytes, keeping ~2,000 bytes from
each end and leaving a marker that says how much went but never which part. A
`history` or `brief` request that carries it is fitted to that line as well as
to this one, and the tighter of the two binds; on every other action it is
carried and ignored. Absent — or JSON `null` — is exactly the behaviour that
existed before the field, which is what keeps the Companion Channel byte for
byte unchanged. An unrecognised value is `malformed_request`, naming the values
there are. The engine sets it, in the `bridgectl` invocation it generates for
the Call Agent (`--reader voice`); the Companion Channel's `/` grammar never
does ([#302](https://github.com/okqixiaobao727-design/GPT-VoiceCoding/issues/302),
ADR 0016 as amended).

```json
{"action": "history", "payload": {"target": {"agent": "codex", "session_id": "abc"}}, "reader": "voice"}
```

### Reply

```json
{"ok": true, "action": "switch", "protocol": 8, "data": {"name": "duty", "on": true, "previous": false}}
```

```json
{"ok": false, "action": "switch", "protocol": 8, "error": {"code": "unknown_switch", "message": "unknown switch: 'sound'"}}
```

`action` is `null` when the line never named a usable one. `protocol` is the
numeric protocol version, currently `12`. A missing field or JSON `null` means the
reply did not declare a usable version. The Swift shell refuses to interpret any
reply whose version is missing or differs from the version it supports, and shows
that protocol mismatch separately from an engine refusal or an unreachable engine.

**`error.message` is rendered verbatim.** It is the refusal's own words, from
Bridge Core. A surface that rephrased one would be a second voice deciding what
the user is told.

### Error codes

| Code | Means |
| --- | --- |
| `malformed_request` | Not one JSON object this protocol can represent, or past the byte bound. |
| `unknown_action` | Well-formed, naming an action this engine does not have. |
| `invalid_payload` | The action is known; what came with it is not usable. |
| `unknown_switch` | No switch by that name is registered. |
| `unknown_session` | No Session by that identity was ever registered here. |
| `stale_session` | Known session id, unreachable under that identity — a fork, or an end. |
| `unknown_pending` | Nothing is waiting under that id; it was answered, or its hook ended. |
| `refused` | Any other Bridge Core refusal. Still carries its own words. |
| `telegram_credentials`, `telegram_network`, `telegram_destination`, `telegram_api` | Binding refused at the named Telegram layer; no token is included. |
| `engine_unreachable` | Raised by a **surface**, never sent by the engine: nothing answered. |

### Redesign documents and actions (protocol 12)

`status.engine_version` is the running engine's package version. `status.call_agent`
is absent until the first Call Agent is created after startup, and absent again
after `forget_call_agent`. When present it contains `model`, `effort`,
`context_window`, `context_percent`, `total`, and `last`. Each usage object has
`input`, `output`, `reasoning`, and `cached` token counts. Usage and context fields
are `null` until the live `thread/tokenUsage/updated` notification arrives;
the meter is percent used, with codex's 12,000-token baseline removed from
capacity. These are the Call Agent's tokens, not the Voice's. `bridgectl status`
prints these fields alongside the existing diagnostics.

`models` takes no payload and returns `{"models": [{"model": "…", "efforts": ["low", "high"]}]}`.
The engine asks its shared, long-lived app-server once at startup; reads never
poll codex. Hidden models are omitted. An empty or unavailable catalog returns
an empty list until restart, which fetches a fresh catalog.

`forget_call_agent` takes no payload and returns `{}`. It is refused while a call
is connecting, up, or closing. Between calls it clears the current thread and
its usage. A user's `live` dial always starts a fresh Call Agent; a system dial
continues the current thread or starts one if none exists. This memory is never
persisted across engine restarts.

`bind_telegram` uses three requests, without changing configuration or storing
the token on disk:

- `{"token": "<pasted token>"}` validates it and returns `bot_name`, `username`,
  `chat_id: null`, and `chat_type: null`. It discards pre-binding updates.
- An empty payload checks for a new `/start`; while waiting it returns the same
  pending document. After Start it returns the chat id as a string and Telegram's
  actual chat type (`private`, `group`, or `supergroup`), sends one confirmation,
  and completes. Another empty request after completion is refused.
- `{"cancel": true}` abandons a pending binding. A surface dismissing the flow
  must cancel it. Supplying another token replaces the pending flow.

The existing Telegram wire constructs a transport bound to the pasted token.
A composed Telegram adapter pauses its poll for the entire flow, including
waiting for its in-flight poll to finish, and resumes on success, refusal, or
cancellation. The null channel is unchanged and needs no Telegram knowledge.
Refusals carry the existing Telegram layer words; tokens are never logged.
CLI equivalents are `bridgectl models`, `bridgectl forget_call_agent`, and
`bridgectl bind_telegram [<token>|--cancel]` (no argument checks for Start).
Prefer the socket payload for tokens: entering a literal token in a shell command
can leave it in shell history even though the engine never logs it.

The desktop shell owns one shared poller (#360, maintainer clarification): with
only the Duty Card visible, read both `status` and the roster `brief` every two
seconds. With the Control Panel window open, read `status` every second and
`brief` every two seconds, including the targeted brief while that screen is
open. With neither surface visible, stop polling. The card needs `status` as
well as `brief` because its counts, switches, and call state come from `status`.
Both roster rows and targeted Session Briefs also carry `state_word`, rendered
by Core's existing wording function. This additive field completes #359's
desktop contract (#360, maintainer clarification): the shell displays it
unchanged rather than parsing `text` or keeping a second wording table.
Each Session in `status` also carries `brief_state`, the same Core-derived
state code as its brief. The original lifecycle `state` is unchanged. Desktop
counts use `brief_state` after excluding ended rows, Child Processes, and
Headless Runs from the main Sessions, so prose questions are not miscounted as
finished and the shell does not duplicate Core's question recognition.

## The actions

Fourteen, and the set is closed. Adding one is a contract change. Protocol 12
adds `models`, `forget_call_agent`, and `bind_telegram` (#359). Protocol 6
retired `sessions` and added `brief`, the one verb Session state is fetched
through. Protocol 7 retired `progress` and added `history`: the Session Brief
carries the newest message whole and the History page carries that message and
everything before it, five at a time with a cursor, so the exact progress
publication had no question left to answer. A protocol-6 surface must report a mismatch — it would
otherwise send `progress` to an engine that answers `unknown_action`, and it has
no way to ask for a second page of anything.

Protocol 8 changes no action. It retires `pending_approvals` from `status`: a
pending permission is one of the three Session states, so the roster row in
`permission` is the whole of what is waiting, and its `approval_id` is the handle
`approve` answers with. A protocol-7 surface must report a mismatch rather than
count an absent list as zero — "0 pending approvals" over a dialog that is on
screen is a silent wrong number, which is what the version gate exists to
prevent.

Protocol 9 adds `sessions`, `config` and `assistant` — the three verbs the
Companion Channel's command menu opens screens with (ADR 0021 §6 §7, #264,
#265). The
menu is four entries, `/assistant`, `/sessions`, `/status`, `/config`, and it
advertises only what the shared parser accepts: the entries and the one
sentence shown beside each live in `seams/control_plane.py::MENU`, and the
Telegram adapter sets them on the bot at first contact and keeps no list of its
own. A protocol-8 surface would send `sessions` to an engine that answers it
and be unable to tell that from one that refuses it. The `sessions` here is
not protocol 6's: that one was a second rendering of the roster, and this one
answers with Briefing's own text and adds only labels.

Protocol 10 adds the optional `reader` field described under **Request**
([#302](https://github.com/okqixiaobao727-design/GPT-VoiceCoding/issues/302)). A
protocol-9 engine accepts the same request and ignores the field, answering a
page fitted to this wire alone — which codex then cuts on the way to the Voice,
silently and in the middle. So a surface has to be able to tell the two apart
before it asks. A request that sends no mark is answered at 10 exactly as it was
at 9, and no gate on the field is added on the Python side: `bridgectl` ships
inside the engine's own package, so the two move together.

`launch` and `close` were the eighth and ninth until protocol 4. They are parked
with the code behind them ([#72](https://github.com/okqixiaobao727-design/GPT-VoiceCoding/issues/72)):
v1.0 is a bridge over the Sessions the user starts, so nothing here brings one
into existence or ends one. Their two error codes, `launch_failed` and
`close_failed`, went with them. A surface still sending either action is
answered `unknown_action`.

### `status`

Payload: none. Data:

```json
{
  "switches": {"duty": false, "voice": false, "message": false, "auto_hangup": true},
  "sessions": [ /* see below */ ],
  "call_id": null,
  "pending_relays": [{"request_id": "…", "target": {…}, "kind": "answer", "text": "…",
                      "route": "deliver", "queued_at": 0.0,
                      "outcome": "unknown" /* or null: nothing has been attempted */}]
}
```

A session — every field of one roster row, because a surface that had to ask a
second question to render one line would be a second reader of the same Session:

```json
{"target": {"agent": "claude", "session_id": "abc", "pid": 1234},
 "label": "gpt-voicecoding · build the control plane",
 "name": "workspace-claude-ed",
 "workspace": "/Users/…", "first_seen": 1787222000.0,
 "lifecycle": "live", "state": "idle",
 "waiting_for": {"kind": "question", "caught_up": true, "prompt": "Which base?",
                 "options": [{"text": "main", "description": "Use the default branch",
                              "recommended": true}],
                 "recommendation": "main", "tool_name": null, "detail": null,
                 "approval_id": null, "awaiting": null},
 "progress": {"availability": "readable", "has_history": true,
              "omission": "status_summary",
              "read_at": "2026-08-26T02:44:39+00:00", "recent": []},
 "last_activity": "2026-08-26T02:44:39+00:00",
 "child": {"kind": "main", "parent": null},
 "headless_run": false,
 "reply_window": "open"}
```

`target` is the **address**; `label` is for speech and for matching. A label
never crosses the wire as an address — resolving one to a target is Bridge
Core's router, on the way in from the Companion Channel.

`child` and `headless_run` are the two tiers a row can be in that are not a
Session, and a row carries both because both stay on this wire: `status` lists
every row the engine holds, and neither of these is a row the user may be
offered. `child.kind` is `main` or `child` (ADR 0020's Child Process);
`headless_run` is `true` for a run with no controlling terminal — a complete
process nobody can type into, kept as a row and never announced (ADR 0020 as
amended, `CONTEXT.md`'s *Headless Run*). A surface counting the Sessions a
person can see running counts the rows where `child.kind` is `main` and
`headless_run` is `false`. The key is additive: a reader that does not know it
reads its absence as `false`, which is the count it had before.

`first_seen` is wall-clock seconds, and it is when *this engine* first saw the
Session — no agent knows it.

`waiting_for` is what a stopped Session stopped on, as structure rather than as a
rendered sentence. `kind` is one of `none`, `question`, `permission`, `peer`,
`child`, `unknown`; `unknown` always comes with `caught_up: false`, and means
*ask again*, never *nothing is happening*. A question option always carries
`text` and `recommended`; its `description` is the Agent's optional explanation
of that choice, or `null` when the Agent supplied none.

`awaiting` is set on `peer` and `child` alone, and it is the **lane's** own
reference to the party being awaited: the awaited Session's address for `peer`,
the child's own name for `child`, and `null` for a child that has none. The name
the *user* reads is Core's and travels on a brief's `awaited`, because a Session
Name climbs (ADR 0024) and a name resolved at the Stop would be announced stale.

A row's `progress` always carries the same five fields:

- `availability`: `not_read`, `unreadable`, or `readable`;
- `has_history`: a boolean only when readable, otherwise `null`;
- `omission`: `none`, `older`, `status_summary`, or `newest_oversize`
  (`oversize` names one History page entry and never a whole reading);
- `read_at`: the readable observation's timestamp, otherwise `null`; and
- `recent`: ordered whole entries, each with `role` and `text`.

Only `readable` plus `has_history=false` and `omission=none` means nothing has
been said. A roster publication carries no chat body: readable history becomes
`has_history=true`, `recent=[]`, `omission=status_summary`. `not_read` and
`unreadable` are never inferred from an empty array. The source failure behind
an unreadable discovery is reported as lane degradation; a `history` read maps
it to a typed refusal.

`last_activity` is separate from the progress summary on purpose. A Session can have moved
without saying anything a reader would show, and this is the field that says so.
It is `null` when nothing read one.

It is deliberately **wider than that summary**: it counts any work in the Session,
its own subagents included, where the progress summary carries only what the user
would be shown. On the Claude lane that means a sidechain record advances it even though
such a record never becomes an entry; on the Codex lane it is the thread's own
`updatedAt`, which moves for any work at all. One meaning on both lanes — and a
Session four minutes into a subagent is not a Session nobody has heard from. A
subagent is not a Child Process: that is a separate agent process, and it has a
row of its own.

`reply_window` is derived on the row and rendered here, so no surface re-derives
it and no two surfaces can disagree about one Session.

### `brief`

Payload: none, or `{"target": {"agent": "codex", "session_id": "abc", "pid": null}}`.
Answered by Briefing (`core/briefing.py`), which is the **only** thing in this
engine that puts words to what a Session is doing. An omitted `target`, or one
written as JSON `null`, means the whole roster — the same reading `route` gives
an absent value. A `target` that is **present and unusable** is refused rather
than widened: a surface that asked about one Session is never answered with all
of them.

Every reply carries `kind`, `text`, and the structure `text` was rendered from:

Roster rows and counts include every live, addressable Session, whether Focus
or not. Rows are ordered by most recent activity descending on every surface;
unknown activity sorts last and ties retain registry order. `focus` remains
call-side metadata, never a sorting or counting rule. Each row adds `newest`
(the first line of the newest assistant message, or `null` when unavailable)
and `last_activity_at` (ISO 8601, or `null`). The target-specific Session Brief
continues to carry the whole newest message. Names start at the known project
alone and climb to `<project> · <task>` when a task source arrives; an unknown
project is not fabricated from a socket address or pid.

```json
{"kind": "roster",
 "text": "sessions: 1 waiting for your decision\n  …",
 "roster": {"counts": {"decision": 1},
            "focus": {"agent": "codex", "session_id": "abc", "pid": null},
            "rows": [{"target": {…}, "name": "gpt-voicecoding · port the log",
                      "agent": "codex", "state": "decision", "focus": true,
                      "newest": "I rebuilt the index.",
                      "last_activity_at": "2026-09-02T03:04:05+00:00"}]}}
```

```json
{"kind": "session",
 "text": "gpt-voicecoding · port the log — codex:abc — waiting for your decision\n  …",
 "session": {"target": {…}, "name": "gpt-voicecoding · port the log", "agent": "codex",
             "state": "decision",
             "newest": {"state": "said", "text": "I rebuilt the index."},
             "decision": {"prompt": "Which base?",
                          "options": [{"text": "main", "description": "the default branch",
                                       "recommended": true}],
                          "recommendation": "main", "tool": null, "summary": null},
             "answerable_here": true,
             "last_activity_at": "2026-09-02T03:04:05+00:00"}}
```

**`text` is the engine's own rendering, and the words in it never fork.**
`bridgectl brief` prints exactly this string, and so does the engine log. One
source of the words is the point: two would be two descriptions of one Session.
What may differ per surface is the **layout** (ADR 0021 §5): the Companion
Channel is handed the same brief as a structured carrier beside this text —
state, agent, name, question, option labels in order, recommendation, the newest
message whole — filled from the same wording tables, and an adapter with a shape
of its own (Telegram: a state light, the question in bold, the original folded)
arranges those words and chooses none. An adapter with no layout prints `text`.
That is what the realtime adapter already does with the spoken brief, and the
rule it restates is the one above: the vocabulary is `core/briefing.py`'s alone.
The structure travels beside the text here too, for a surface that reads fields
rather than lines.

`state` is one of five, and they are what the user is told:

| State | When |
| --- | --- |
| `decision` | A question is put to the user — a question hook, or a question mark in the turn's final prose. A question mark makes a decision; nothing else does. |
| `permission` | A permission dialog is open. |
| `waiting_on` | The turn ended with the ball in another Session's hands or in the Session's own Child Process's. The one state word that names somebody: `waiting on <name>`, filled with the awaited Session's Session Name, the child's own name, or `a background command` for a child that has none. |
| `finished` | No question and nothing awaited — including a hand-over that asks nothing, and a stop nobody could read. Exited Sessions appear nowhere. |
| `running` | Mid-turn. Nothing is being asked of the user. |

`unreadable` **was a sixth state and is retired** (#320). A read that failed is
a fact about the *message*, not a state of the Session, so it is told where it
belongs: the state says nothing is being asked and `newest.state` says the
message could not be read. Nothing about a failed read is dressed as a decision
the user has to make.

`newest` is the newest assistant message **whole**, under ADR 0016's omission
rules — the engine never condenses, so the one-line conclusion a user hears and
the detail they may ask for are one field. Its `state` is `said`, `nothing_said`,
`not_read`, `unreadable` or `oversize`, and `text` is present only for `said`.

`awaited` is who the `waiting_on` state word names, already in the words the
user reads — the awaited Session's Session Name, or the awaited child's name.
`null` on every other state, and on a `waiting_on` whose party has no name of
its own, which the rendered `text` beside it words as `a background command`.

`decision` is `null` when nothing is being asked. A question carries `prompt`,
every `option` and any `recommendation`; a permission carries `tool` and a
one-line `summary`. Both shapes use the one object, and which one it is follows
from `state`.

`answerable_here` says whether the user's reply can reach this Session from
here. A question is answerable while its lane still holds the route; a
permission is answerable while a handle still holds the dialog open. Anything
else is answered at the terminal.

The **Roster Brief** lists one header row per live Session, the Focus Session
first, and children nowhere: every row is one `brief <address>` answers, and a
Child Process is seen and never spoken to. `counts` is **the others** whenever
there is a Focus Session — that Session is named in full, so counting it again
would be one Session told twice.

**The Focus Session is never set here.** It moves when the user *replies* to a
Session — `relay` or `approve` — and is cleared when that Session ends. Asking
about a Session is not replying to one.

`newest` travels twice — as a field and inside `text` — so one whole message can
overflow the line that has to hold it. When it does, the reply still answers:
`newest` becomes `{"state": "oversize", "text": null}` and everything the user
acts on — the header, the state, the whole decision — stays. Text is never cut
(ADR 0016).

`brief <address>` is a **read**, read now, through exactly one `inspect`. Its
refusals are `history`'s minus one: `unknown_session`, `stale_session`, and
`refused` for a Child Process or a lane that could not be read at all. A Session
whose *progress* could not be read is **not** a refusal here — it answers with
an `unreadable` `newest` beside a state read off the wait, whether that Session
is running or has stopped.

### `history`

Payload: `{"target": {"agent": "codex", "session_id": "abc", "pid": null},
"before": 12}`. `before` is optional, and JSON `null` reads the same as absent.
Data:

```json
{"entries": [{"ordinal": 14, "role": "assistant", "text": "done"},
             {"ordinal": 13, "role": "user", "text": "do the thing"},
             {"ordinal": 12, "role": "assistant", "omission": "oversize"}],
 "older": true,
 "read_at": "2026-08-26T02:44:39+00:00"}
```

One page of what a Session said and was told, **newest first**, bounded by a
count and not by bytes: `[policy] history_page_entries`, five by default, both
roles counted. The size is the engine's dial and is never on the request — two
surfaces cannot ask one engine for two different pages of one Session.

`ordinal` is the entry's place in the Session's visible record, **counted from
the oldest entry and starting at 0**, assigned by the lane at read time. Both
sources are append-only for the entries this carries — a Claude transcript file,
a Codex thread's turns — so an ordinal names the same entry across reads while
the Session lives. `older` says whether anything remains before the oldest entry
on this page; the next request passes that entry's ordinal as `before`, which is
**exclusive**.

**No `before` is the newest page, and the newest page includes `newest`.** Every
page is complete on its own and the engine remembers no cursor between reads, so
the page a surface gets first is the one a Session Brief's `newest` is on. A
`before` above every ordinal is the newest page too. A `before` past the oldest
entry is an **empty page with `older: false`** — an answer, not a refusal, and
the distinction this whole action is drawn around.

**The 65,536-byte limit stays a ceiling on the line, never the page.** An entry
that would push the encoded Reply past it is published as *existing but omitted*
— `ordinal`, `role`, `omission: "oversize"`, and no `text` — and **keeps its
slot**, so the page always advances and one large message never blocks the ones
before it. Text is never cut (ADR 0016).

It is a **read**: it resolves one exact identity, asks that lane and no other,
and never starts a turn. It is **not folded into the roster** — `inspect` keeps
answering the newest tail and folding, and a page is a separate read that
observes nothing. A Session mid-turn is readable here.

**It refuses rather than answering emptily**, and the four refusals are four
different facts:

| Code | When |
| --- | --- |
| `unknown_session` | No Session by that identity was ever registered here. |
| `stale_session` | The identity names a different process now, **or** the Session has ended. The row is not ended by this action: discovery is the whole-lane reading and is the only thing that ends one. |
| `refused` | The lane could not be read at all. The message is the lane's own words, and the row stands exactly as the roster last saw it: not being able to look is not a sighting. |
| `refused` | Nothing could read what it said — a Codex Session the shared daemon does not hold, or one whose first turn has written no record yet. |

The last one is the line this action is drawn around. A Session that *was* read
and has nothing before the cursor answers with an empty page; a Session nobody
could read is a refusal. A surface handed the second as the first would render a
working Session as one that has said nothing.

A Session the Codex daemon does not hold therefore never gets an invented
reading. Its rollout is on disk and reading it would be a second source answering
the same question with worse evidence.

### `switch`

Payload: `{"name": "duty", "on": true}`. `on` must be a JSON boolean; a string is
refused, because `"false"` is truthy and the switch it would turn on is the
master. Data: `{"name": …, "on": …, "previous": …}`.

Four names are registered — `duty`, `voice`, `message`, `auto_hangup` — plus
whatever Feature Switches configuration declares. Any other name is
`unknown_switch`. Every position is persisted, and a surface reads them back from
`status` under the same keys.

`auto_hangup` is the Auto Hang-up Switch, and it is the odd one: it starts **on**,
where the other three start off, and it hangs from nothing. The Silence Ceiling
is the call's own limit rather than an act toward the user, so it ends a silent
call with Duty off and on calls the user opened; only this switch stops it. How
long that silence runs is configuration, not a switch — `[policy]
silence_end_seconds`, and `speech_settle_seconds` for how long a pause stays a
pause before it counts. The Call Keeper asks `SwitchAdjudicator.may_auto_hangup()`,
the same way it asks `may_touch_call()` before it dials.

Growing the set is additive on the wire and not a protocol change: the grammar
above is unchanged, and a surface renders `status`'s switches over its own known
order, so a key it has no row for is ignored rather than guessed at.

Turning an outlet on is read **at the flip**, on the roster as it stands: each
live main Session still waiting on a question or permission is pushed to the
Companion Channel, and the Call Keeper is woken once. It never replays a
historical Stop Notice.

A wake reaches the call only through the Keeper, which paces it: after any end of
a call — hung up, dropped, or a dial that failed — `[policy] cool_down_seconds`
passes before it dials again, and a wake inside that window marks one dial owed
and pays it from a fresh reading when the window closes. The user's own
`bridgectl live` is not subject to it.

### `live` — the Live Toggle

Payload: none. Data: `{"state": "up" | "connecting" | "down", "call_id": "…" | null}`.

One action: it ends the call the system owns, or starts one if none is up. Every
surface calls this one — a surface holding its own call state is how two toggles
once opened two calls. It is bound only by the one-call-at-a-time invariant,
never by a switch.

### `relay` — an Answer Relay

Payload: `{"target": {…}, "text": "carry on", "route": "deliver" | "supplement"}`.
Data:

```json
{"request_id": "…", "target": {…}, "state": "pending" | "retained" | "delivered" | "reported_failed",
 "route": "deliver",
 "receipt": {"outcome": "delivered" | "failed" | "held" | "unknown", "reason": "…"} | null,
 "reason": "delivered" | "awaiting_reply_window" | "duplicate_risk" | "held_far_side"
         | "session_ended" | "question_unanswerable"}
```

**The receipt is a grade and a reason, never a sentence.** Three facts and no
prose: `state` is where the words are, `receipt` is what the last attempt proved
— the delivery seam's own value, with the adapter's evidence in `reason` — and
the top-level `reason` is one code from the closed `RelayReason` set
(`core/relays.py`), which says why the Relay stands where it does:

| code | what it says |
| --- | --- |
| `delivered` | the attempt proved the words reached the model |
| `awaiting_reply_window` | they wait, and may go again when the Session next takes a turn |
| `duplicate_risk` | an attempt proved nothing either way, so they are kept and never re-sent on this system's authority (P9) |
| `held_far_side` | the far side parked them in front of a person |
| `session_ended` | terminal: the Session ended while they waited |
| `question_unanswerable` | terminal, before the wire: that question is no longer answerable from here (#68) |

`receipt` is `null` when nothing was attempted — **never** a grade of
`unknown`, which is a positive observation about an attempt that was made. The
two are the difference between "it may already have arrived" and "it never left
this process", which is the whole of the duplicate-safety rule.

Surfaces print the three codes and compose no sentence: `state=<state>
grade=<grade|none> reason=<code>` is the one format (`core/relays.py`,
`receipt_line`), and `bridgectl relay` and the Companion Channel's inbound relay
path both answer with exactly it. The words the user *hears* are the Voice's,
re-rendered from these facts.

`state` is `Lifecycle` (`core/lifecycle.py`), which is where the words stand
across every attempt they will get — deliberately not the per-attempt `Delivery`
grade, because reading one attempt's grade as the item's fate is the reference
implementation's worst delivery bug. Its literals are the four states this verb
can answer with: **queued** is `retained`, **delivered** is `delivered`, and
**terminal** is `reported_failed`. There is no `held` *state*: held is a grade
the far side returned, and it reaches a surface as `receipt.outcome = "held"`
with `reason = "held_far_side"`, still `retained` because the words are still
waiting.

Queued is not delivered, and `state` says which it was.

Route follows the user's explicit intent and is never inferred from how busy a
Session is — the same "busy" carries both "add this now" and "this can wait".

There is no action for system-authored words: a surface asking for one would be
a surface claiming to be the system.

### `approve`

Payload: `{"approval_id": "a1", "verdict": "allow" | "deny" | "ask"}`. Data: the
verdict that was carried, and `relay`'s receipt for carrying it — an Approval
Relay is a Relay, so its receipt is the same state, grade and reason.

```json
{"approval_id": "a1", "verdict": "allow", "request_id": "…", "target": {…},
 "state": "delivered", "route": "deliver", "reason": "delivered",
 "receipt": {"outcome": "delivered", "reason": "…"}}
```

The handle is the `approval_id` on a live roster row that is `waiting` on a
`permission`. Two refusals, and they mean different things to the user:

- `unknown_pending` — no live row carries that handle. The dialog was answered,
  or its hook ended and it went back to the screen; that is where it is answered
  now. The engine keeps no clock over it and no ledger of it, so a handle that is
  not on the roster is not waiting anywhere.
- `refused` — the row is a Child Process. It is seen and never spoken to, and
  that includes never answered.

### `verify` — ADR 0003

Payload: none. Data:

```json
{"seams": [{"seam": "companion_channel", "outcome": "pass" | "fail" | "manual",
            "configured": "…", "loaded": "…", "detail": ""}]}
```

Seam names: `call`, `companion_channel`, and `agent.<kind>` per configured agent.

The engine reports what it **actually loaded**, never what a configuration file
says it should have loaded. `configured` is what the file named; `loaded` is what
the adapter says about *itself* when asked — every seam here is pluggable and
every one of them has a `verify` verb, so all of them are asked, and a Call adapter
whose far side is down reports that rather than the engine reciting the
configuration back and calling it an observation.

Three outcomes: `pass`, `fail`, and `manual` — nothing configured anywhere, which
is handed to the operator rather than passed or failed. What is compared is
*presence*, not spelling: configuration names a factory and an adapter names its
implementation, and demanding those match character for character would fail on
every correctly wired machine. So `fail` means one of three things — the adapter
itself reported a failure, configuration names an adapter and the engine loaded
nothing (or the null one), or something is loaded that nothing configured.

### `sessions`, `config` — the menu screens

Payload: none. Data: `{"text": "…", "options": ["…", …]}`.

A **screen** (ADR 0021 §6): `text` is the screen for a surface that draws
nothing — `bridgectl` prints it, and a Companion Channel with no buttons sends
it — and `options` are the labels, in order, for a surface that draws choices.
A label is picked **by position**: a surface that hands one back sends the
numeral, and the label's words are never read. On the Companion Channel every
screen is an Anchor, so a numeral replying to it picks, and a button is that
numeral (`core/anchors.py`, `core/menu.py`).

`sessions` is the roster: `text` is exactly what `brief` with no target
renders, and `options` carry one label per live Session in the roster's own
order — the Session Name, with the address after it where two rows share a
name. With no live Session the text ends with the nothing-running hint and
`options` is empty: a screen with nothing to pick is not an Anchor. On the
channel, picking a Session opens its greeting (`brief` / `history` / `send
message`); `send message` opens the `Say to <name>:` prompt, an Anchor whose
replies are Answer Relays to that Session.

`config` offers `switch`, `verify` and `live`. On the channel, `switch` opens
the switch screen — one label per switch carrying its state, a press meaning
flip, read against the board as it stands when the press arrives — and
`verify` and `live` run at once and answer in text. `status` stays plain text
and is not a screen.

### `assistant`

Payload: none. Data is a screen, the same shape `sessions` and `config` answer
with: `{"text": "...", "options": []}`.

Opens an Assistant Conversation (ADR 0021 §7): the engine starts a fresh coding
thread with the delegated instructions and `[delegate] model`, and answers with
the fixed opening line. **No turn is run** — the line is the engine's own words,
so a conversation costs nothing until it is replied to. There are never options:
a conversation is answered in words.

The conversation is continued only by **replying** to one of its messages on the
Companion Channel, so a caller reached through this socket opens a thread it has
no way to continue: nothing sent here is an Anchor. `bridgectl assistant` is a
way to prove the seam answers, not a way to hold a conversation.

The engine refuses, `refused`, in its own words when it has no assistant behind
it or the coding thread could not be started. How many conversations stay
answerable at once is `[policy] assistant_conversations`, three by default; the
oldest is dropped whole when one past that is opened. A reply to a dropped one
is a reply to an Anchor the engine no longer holds, so it is words that replied
to nothing and goes to the newest Anchor, whichever that now is. The
start-again hint is for the other case: a thread the coding model itself no
longer has.

## The command line

`bridgectl` and the Companion Channel's `/` grammar are one command set, parsed
by one parser, so neither can grow a command the other lacks. On the Companion
Channel `relay` and `approve` are folded away — a reply *is* the relay and a
numeral *is* the approval (ADR 0021 §6) — but they stay typeable, as does every
verb below.

```
status
switch <name> on|off
brief [<agent>:<session id>[:<pid>]]
history <agent>:<session id>[:<pid>] [--before <ordinal>]
live
relay <agent>:<session id>[:<pid>] [--supplement] <words>
approve <approval id> allow|deny|ask
verify
sessions
config
assistant
```

`<agent>:<session id>[:<pid>]` is how a `SessionTarget` is written on one line. A
Claude target without a pid is refused: `--resume` forks a second process under
the same session id. The pid is read by converting it, not by spelling it, so
anything that is not a whole number above zero — a superscript digit, a run of
digits past CPython's int-conversion limit — comes back as the refusal `not a
process id` rather than a traceback.

`bridgectl` exits **0** when the engine answered, **1** when it refused, and **2**
when there was no engine to ask. Collapsing the last two would tell a user their
switch does not exist when nothing is running.

`--task` consumes every remaining word. Project names containing spaces are one
quoted argument; callers never quote an absolute workspace or compose a Session
Label. Omitting `--agent` selects the configured global default. The old
positional agent/workspace/label form is not accepted beside this one.

## Configuration

One TOML file, read once, by the composition root and nothing else.

`[adapters.settings.session_launcher]` and `[launch]` are no longer read. A
configuration carrying `[adapters.settings.session_launcher]` is refused rather
than silently ignored.

```toml
[engine]
socket_path = "/tmp/gpt-voicecoding-501/control.sock"   # optional
state_path  = "~/Library/Application Support/GPT-VoiceCoding/engine/state.json"  # optional

[adapters]
call              = "gpt_voicecoding.adapters.call.realtime:realtime_call"
companion_channel = "gpt_voicecoding.adapters.companion_channel.telegram:telegram_channel"

[adapters.agents]
claude = "gpt_voicecoding.adapters.agent.claude:build"
codex  = "gpt_voicecoding.adapters.agent.codex:codex_agent"

[adapters.settings.call]            # optional; every key belongs to that adapter
workspace = "~/code"                # where the bridge's own threads run; default is ~
voice = "cove"                      # optional; the default Voice
realtime_model = "gpt-live-1-codex"   # optional; realtime model, not the Call Agent model

[policy]                            # optional; these are the locked defaults
silence_end_seconds     = 60
cool_down_seconds       = 30
speech_settle_seconds   = 5
anchor_rows_per_session = 100         # Anchors the Companion Channel keeps per Session (ADR 0021 §2)
assistant_conversations = 3           # Assistant Conversations it keeps at once (ADR 0021 §7)

[log]                               # required: three numbers with no default
max_bytes                     = 8388608
retained_files                = 3
stripped_environment_prefixes = ["Malloc"]

[delegate]
model = "the-model-you-chose"       # required: the cost lever has no default
effort = "high"                     # optional; shared by Call Agent and Delegated Turn
cli   = "/Applications/GPT-VoiceCoding.app/Contents/Resources/engine/bin/bridgectl"
```

Each adapter reference is `module:attribute`, resolved by the composition root —
the only thing in the system that imports an adapter. A factory is called as
`factory(sink=<event sink>)` and returns the adapter.

`[adapters.settings.<seam>]` is that seam's own table, and the composition root
**forwards it without reading a key**: only the adapter knows what its own keys
mean, and a root that parsed them would be the hub growing adapter-shaped
knowledge (ADR 0001). A seam given no table is called with the sink alone. Every
adapter that takes one refuses to start on a key it does not recognise, because a
misspelled setting that silently falls back to a default is the
configuration-shaped version of the silent fallback this project bans.

The shipped Call and Codex Agent adapters **share one `codex app-server`**. The
Codex Agent adapter spawns, owns and reaps it; the Call adapter's realtime route
rides it and starts none of its own. The composition root introduces them, which
is why naming the shipped Call adapter without also naming a Codex Agent adapter
in `[adapters.agents]` refuses to assemble rather than starting an engine whose
voice surface could never come up.

The shipped Call adapter also needs the voice extra —
`pip install 'gpt-voicecoding[voice]'` — and its factory says so at assembly
time rather than at the moment somebody tries to speak.

An adapter with a connection, a reader task or a child of its own may implement
the optional `Connectable` shape (`seams/connection.py`): `async connect()` and
`async aclose()`, both idempotent. The composition root opens every one of them
**before** it serves the socket — a surface that reaches a serving engine reaches
one whose seams are actually filled, and an adapter that cannot open stops the
start rather than being answered for. On shutdown they are closed in reverse
order, and every one gets its turn even if an earlier one raises. An adapter with
nothing to open implements neither verb; the contract is optional because not
every seam has a connection to hold.

**This file is executed with the privileges of the user who wrote it, and is
exactly as trusted as the engine itself.** That is the deliberate cost of naming
adapters by reference: a compiled-in table of allowed names could not name a
deployment's own private wiring, and keeping that wiring private is a charter
decision. The file lives in the user's own application-support directory for the
same reason.

`[delegate] cli` is where the control-plane CLI really is. Bridge Core names it
in the instructions it generates for the voice thread and for a Delegated Turn,
so it has to be true: an instruction naming a binary that is not there is exactly
the invented detail those instructions forbid. Left out — the ordinary case — the
engine uses the console script installed beside its own interpreter, and uses it
only after finding that it exists and can be run. A bundle moves that binary, so
a bundle states this key. If neither is really there, the engine **refuses to
start** rather than describing a CLI nobody can run.

In the bundle that key is not optional, and it points into `Contents/Resources`
rather than `Contents/MacOS`: `pip` installs a console script beside the
interpreter that installed it, and the bundle's interpreter is under
`Contents/Resources/engine/`. There is no second copy in `Contents/MacOS/` — a
duplicate binary that shadowed the real one would be two things to sign and two
things to be wrong. The bundled `bridgectl` keeps `pip`'s console-script body,
but the pipeline replaces its *absolute* shebang with a shell/Python preamble
that resolves the script's real path and execs the interpreter sitting beside
it. The same rewrite covers every Python console script in `engine/bin/`; see
[`docs/app-bundle.md`](app-bundle.md).

An unconfigured Call or Companion Channel seam **refuses to start**, with a
named error. An engine that silently loaded nothing behind a seam
looks exactly like a healthy one until it is needed — the outage ADR 0003 exists
to prevent. Running without a Companion Channel is legitimate, but the null
implementation ships with that adapter (#10) and is not built yet, so today it is
also a refusal that says so.

`[log]`'s three numbers are **required, with no default in code**: they are what
ADR 0004's outage measured, and a compiled-in fallback would quietly reinstate a
value the measurement proved matters. The log's *path* is a location rather than
a decision, so it defaults beside the state file. `max_bytes` binds every
generation, so the disk one log can occupy is `max_bytes × (retained_files + 1)`.

## Running headless

```bash
python -m gpt_voicecoding.engine --config ~/Library/Application\ Support/GPT-VoiceCoding/engine/config.toml
```

The engine stays in the foreground and never daemonises: the menu-bar shell
spawns it as a direct child and expects it to remain one (ADR 0005). `SIGINT` and
`SIGTERM` both stop it in order — loops cancelled, socket removed, so the next
start is not left claiming its own debris. It exits **2** when it could not start,
naming what was missing on stderr. A configuration refusal before log takeover
goes there in full. After takeover, only the final
`the engine cannot start: …` sentence is mirrored to the inherited stderr; the
full diagnostic stays in `engine.log`, which the engine owns (ADR 0004).
