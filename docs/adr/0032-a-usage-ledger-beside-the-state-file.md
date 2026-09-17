# 32. A usage ledger beside the state file, for the Codex work this product starts

Date: 2026-09-17 · Status: Accepted · Source: [#377](https://github.com/Simon-AI-coding/GPT-VoiceCoding/issues/377)

On 2026-09-17 the user's weekly Codex allowance fell from 25% to 23% remaining on a morning they did
not remember using Codex. Finding what this product had spent took a manual search of
`~/.codex/sessions` and `engine.log`, and it only half worked. The Call Agent's token usage was held
in memory for the status reply and nowhere else. The engine logged the name of the Voice's
`session.usage.updated` event once per call and dropped what the event said. Nothing read the
account's rate limits.

`core/persistence.py` keeps switch state and nothing else, and it says a durable history would be
"a different store and a different decision". This ADR is that decision.

## Decision

**The engine appends one JSON line for each usage fact about Codex work it started**, when the fact
arrives, to `usage-YYYY-MM.jsonl` in the directory that holds `state.json`.

- **Content.**
  - Each line has `at` (ISO 8601 with offset) and `kind`, plus the ids known at that moment
    (`call_id`, `realtime_session_id`, `thread_id`, `turn_id`, and `role` for a thread).
  - `payload` is what Codex sent.
  - The line kinds are:
    - `codex_thread`: `thread/tokenUsage/updated` for the Call Agent's thread, or for a Delegated
      Turn's thread while that turn is running.
    - `realtime`: the realtime channel's `session.usage.updated` event. A real event from codex's
      own log carries no transcript: `usage.audio_duration_ms`, `usage.backend_model_usage` and
      `usage_limit`.
    - `rate_limits`: `account/rateLimits/updated`. Codex's log shows that notification alongside
      every token reading on a server, and only this product's own threads run on the engine's
      server.
- **Exclusions.** The user's own Sessions are left out; they run on the shared daemon, and Codex
  already records them.
- **Files.** One file per local calendar month. Files are never pruned and there is no switch. The
  composition root builds the ledger and passes it to the Call adapter, just as it passes
  `[delegate] model`.
- **Writing.** Every write opens, appends and closes the file.
- **Failures.** A failed write produces one warning. Later failures stay silent until a write
  succeeds again. The ledger never raises.
- **Transport.** `CallTransport` gains `on_event`, because the Voice's usage arrives on the realtime
  events channel and in no app-server notification.

**This is not the durable subset ADR 0001 describes, and does not amend it.** That subset is Bridge
Core's truth: written by one storage component and read back on restart. The ledger is bookkeeping
the Call adapter writes and nothing in the engine reads back, so no decision is ever made from it.

## Consequences

- `jq` over one month's file shows what this product spent and when, for the Call Agent, Delegated
  Turns and the Voice, next to the account's rate-limit readings.
- The ledger is a second durable file, and it is write-only for the engine. Nothing in the engine
  reads it, so it cannot become a second source of truth for anything.
- No UI or summary command reads it yet. Either would be a separate decision.
