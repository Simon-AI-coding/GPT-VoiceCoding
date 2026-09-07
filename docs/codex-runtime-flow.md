# Codex Runtime — the flow from install to attach, run on this machine

**Issue:** #271, landed by #272 as [ADR 0022](adr/0022-the-shared-app-server-is-the-users-own-codex.md).
**Date of the run:** 2026-09-07. **Machine:** the author's Mac, macOS 26.6.2, arm64.

> **What this document is now.** It was written on branch `prototype/codex-runtime`
> against `scripts/prototype_codex_runtime.py`, a throwaway. That branch is not merged
> and is deleted with #272; this copy is kept because **it is the acceptance script**.
> Re-run it against the landed code. Everything below is as it was observed on the day,
> except where a box like this one says otherwise — nothing measured has been edited to
> match what shipped.
>
> Two names changed on the way in: `CodexRuntime` is now
> `src/gpt_voicecoding/installation/codex_runtime.py`, and the prototype's own
> login-shell resolution is **not** what landed. Simon ruled option (A) on #272: the
> Swift shell hands the login `PATH` it already reads to the installation subprocess,
> and installation resolves codex with an ordinary `which` over it. So step 1's finding
> stands — the reading must come from a **login and interactive** shell — but the code
> that does the reading is `shell/Sources/ShellCore/LoginShellPath.swift`, which already
> knew it, rather than a second implementation in Python.

**The question.** Can everything on this side that needs codex be handed exactly two
facts — `executable` (the one codex the user already has) and `control_socket`
(`$CODEX_HOME/app-server-control/app-server-control.sock`, derived) — with the
`daemon` subcommands, the managed standalone tree and `daemon version` all gone?

**Short answer: yes, with one addition #271 did not anticipate** — an `executable`
alone is not enough to start a job under launchd. See step 2.

---

## Starting state, before anything was changed

| Fact | Observed |
| --- | --- |
| The user's `codex` in an interactive terminal | `/Users/simon/.nvm/versions/node/v24.13.0/bin/codex`, `codex-cli 0.153.4` (npm) |
| A second `codex` earlier on the *non-interactive* login `PATH` | `/Users/simon/.local/bin/codex` → `~/.codex/packages/standalone/current/bin/codex`, `0.149.1` |
| Managed standalone tree | `current -> releases/0.149.1-aarch64-apple-darwin`, symlink untouched since 2026-08-25 |
| Shared server actually running | pid 875, `~/.codex/packages/standalone/current/codex app-server --listen unix://…`, started 09:24 by launchd |
| Live Call app-server | pid 12309, same 0.149.1 binary, `--enable realtime_conversation --listen unix:///tmp/gpt-voicecoding-501/codex-app-server.sock` |
| LaunchAgent | `com.gpt-voicecoding.codex-daemon`, `ProgramArguments` = managed path + `app-server daemon start` |
| That job's launchd state | `state = not running`, `active count = 0` — `daemon start` exited; the server it left behind was **not** a launchd job |
| Loaded threads on the control socket | 1 (a real session, cwd `~/Documents/coding/brainstorming`) |

Two of #271's claims are confirmed here as observations rather than readings:

1. **The daemon's server process is nothing but `codex app-server --listen`.** `ps` on
   pid 875 shows exactly that command line. The seam is the socket, not the daemon.
2. **The version seam is live.** `daemon version` asked through the npm CLI answered
   `cliVersion 0.153.4`, `appServerVersion 0.149.1` — the disagreement `shared_daemon`
   reports as `degraded` was on this machine every day since 2026-08-25.

---

## Step 1 — Install: finding the user's codex

**Commands.**

```
launchctl print gui/501/com.gpt-voicecoding.codex-daemon      # "default environment"
env -i HOME=$HOME PATH=/usr/bin:/bin:/usr/sbin:/sbin /usr/bin/which codex
zsh -l -c 'command -v codex'
zsh -l -i -c 'command -v codex'
python scripts/prototype_codex_runtime.py resolve             # under a launchd-like env
```

**Observed.**

- launchd's `PATH` for a LaunchAgent is `/usr/bin:/bin:/usr/sbin:/sbin` (read off
  `launchctl print`'s *default environment*). `which codex` under it: **nothing**. So a
  job cannot find codex on its own, and resolution through the user's shell is required
  rather than merely convenient.
