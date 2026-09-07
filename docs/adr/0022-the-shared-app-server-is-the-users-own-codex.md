# 22. The shared app-server is the user's own codex, on a derived control socket

Date: 2026-09-07 · Status: Accepted · Source: [#272](https://github.com/okqixiaobao727-design/GPT-VoiceCoding/issues/272), prototyped by [#271](https://github.com/okqixiaobao727-design/GPT-VoiceCoding/issues/271)

[ADR 0012](0012-installation-runs-at-first-launch.md) put a login `LaunchAgent` on the machine and [#82](https://github.com/okqixiaobao727-design/GPT-VoiceCoding/issues/82) chose what it starts: the **managed standalone** binary at `$CODEX_HOME/packages/standalone/current/codex`, run as `app-server daemon start`. That choice was deliberate and it was argued from a real hazard — what `codex` resolves to in a shell is whatever the user's profile says, and on this product's own author's machine it was once a generation-1 wrapper function. Deriving one binary from one `CODEX_HOME` made the LaunchAgent and the Codex adapter agree.

What it bought was agreement between two parts of this product. What it cost was agreement with the user.

## What was measured

On the reference machine on 2026-09-07, with the shipping build running (#271, `docs/codex-runtime-flow.md`):

| Fact | Observed |
| --- | --- |
| The `codex` the user gets in a terminal | `~/.nvm/versions/node/v24.13.0/bin/codex`, **0.153.4** (npm) |
| The managed standalone tree | `current -> releases/0.149.1-…`, symlink **untouched since 2026-08-25** |
| The server this product's job had running | pid 875, the **0.149.1** managed binary |
| `daemon version` through the npm CLI | `cliVersion 0.153.4`, `appServerVersion 0.149.1` |
| That job's launchd state | `state = not running, active count = 0` |

So the shared app-server and the user's own `codex` had been **different programs for thirteen days**, the version disagreement `shared_daemon` reports as `degraded` was on that machine every day of them, and the "updater the job follows" — the `current` symlink — was the thing that had gone stale. The job had also exited: `daemon start` waits for the server's initialize and returns, so the surviving server belonged to no launchd job and `launchctl print` could not answer whether the shared server was up.

## Decision

**Everything on this side that needs codex is handed three facts about the one codex the user already has, and nothing else.**

| Fact | What it is |
| --- | --- |
| `executable` | The codex the user gets when they type `codex`. |
| `control_socket` | `$CODEX_HOME/app-server-control/app-server-control.sock` — **derived**, never asked of a running process. |
| `launch_environment` | `CODEX_HOME`, plus a `PATH` that can find the executable's interpreter. |

They live in one module, `installation/codex_runtime.py`, with three consumers: the LaunchAgent that renders the job, the adapter settings that default the executable, and the engine's client of the shared server. The adapter-side imports are the one `adapters -> installation` direction ADR 0012 allows; installation still imports no part of the engine.

**The managed standalone tree, the `daemon` subcommands and `daemon version` all go.** The job runs `<the user's codex> app-server --listen unix://<control socket>`. `~/.codex/packages/standalone/` is left exactly where it is: it is codex's directory, not this product's.

### The seam is the socket, not the daemon

The server on the other end is nothing but `codex app-server --listen`, observed on the reference machine's process table. Upstream computes the control socket's path from `CODEX_HOME` with no variable and no flag over it, which is what makes it derivable rather than something to ask for. Two facts make a bare `codex` attach to it, and no more: a live JSON-RPC server on that path, and nothing else. Proved by file descriptor —

```
server  92722 fd 31u  unix 0x7f501a001ccccfc7  …/app-server-control.sock
tui      4258 fd 38u  unix 0x9fb89930e513c031 -> 0x7f501a001ccccfc7
```

— and by `thread/loaded/list`, which listed the thread a real terminal `codex` had just started, under the title codex itself gave it.

Upstream hard-codes the managed path in `managed_install.rs`, and `openai/codex#41188` is the open request to lift that; codex calls the `daemon` family "experimental". This decision does not depend on either: it uses none of that family.

### Resolution happens once, in the shell's own reading

The engine already runs on the user's real `PATH` — `ProcessLauncher.swift` reads it from their **login and interactive** shell (`LoginShellPath.swift`) and spawns with it. `InstallationRunner` now does the same, so the reconcile resolves codex with an ordinary `which` over that `PATH`, and the plist's `PATH` is that same value.

This was the ticket's one open design question and it was ruled, not assumed. The alternative — installation reading the login shell itself, which is what the prototype did — buys a second login-shell read on every reconcile **and a second implementation of a lesson that was learned the hard way**: `zsh -l -c 'command -v codex'` answers `~/.local/bin/codex` (the stale 0.149.1 symlink, from `.zprofile`) while `zsh -l -i -c` answers the npm 0.153.4 (from `.zshrc`, where nvm writes itself). A login-but-not-interactive shell resolves *a different binary from the one the user gets*, which is the same class of bug this decision exists to end. One reading, one implementation.

Two consequences fall out of resolving over a `PATH` rather than asking a shell for a name:

- **A shell function or alias is refused by construction.** `command -v` answers a name for one; `which` over a `PATH` only ever answers a file it found, with the executable bit set. #82's hazard is dissolved rather than guarded against.
- **The resolution is repeated, never recorded.** `LoginShellPath.swift`'s own ruling applies unchanged: a copy of something the user's shell already states goes stale the day they edit their profile. There is **no `config.toml` key** for the resolved path. The existing `[adapters.codex_app_server] executable` stays what it is — an override an operator may set — and its *default* becomes the resolved codex. A codex that moved (`nvm use`, an uninstall) is simply resolved again, which is what "re-resolve rather than fail" is with no staleness check to write.

### An `executable` alone is not a runnable launchd job

#271 proposed two facts. The prototype found a third, and it found it by failing. The npm codex is `@openai/codex/bin/codex.js` behind `#!/usr/bin/env node`; started under launchd, whose whole `PATH` is `/usr/bin:/bin:/usr/sbin:/sbin`, it died with

```
env: node: No such file or directory
```

and never held the socket. `daemon start` never met this because the managed standalone is a native binary. So the job carries the `PATH` the reconcile ran on. Nothing rendered is hard-coded ([#38](https://github.com/okqixiaobao727-design/GPT-VoiceCoding/issues/38)): writing launchd's own default into a plist would be a constant standing in for something the environment already states. The directory holding the executable is on that `PATH` by construction — it is where `which` found it — and nvm and npm put `node` in that same directory beside the shim. A Homebrew or standalone codex is native and needs none of this; the entry is harmless there, so there is no branch on install kind.

### launchd owns the process now

With `--listen` the job **is** the server: `state = running, active count = 1`, against `daemon start`'s `state = not running`. `KeepAlive` stays absent, so this is still not a supervisor and [#83](https://github.com/okqixiaobao727-design/GPT-VoiceCoding/issues/83)'s scope is unchanged — but `launchctl print` now answers "is the shared server up" truthfully, which it never did before.

### Status is a handshake, and it names no version

`bridge-install status` connects to the derived socket and completes a codex `initialize`. Measured at **1 ms** against a live server, against 139 ms for the subprocess it replaces. It reports three states — no socket, a socket nothing is behind, a server that answered — and **says nothing about versions**. The running server's version is available only as a substring of `userAgent`, and with one codex on the machine there is no second version for the first to disagree with.

**[#67](https://github.com/okqixiaobao727-design/GPT-VoiceCoding/issues/67)'s no-version-pin ruling is not reopened, it is dissolved.** There is no pair of versions. The `degraded` note for a version disagreement goes; everything else `degraded` carries stays.

### The protocol moved down, the transport did not

The handshake gave this product a second caller for its hand-rolled RFC 6455, and ADR 0012 forbids installation importing an adapter. Rather than write the framing twice — [#47](https://github.com/okqixiaobao727-design/GPT-VoiceCoding/issues/47)'s shape exactly — the upgrade request, the accept-key proof and the frame layout moved into `gpt_voicecoding/websocket.py`, a stdlib-only leaf beside `locations.py`. The engine's client stays `asyncio` because it lives in the engine's loop; installation's stays a blocking socket because it runs before any loop exists. The move was pure: `adapters/codex_app_server/wire.py` kept its behaviour and its public names, and every existing test of it passes unchanged.

## Migration

A machine that ran #82's job carries `com.gpt-voicecoding.codex-daemon` with the managed path and `daemon start` in it, and a running standalone server under it. The reconcile:

- **re-renders that same label.** One job, so two `RunAtLoad` plists can never race for one socket — which is precisely the state the prototype left the author's machine in, and the reason it was restored before this landed;
- **does not stop the running server.** #83's rule stands: the product never boots out a server the user's TUIs are attached to. The changed render is written and reported and takes effect at the next login, which is already what a changed render does here. Status says so in a sentence rather than reporting the job as current;
- **leaves `~/.codex/packages/standalone/` alone.**

## What this does not decide

**ADR 0020 is not reopened.** Its rule — a Codex Session is a daemon-held user root that a live terminal vouches for — is unchanged. What changed is who started the server holding those threads; see the vocabulary amendment at the head of that document. [#144](https://github.com/okqixiaobao727-design/GPT-VoiceCoding/issues/144)'s identity rule is untouched, and so is [#132](https://github.com/okqixiaobao727-design/GPT-VoiceCoding/issues/132)'s plist-SHA read-back.

**Installing codex for the user is out of scope, permanently.** The product uses the codex the user has and reports when there is none — the Codex lane says it is absent, with the reason, and the run carries on. Reviving generation 1's per-Session wrapped app-server (ADR 0010) stays dropped.

## Still owed to a real machine

Stated rather than implied, because this ADR is written on measurements and these are the ones not yet taken:

1. **A Live Call on the resolved executable.** Needs a person and a microphone; also [#270](https://github.com/okqixiaobao727-design/GPT-VoiceCoding/issues/270)'s scenario.
2. **A real upgrade with the job up.** `npm view @openai/codex version` was `0.153.4` on the day, which is what was installed, so there was no upgrade to run.
3. **Homebrew.** The `PATH` rule is argued for a native binary, not measured against one.

And one for whoever re-runs the acceptance harness's codex lane: **`approvalPolicy: "on-request"` with a `readOnly` sandbox no longer forces an approval request** on 0.153.4. #82's gate prompt produced none twice (90 s and 120 s waits) — the model answered in prose and never called the shell. `"untrusted"` produces the request immediately. A gate written on the old assumption **hangs rather than fails**.
