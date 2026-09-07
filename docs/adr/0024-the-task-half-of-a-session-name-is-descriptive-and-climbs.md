# 24. The task half of a Session Name describes the Session, climbs to a better source, and never addresses

Date: 2026-09-07 · Status: Accepted · Source: #273, #281

#78 made the task half of a Session Name the agent's own stable name — Claude's
derived `<dir>-<2hex>`, Codex's `Thread.name` — frozen so the user could say it
back to address the Session, and it dropped the transcript `ai-title` on the
strength of a legacy figure (30% never written, median 440 s late) that #280
found to be unverifiable prose with no script or data behind it. The result was
a name that was stable and said nothing: `gpt-voicecoding-4c` does not tell the
user what a Session is doing, which is the only thing they want a name for.

**Decision.** The task half is descriptive and there is one field. It is read,
never asked for: a lane hands the shared naming module the strings its agent has
already written, and the module picks the highest of a fixed ladder — Claude: a
user-given name (`nameSource` not `derived`), the newest `ai-title`, the first
prompt cleaned, the derived name as the floor; Codex: a `Thread.name` that is
not the `preview` read back, the `preview` cleaned, the short thread id as the
floor. The floor is the agent's own identity, so every main Session the agent
has identified is named; a row with no identity yet (a Codex process without a
thread id, a Claude registry row with no name and no transcript) stays unnamed
and is shown by its address — the bridge never composes a name. The name
climbs and never falls: a higher rung replaces a lower one, the same rung
follows its source's latest value, a source that vanishes leaves the last name
standing. A change is not announced on any surface. Cleaning the first-words
rung is deterministic string work — strip a slash-command wrapper to its
arguments, drop image markers, one line, cut at a configured length — and never
a model call. The Codex provisional-title rule (#113) is absorbed: a name equal
to its preview is simply the second rung.

**A Session Name is never an address.** That was already the rule for Relay
and Approval; it now removes the one place a name took part in targeting, the
Companion Channel's `@<name fragment>: words` form. The user replies to an
Anchor or picks from the menu; the Voice copies addresses the engine gave it.
Two Sessions that read alike keep the existing `(address)` suffix on the roster.

**Why this shape.** The alternative — a second, descriptive field beside a
frozen addressing name — was rejected because the addressing name was never
spoken back in practice and two names for one Session is what #78 had already
collapsed once. Legacy's self-report (`legacy@1d32845:bridge/hook.py:215-253`,
`bridge/labels.py:73-106`, `bridge/store.py:1875-1902`) is **dropped**: it made
the user's first turn wait on a synchronous, unbounded step — the "often timed
out" the user reported; its immutability semantics are **adapted** into
climb-never-fall, and its validation (non-empty, one line, no silent fallback)
is **ported**. All judgement lives in the one pure naming module; adapters carry
facts and the resolved project name; the module lives in Bridge Core (ADR 0001:
`core` imports no adapter), which calls it and stores the result and the rung it
came from.