- `zsh -l -c 'command -v codex'` → `/Users/simon/.local/bin/codex` (**0.149.1**).
- `zsh -l -i -c 'command -v codex'` → `/Users/simon/.nvm/.../bin/codex` (**0.153.4**).

  The difference is `.zprofile` vs `.zshrc`: `.zprofile` prepends `~/.local/bin`;
  `.zshrc` sources nvm, which prepends the node bin directory. **A login-but-not-
  interactive shell resolves a different binary from the one the user gets when they
  type `codex`.** The rule therefore has to be `$SHELL -l -i -c 'command -v codex'`.
  Measured: 0.54 s, no stderr, no tty required, stdin closed.
- Resolution under a launchd-like environment (`env -i`, system `PATH` only) still
  answers the npm binary, which is the point of asking the shell.
- With no codex anywhere (`HOME` pointed at an empty directory, so no rc file adds a
  path), `resolve` answers, and does not raise:
  `{"present": false, "reason": "/bin/zsh -l -i -c 'command -v codex' found no codex (status 1)"}`.
  That is the Codex lane reporting itself absent, which is what today's
  `_no_managed_binary` does and what #271 asks to keep.

**Path stability across an upgrade.** npm's `bin/codex` is a symlink to
`../lib/node_modules/@openai/codex/bin/codex.js`; `npm i -g` rewrites the target of that
symlink and leaves the symlink's own path alone, so a recorded
`/Users/simon/.nvm/versions/node/v24.13.0/bin/codex` survives an upgrade. It does **not**
survive `nvm use <other version>`: the path contains the node version. Only one node
version is installed here (`v24.13.0`), so the stale-path case could not be produced on
this machine — the risk is real but **not observed**. A resolution recorded once at first
launch (ADR 0012) needs a re-resolve when the recorded path stops existing.

**Verdict: works as designed, with a changed rule** — the resolving shell must be
`-l -i`, not `-l`.

## Step 2 — Login: the LaunchAgent starts the server itself

**Commands.**

```
launchctl bootout gui/501/com.gpt-voicecoding.codex-daemon; kill 875
# prototype job: <npm codex> app-server --listen unix://<control socket>
launchctl bootstrap gui/501 ~/Library/LaunchAgents/com.gpt-voicecoding.prototype-codex-runtime.plist
launchctl print gui/501/com.gpt-voicecoding.prototype-codex-runtime
```

**Observed, first attempt — it failed, and the failure is the finding.** The job died
instantly. Its whole log:

```
env: node: No such file or directory
```

The npm `codex` is not a binary: it is `@openai/codex/bin/codex.js` with
`#!/usr/bin/env node`. Under launchd's `PATH` there is no `node`. **An `executable` on
its own is not a runnable job.** `daemon start` never hit this because the managed
standalone is a native binary.

**The smallest fix, and the one the prototype adopts:** put the executable's *own
directory* in front of launchd's default `PATH` in the job's environment
(`CodexRuntime.launch_environment`). npm and nvm install `node` next to the shim
(`.../v24.13.0/bin/node` sits beside `.../bin/codex`). The directory must be the path as
resolved, **symlink intact** — following nvm's symlink lands in
`lib/node_modules/@openai/codex/bin/`, where there is no `node`. A Homebrew or standalone
codex is native and needs none of this; the entry is harmless there, so there is no
branch on install kind.

**Observed, second attempt.**

```
state = running
active count = 1
program = /Users/simon/.nvm/versions/node/v24.13.0/bin/codex
arguments = { … app-server --listen unix:///Users/simon/.codex/app-server-control/app-server-control.sock }
```

- The socket appeared at the derived path, mode `srw-------` (0600) — it passes the
  engine's existing `verify_private_socket`.
- Process tree: `node …/bin/codex app-server --listen …` (ppid 1, launchd's) with the
  native `…/codex-darwin-arm64/vendor/…/bin/codex` as its child. The **child** holds the
  socket. The Node wrapper comes up cleanly detached; nothing needed a tty.
- Log file: empty. No noise, no warning.
- **A lifecycle change worth naming.** With `daemon start`, launchd's job exited
  (`state = not running`) and the surviving server belonged to no job. With
  `--listen`, launchd owns the process for real: `state = running, active count = 1`.
  There is still no `KeepAlive`, so this is not a supervisor — but a `launchctl print`
  now answers "is the shared server up" truthfully, which it never did before.

**Verdict: works with a change** — the job must carry `PATH` = `dirname(executable)` +
launchd's default. Homebrew was **not** probed (no Homebrew codex on this machine).

## Step 3 — Attach: a bare `codex` joins it

The TUI was driven headlessly on a pty (`script(1)` with a FIFO for stdin), which is
what let this run without a human at the keyboard.

**Observed — attach happens, and is provable by file descriptor.** With the server at
pid 92722 and a bare `codex` at pid 4258:

```
server  92722 fd 31u  unix 0x7f501a001ccccfc7  /Users/simon/.codex/app-server-control/app-server-control.sock
tui      4258 fd 38u  unix 0x9fb89930e513c031 -> 0x7f501a001ccccfc7
```

The TUI is a client of the control socket held by a server that was started as a plain
`app-server --listen` from the **npm** binary, with no `daemon` subcommand anywhere in
the picture. This is #271's central claim, observed.

**Observed — a `codex` opened *before* the server is up stays embedded.** The job was
booted out, a TUI started (pid 10566), then the job bootstrapped. The server's only
control-socket descriptor stayed its listener; the earlier TUI held no peer to it and
never adopted the server. That reproduces #82's finding on this route, which is why the
job has to be a login item and not an engine-start step.

**The roster half, finished by hand.** `thread/loaded/list` lists *threads*, not clients,
so a TUI that has never been given a turn has no row: `roster` read `{"loaded": 0}` while
the headless TUI was attached, and driving a real turn through the pty failed (the TUI
exited on the injected input; three attempts, then abandoned). Simon opened a real
terminal `codex` in this repository and sent one message. `roster` then read:

```json
{"loaded": 2, "threads": [
  {"threadId": "01a078ec-6cc0-7042-9116-03028437c981",
   "title": "Respond to greeting", "cwd": "/Users/simon/Documents/coding/GPT-VoiceCoding"}, …]}
```

The thread id matches the one in the TUI's own status bar, and the title is the one codex
named the thread. A hand-started, unwrapped terminal Session is visible to this product
through a socket nothing on this side started with a `daemon` subcommand.

**And a property of the shared server showing through the TUI, unprompted.** One of the
later terminals printed `Disconnected from this task. Any running work continues.
Reconnect: codex resume 01a078f6-9941-7b42-8adc-7d611e3711e8`. The turn lives in the
shared server; the terminal is a client that may come and go. That is the same fact this
product depends on for a Relay, said by codex's own UI.

**Verdict: works as designed.**

> **Before re-running step 4's gate, read this.** `approvalPolicy: "on-request"` with a
> `readOnly` sandbox no longer forces an approval request on 0.153.4 — see the measurement
> at the end of this step. **Use `"untrusted"`.** A gate written on the old assumption
> hangs rather than fails, which costs the wait rather than reporting it.

## Step 4 — Join: the engine reaches the same socket

**Observed, and it happened by itself.** After the swap, `lsof -U` showed the client on
the server's accepted descriptor was:

```
python3.1  12308  fd 14u  unix … -> 0xfb711c7f13cab26c   (server 11319 fd 31, at the control socket)
```

pid 12308 is the **shipped, unmodified engine** (`GPT-VoiceCoding.app/…/python3 -m
gpt_voicecoding.engine`), started 09:34 — before the swap. It lost the 0.149.1 server,
re-located, and joined a server started from the npm binary by a job that never ran
`daemon start`. `shared_daemon`'s "locating is re-tried, not remembered" is what made
that free.

Two supporting facts:

- `daemon version` still answers against a plain `--listen` server: `status running`,
  `appServerVersion 0.153.4`, the right `socketPath`. It reports on the socket, not on
  the daemon that did or did not start it. So today's code is not *broken* by this
  change — the prototype removes the subprocess because it is unnecessary, not because
  it fails.
- With the npm CLI on both sides, `cliVersion == appServerVersion == 0.153.4`: the
  `degraded` version-disagreement note this lane has been carrying since August
  disappears, because there is one binary.

**#82's gate, re-run on this server** (`prototype_codex_runtime.py prove`, which finds the
socket by derivation and never calls `daemon version`), against Simon's live terminal
thread:

```
turn accepted: 01a078f4-2d4f-74e2-93a6-333cccc1b32a
approval observed: item/commandExecution/requestApproval id=0
  command: /bin/zsh -c 'echo codex-runtime-approval-proof'
approval receipt: serverRequest/resolved
relay readback copies: 1
commandExecution → aggregatedOutput "codex-runtime-approval-proof\n", exitCode 0
```

Relay delivered with exactly one readback copy of its `clientUserMessageId`; approval
requested, answered `accept`, receipted by `serverRequest/resolved`; the command ran and
the turn completed. That is the whole #82 gate, on a server started by the user's own npm
codex under launchd, reached through a derived socket path.

**One behaviour changed between 0.149.1 and 0.153.4, and the acceptance needs to know.**
#82's own prompt — "use the shell to `printf` into a temp file", with
`approvalPolicy: "on-request"` and a `readOnly` sandbox — produced **no approval request**
twice in a row here (90 s and 120 s waits). The turn completed with the model answering
in prose instead: first `proof complete` without ever calling the shell, then
`命令执行失败：无权限写入目标文件`. No `commandExecution` item appears in either readback.
Switching to `approvalPolicy: "untrusted"`, where every command needs approval, produced
the approval request immediately. So `on-request` + `readOnly` is no longer a reliable
forcing function for an approval; a gate that relies on it will hang rather than fail.

**Verdict: works as designed** — the engine's join proved on a live unmodified build, and
the Relay/approval gate re-proved end to end, with the approval-policy change noted above.

## Step 5 — Live Call — NOT RUN

The engine's own realtime app-server (pid 12309) is still the one it spawned at 09:34
from the 0.149.1 managed binary; it was left alone. Restarting the engine so it spawns
its Live Call server from the npm binary, and speaking one hand-off, needs a microphone
and a person. **Not run on this machine, stated rather than guessed.**

## Step 6 — Update — PARTIALLY RUN

`npm view @openai/codex version` → `0.153.4`, which is what is installed. **There is no
upgrade available today**, so "upgrade with the server running" could not be produced.

What *was* produced is the state an upgrade leaves behind — a client and a server of
different versions — because this machine has two codex binaries. A `0.149.1` TUI was
started against the running `0.153.4` server: the server accepted a client while it was
up (`lsof` showed one accepted descriptor beyond the listener that was not the engine's).
The reverse direction (`0.153.0` CLI against a `0.149.1` daemon) was already measured on
2026-09-05 and recorded in `shared_daemon.py`. Nothing here contradicts #67's no-version-
pin ruling; the running server stays whatever it was, a new terminal `codex` is whatever
the package manager installed, and the next login starts the newly resolved path.

**Verdict: the rule stands, but the upgrade itself was not run.** Re-run when an upgrade
is actually available; it is one `npm i -g @openai/codex@latest` with the job up.

## Step 7 — Status: what replaces `daemon version`

**Command.** `python scripts/prototype_codex_runtime.py status`

**Observed.** One connect + `initialize` on the control socket, **1 ms**:

```json
{
  "answering": true,
  "socket": "/Users/simon/.codex/app-server-control/app-server-control.sock",
  "elapsed_ms": 1,
  "initialize": {
    "userAgent": "gpt-voicecoding/0.153.4 (Mac OS 26.6.2; arm64) unknown (…)",
    "codexHome": "/Users/simon/.codex",
    "platformFamily": "unix",
    "platformOs": "macos"
  }
}
```

Against the old server the same call answered `gpt-voicecoding/0.149.1`. So the running
server's version *is* available without a subprocess — but **only inside the `userAgent`
string**; there is no version field. `daemon version` returns clean
`cliVersion`/`appServerVersion` fields and costs a process spawn (measured at 139 ms in
`shared_daemon.py`) plus a dependency on the `daemon` subcommand #271 wants gone.

**The trade to put to the ticket:** the status line becomes "the socket answered
`initialize` in 1 ms", which is a stronger fact than "a subprocess said running", and the
version comes off `userAgent` by string parse — or the status line simply stops naming a
version, since with one binary the CLI version is the one the product resolved.

**Verdict: works as designed, with one caveat** — the version is only available as a
substring.

---

## What the design must carry (revised)

`CodexRuntime` needs **three** members, not two:

| Member | Source |
| --- | --- |
| `executable` | `$SHELL -l -i -c 'command -v codex'`, resolved once at first launch, written to `[adapters.codex_app_server] executable` |
| `control_socket` | derived: `$CODEX_HOME/app-server-control/app-server-control.sock` |
| `launch_environment` | `CODEX_HOME` + `PATH` = `dirname(executable)` : launchd's default — **without it an npm codex cannot start under launchd at all** |

`managed_binary()` and `MANAGED_BINARY_PARTS` go. `shared_daemon.locate()` stops
spawning `daemon version` and connects to `control_socket`. The LaunchAgent runs
`<executable> app-server --listen unix://<control_socket>` and launchd owns the process.

## A side effect the version move brings with it: MCP plugins

When Simon's terminal `codex` came up against the new server it printed:

```
▲ MCP client for `cua_repl` failed to start: MCP startup failed: handshaking with MCP
  server failed: connection closed: initialize response
▲ MCP startup incomplete (failed: cua_repl)
```

**Investigated, not guessed.** `cua_repl` comes from the bundled ChatGPT plugin
`unified-computer-use`; its `.mcp.json` runs `node scripts/launch.mjs`. Run by hand, that
script fails with:

```
CUA REPL could not start: CUA_REPL_NODE_REPL_PATH must name an absolute executable
```

— **identically under a full interactive user environment and under a launchd-minimal one**.
So it is not a `PATH` problem and not this job's environment. `CUA_REPL_NODE_REPL_PATH` is
referenced only by `launch.mjs` itself and set by nobody in `~/.codex/config.toml`; the
ChatGPT desktop app supplies it when *it* runs the plugin.

What ties it to this change is the version move, not the socket: the plugin cache
directory `~/.codex/plugins/cache/openai-bundled/` has mtime **10:52 today**, minutes after
the 0.153.4 server came up, and now holds build `26.901.51231` only — while the ChatGPT
desktop app is still running `26.901.31953` and `config.toml` still records that older
build. The current codex refreshed the bundled plugins to a build the installed desktop
app does not configure.

**Impact observed: a warning line, nothing more.** The thread started, the turn ran, the
Relay and approval gate above passed on that same session.

**It did not reproduce.** Simon opened two more terminal `codex` sessions at 11:02 — no MCP
warning in either. `ps` shows no `launch.mjs` running from the new build `26.901.51231`;
the only ones alive are the ChatGPT app's own `26.901.31953`. Every bundled plugin
directory (`browser`, `chrome`, `unified-computer-use`) carries the same 10:52 mtime. So
the warning was a **one-off during that refresh**: the plugin was started once while its
cache was being replaced, failed its handshake, and later sessions do not start it at all.
A transient of the version move, not a standing fault.

**Still not proved: that the warning was absent before the swap.** It could not be read out of
any log (`~/.codex/log/` holds only a July login log; the daemon log holds only `daemon
start` JSON). The decisive test is one terminal `codex` against the restored 0.149.1 job.

## Open, and honest about it

1. Whether the `cua_repl` warning predates this change (see above). It has not recurred.
2. Live Call (step 5) — needs a microphone.
3. A real upgrade (step 6) — needs a release newer than 0.153.4.
4. Homebrew was never probed; the `PATH` rule is argued for it, not measured.
5. A resolved npm path goes stale on `nvm use`; only one node version exists here, so
   the re-resolve trigger is designed, not observed.

## Acceptance against the landed code

The steps above are the script. Re-run them in order; what each one is now checking:

| Step | Against the landed code |
| --- | --- |
| 1 Install | `bridge-install status` names the codex the user's terminal names. The resolution is the shell's (option A), so a mismatch here is a `LoginShellPath` question, not a Python one. |
| 2 Login | `launchctl print gui/$UID/com.gpt-voicecoding.codex-daemon` reads `state = running, active count = 1`, its `program` is the resolved codex, and its `EnvironmentVariables` carry `PATH` and `CODEX_HOME`. |
| 3 Attach | A bare `codex` in a terminal, then `lsof -U` for a peer on the server's control-socket descriptor. |
| 4 Join | The engine's own pid appears as a second client, and #82's Relay/approval gate passes — **with `approvalPolicy: "untrusted"`**. |
| 5 Live Call | **Still owed.** Needs a person and a microphone (#270). |
| 6 Update | **Still owed.** Needs an npm release newer than 0.153.4. |
| 7 Status | `bridge-install status` says the shared app-server answered `initialize`, and names no version — that is the ruling, not an omission. |

## Restoring this machine

The prototype job was `com.gpt-voicecoding.prototype-codex-runtime`, and the machine was
put back the way #271 found it before #272 landed. Kept for the record:

```
launchctl bootout gui/501/com.gpt-voicecoding.prototype-codex-runtime
rm ~/Library/LaunchAgents/com.gpt-voicecoding.prototype-codex-runtime.plist
launchctl bootstrap gui/501 ~/Library/LaunchAgents/com.gpt-voicecoding.codex-daemon.plist
```

The original plist was never edited or deleted.
