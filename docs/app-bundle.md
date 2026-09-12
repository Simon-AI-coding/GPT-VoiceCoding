# The app bundle

One command, from a clean checkout, to a signed `GPT-VoiceCoding.app`:

```bash
scripts/build-app.sh
```

That is the whole interface. The pipeline itself is [`app_bundle/`](../app_bundle),
in Python, under the project's own test suite; the script is one line that runs
it. Three other things it will do:

```bash
scripts/build-app.sh --debug            # a debug build of the shell
scripts/build-app.sh --without-engine   # the shell alone, for the developer loop
scripts/build-app.sh lock               # regenerate the dependency lock
```

`--without-engine` is the successor to the old `dev-app.sh`. It builds a bundle
the shell's own resolver falls through past, to `GPTVOICECODING_ENGINE_PYTHON` or
`PATH` — the developer path, which is a stated feature rather than a degraded
build.

After the engine is installed, the pipeline inspects every ordinary executable
text file in `engine/bin/`. A Python console script whose shebang names the
interpreter in this build tree is given a shell/Python preamble that re-executes
the `python3` beside the script; shell scripts and binaries are left as they are.
There is no list of console-script names, so a script added by a future locked
dependency takes the same path automatically. Before signing, the pipeline also
checks every text file in the assembled `.app` and refuses the build if any one
still names the source checkout. The real bundle build in CI runs that same
check.

## Desktop shell (#360)

The existing `ControlPanel` owns control-plane readings and the facts the views
display; `ShellModel` owns visibility and one shared poller. AppKit owns the
non-activating Duty Card and the one normal window; Home, Session Brief, and
Settings replace each other inside that window. Diagnostics is complete here;
the other five Settings groups and first-launch setup belong to #361.

The design tokens are carried into one named Swift palette, with system-driven
dark and light colours. This is the maintainer-approved exception to #360's
asset-catalog wording: the existing Command Line Tools-only build remains the
contract, without `actool`, an Xcode dependency, or a custom asset loader.
The four design marks are bundled resources. Tests use the existing scripted
control-plane seam and socket double, and inspect panel configuration rather
than driving the window server.

Configuration is read through the existing Python configuration module in a
one-shot subprocess, using the shell's existing command runner. The result
contains only the display fields; Swift does not duplicate TOML validation or
the engine's defaults, and the read never writes the user's file or starts an
adapter. This is not an additional control-plane action. A failed read opens
the fixed settings-unreadable page and leaves details under Diagnostics.

### Settings and first launch (#361)

The configuration file remains user-owned. The shell edits named values in
place, preserving comments and keys it does not own; Python remains the one
parser and validator. Settings and onboarding share the same controls and save
path. Engine-setting saves wait for an explicit restart through the existing
supervisor; runtime switches, the login item and language do not request one.

Maintainer decision: an unchosen Call Agent model is a readable Settings state,
not a reason to replace the whole page with the unreadable-file screen. The
Settings reader permits an absent model and reports it as unchosen, without
writing a default. Engine startup still requires a chosen model. Both readers
share parsing and field validation; other invalid configuration still fails.

Telegram's returned bot name is kept in `[shell.telegram] bot_name`, in the
same configuration file and outside the adapter's settings. The token remains
in the private environment file. Unbind removes both Telegram tables and the
token file. The maintainer explicitly excluded old-binding migration and name
fallbacks: implement the new binding flow, without a background name lookup.
Maintainer decision: Unbind preserves handwritten standalone comments even when
their former Telegram table is removed; do not infer which table owns a comment.

When the user changes the Call Agent model, keep the chosen effort only if the
new model supports it. Otherwise remove the effort key and display "not chosen"
until the user picks; never choose the first offered effort on their behalf.

First launch uses the bundled configuration example as its single template,
with the actual bundled delegate CLI filled in. Only an absent file is created.
The six-step flow stays in `ShellModel`; Call Agent and Telegram reuse Settings
controls. The folder-access notice is visible in the Codex step, before moving
to the installation report and starting the engine's first workspace sighting.
The existing Installation subprocess's per-item outcome lines supply that
report; the shell does not repeat installation or infer success from exit alone.

Maintainer decision: the installer requires Python **3.12 or newer**, matching
the existing build entry point. This supersedes #361's 3.10 prerequisite; no
additional bootstrap interpreter or compatibility path is introduced.

Tests exercise the shell model through the scripted control plane and existing
process doubles, and the editor as pure text in/text out. Each setting's save
and restart path is one sequence, using real temporary configuration files.
Installation prerequisites and real calls belong to the manual checklist, not
an automated run against the developer's machine.

## What ends up inside

```
GPT-VoiceCoding.app/
└── Contents/
    ├── Info.plist                    the bundle's identity, copied from shell/Resources
    ├── MacOS/GPTVoiceCodingShell     the menu-bar shell, and nothing else
    └── Resources/
        ├── config.example.toml       the shell's first-launch template
        └── engine/                   python-build-standalone, the locked wheels, the engine
            └── bin/
                ├── python3 -> python3.12    what the shell spawns
                ├── bridgectl                what `[delegate] cli` names
                └── bridge-install           install, uninstall, status (ADR 0012)
```

The engine is inside the bundle because that is what earns the microphone grant
— see [ADR 0005](adr/0005-the-engine-lives-inside-the-app-bundle.md). Bundle
containment is the mechanism; the process tree is not.

## Why the pipeline is a plan and a doing side

`codesign --deep` is deprecated for signing, and never discovers
`Contents/Resources` anyway — so the set of things to sign is **enumerated**, not
delegated to a flag. `--deep --strict` is still the right thing to *verify* with,
and the build runs it, but it is not what finds the files.

That makes the enumeration and the signing order the two facts nothing else can
check. They are decided in `app_bundle/plan.py` and `app_bundle/signing.py`,
which read and never write, spawn or download, and they are asserted by
`tests/test_app_bundle.py`. `app_bundle/run.py` executes the result in order and
stops at the first failure.

The size of what is at stake: a bare python-build-standalone tree carries **11**
Mach-O files, and the engine with its voice extra carries **85** — the wheels are
almost all of it, and that set changes shape every time the lock is regenerated.
A missed `.so` under `lib-dynload`, or an app signed before its own contents,
produces a bundle that verifies clean and fails at the one moment it matters.

## The decisions this pipeline holds

| Decision | Why |
| --- | --- |
| python-build-standalone `install_only`, not a framework CPython | A framework CPython re-executes `Python.app/Contents/MacOS/Python` from its original location, so the process that ends up running is *outside* the bundle even though the shell spawned the one inside it. |
| Pinned by release tag **and** SHA256, and a hash-pinned lock for the wheels | A pipeline whose job is to sign a set of Mach-O files must not let an unpinned upstream change that set underneath it. Reproducibility here is a signing-integrity property, not a convenience. |
| One lock per host triple, and a refusal when there is none | A wheel's hash is architecture-specific, so one lock cannot cover two. Falling back to an unpinned install would mean signing binaries nobody reviewed. |
| Single-architecture, read from the build host | python-build-standalone publishes no `universal2` build, so a universal `.app` means lipo-ing two interpreters and two sets of wheels. Adoption-era work, which route (a) accommodates later without architectural change. |
| Ad-hoc signature, no Developer ID, no notarization | Charter decision 9: v0 targets developers. The accepted cost is that the signature changes per build, so a *new build* may re-prompt for the microphone. |
| Hardened runtime **off** | It buys nothing v0 ships — it is what notarization needs — and it is the most likely cause of the CFFI audio-callback crash the `allow-jit` escape hatch was reserved for, whose usual companion fix (`disable-library-validation`) is forbidden. It becomes the notarization-era ticket's decision, where `allow-jit` becomes live again. |
| `com.apple.security.device.audio-input` on the bundled interpreter only | Entitlements go on executables, and `python3.12` is the process that opens the device. Without the sandbox or the hardened runtime it is **inert** — belt and braces, deliberately, so the bundle is already the right shape later. It is not what earns the grant. |
| Bytecode pre-compiled at build time | Nothing may write into the bundle at runtime. The shell already sets `PYTHONDONTWRITEBYTECODE` for the bundled interpreter it spawns; this covers the two cases it cannot see — the engine run headless from a terminal, and the relocated `bridgectl` — and it also means a start does not recompile from source into memory. |
| Every Python console script is relocated, without a name list | `pip` writes an **absolute** shebang. The interpreter relocates; its scripts do not. The pipeline examines every executable text file in `engine/bin/` and replaces only a shebang that names this build's bundled interpreter. `bridgectl`, `cffi-gen-src`, `pyav`, and any future locked dependency script therefore share one mechanism. |
| The assembled bundle must not name its source checkout | A build-tree path works on the build machine and dies when that checkout or worktree is removed. The final pre-signing check scans the whole `.app`, and CI's real bundle build fails if any text file still carries the source root. Local-install provenance (`direct_url.json`) is removed because it would otherwise violate the same invariant even though the engine never reads it. |
| The user's `PATH` is read from an **interactive** login shell, delimited by sentinels | launchd hands a Finder-launched `.app` `/usr/bin:/bin:/usr/sbin:/sbin`, and the engine inherits it — which is the `PATH` it resolves the agent binaries on (`shutil.which`). A Session is not in this picture: the user starts one in their own terminal, and it carries that terminal's `PATH`. The fix reads the user's own shell — but `-lc` was the wrong question: zsh sources `~/.zshrc` only when interactive, and `~/.zshrc` is where `nvm`'s installer and `brew shellenv` actually write. `-i` reaches that page of the ledger; the sentinels are what make an interactive shell's chatter (powerlevel10k's instant prompt) separable from the answer. Same shape VS Code's shell integration uses. `.zprofile` is a steadier home for a `PATH`, but a product that only works for users who already knew that is broken for the majority. |
| `config.example.toml` is the first-launch template | The shell creates a config only when absent, with the approved defaults and actual bundle CLI. The file then belongs to the user; the shell edits known settings in place and the engine only reads it. The installer never replaces it. |

## Regenerating the lock

```bash
scripts/build-app.sh lock
```

It resolves against the **bundled** interpreter's own version and platform,
because a lock resolved by some other Python is a lock for some other set of
wheels. It writes `app_bundle/locks/<triple>.lock`. Read the diff: it is the list
of binaries the next build will sign.

## Manual acceptance (#358–361)

This is a **person's release checklist**, documented but never run by CI.
Automated tests and an ad-hoc signature do not prove a real microphone, call,
Telegram chat, or macOS permission prompt. Use a clean Apple Silicon Mac, with
a bot no other engine is polling. Back up any existing configuration before
testing first-launch or malformed-file cases.

1. **Prerequisites.** Exercise each installer refusal separately: Intel or a
   translated terminal, missing Command Line Tools, absent or older-than-3.12
   `python3`, GitHub unreachable, and PyPI unreachable. Each stops before
   cloning and gives one sentence with a fix. No Homebrew or Python is installed.
2. **Install.** Run the README's one command. The source stays under Application
   Support; the full app appears in Applications and opens Welcome. The generated
   configuration has the real adapters, null Companion Channel, bounded log,
   actual bundled delegate CLI, and `gpt-5.6-terra` / `low`, with no Codex path.
3. **Six steps.** Follow Welcome → Codex check → What was placed → Call Agent →
   Telegram → test call. Check the filled-square progress row. Exercise absent
   Codex and logged-out Codex separately, their fixes, Re-check and Skip. Read the
   folder-access notice before the first project sighting and Documents prompt.
   The installation report must match what actually succeeded or was absent,
   explain both helpers and reversibility, and offer Continue but no Skip.
   Skip Telegram once and prove the engine and test call still work.
4. **Permissions and first call.** Place the call from step 6. The microphone
   prompt must name GPT-VoiceCoding and appear over the Control Panel; the phase
   stays inside that step. No Verify is run. Test audio with a person, end the
   call, and finish on Home. Reopen Codex check from Diagnostics and the model
   and Telegram controls from Settings.
5. **The desktop.** Start Claude Code and Codex sessions yourself in ordinary
   terminals. Let one stop. Check the Duty Card, Home's counts and recency order,
   and the whole Session Brief. Child Processes and Headless Runs contribute to
   their count, never rows. Drag the card, change Spaces and enter a full-screen
   app; verify visibility and its saved position. Duty off hides it, not a
   separate hide preference. Check click, click-away and Quit behaviours.
6. **Call phases.** On a real call check Ready, Calling…, On a call, Ending… and
   Couldn't connect, including durations and the failure hold. Check the same
   phase words on Home and the card, the Voice Switch, and a fresh Call Agent
   versus a continued system-placed call. Check New Call Agent and Quit's
   on-call confirmations. Do not turn a failed external service into a pass.
7. **Settings and restart.** Change Voice, realtime model, Call Agent model and
   effort, and each of the three timings. Inspect the expected TOML table while
   comments and unknown keys remain unchanged. One restart line persists until
   the replacement engine answers. Save during Calling…, On a call and Ending…:
   Restart after the call is disabled and the edit is kept. The four switches
   and login item stay live and raise no line. With Auto Hang-up off, silence
   seconds are dimmed but retained. A model missing from the offered list is
   kept and noted; no model means not chosen; engine down disables picking.
   Switching to a model without the old effort clears it and waits for a choice.
   Re-picking a value that is already selected changes nothing and raises no line.
8. **Telegram.** Paste a valid token, see its bot name, use Open in Telegram and
   press Start. Receive one confirmation before Save; neither file changes
   before Save. Save writes the named environment variable privately (0600),
   adapter reference, chat id and bot name, and waits for Restart now. The saved
   token stays masked. Change to the **same token** and repeat without competing
   pollers. Leave a waiting flow and re-enter it; ordinary polling resumes.
   Exercise invalid token and network failure. Unbind removes both Telegram
   tables and the token file and chooses the null channel, with a restart line.
   With a bound bot, stop a Session and compare its state word and ordering on
   Telegram beside the app; answer there and check delivery.
9. **Language and appearance.** Run the complete settings and setup screens in
   English and Chinese, in system light and dark modes. Check long names and
   Chinese text fit without clipping. Language changes the preference now and
   the displayed language only after app relaunch. No third language or theme
   switch appears.
10. **Upgrade and failures.** Run the same install command again. Confirm the
    running app quits before replacement and reopens silently; the hand-written
    configuration is untouched and onboarding is skipped. Test a malformed file:
    the fixed unreadable-settings page opens, Diagnostics has the detail, and
    the file is unchanged. Check engine-down Settings display saved values and
    cannot bind Telegram or select a model. Copy diagnostics and inspect it for
    completeness and absence of credentials.

Record the build revision, machine, language and observed result for each step.
A skipped or blocked physical check stays unverified; test-suite success never
fills it in.

## Before a release: the microphone

The one part a machine cannot finish. macOS shows the TCC prompt to a person.

```bash
python3 scripts/microphone_grant_proof.py --reset
```

It prints the checklist and what each step must show. The probe that gated
ADR 0005 already established the negative control — an interpreter outside any
`.app` collapses to the bare binary path — so that half is deliberately not
re-run: it is a property of macOS, not of this bundle.

## Known v0 limitations

Deliberate, and written down so they are not rediscovered as bugs.

**Nothing ends an engine it did not start.** Kill the shell abnormally and the
engine is orphaned, and there is no supported way to stop it but `kill` on the
process. Note this is about the *engine*, not the Sessions it launched: under the
`direct_child` launcher a Session is a direct child of the engine and goes with
its process group, so quitting normally takes the agents with it rather than
leaving them behind. Two reasons, both load-bearing rather than incidental:
`bridgectl` is a *control-plane surface* — status and switches — and giving it a stop verb would
make the control plane a lifecycle owner, which is not what it is; and a
relaunched shell holds no handle on a process it did not spawn, so its Quit
stops its own child and it has none, having refused to start one against the
live socket. `SIGTERM` is what the engine's own signal handling is for: loops
cancelled, socket removed, no debris. If field evidence ever shows the orphan
case is common enough to hurt, that is a reopening with evidence, not a
convenience feature.

**An update may re-prompt for the microphone.** Ad-hoc signatures change per
build. Charter decision 9, accepted; it waits for notarization.

**`bridgectl verify` proves wiring, not that a call can be placed.** Each seam's
verify reports which implementation is loaded and whether that seam's far side
answers — for the Call seam, whether the `codex app-server` responds. It does
**not** establish a realtime session, because a health check that did would open
the microphone, spend a realtime session and need a teardown path. So
`call: pass — the call is down` means "the wiring is sound and no call is
currently up", not "a call can be placed": a refusal that lives further out, at
the realtime backend, is invisible to it. **The first real call attempt is that
proof**, and when it fails the reason is reported verbatim — which is the sentence
to read, and to quote in a bug report.

**Retry with the *same* `--request-id`, never a fresh one.** A launch is held as
a transaction keyed by its request id, so re-issuing the identical command joins
the launch already in flight and returns the Session it produced. A fresh id
describes a *different* launch, and the engine will honour it — starting a
second agent in the same workspace. The identity is the safety mechanism, which
is why the flag is required rather than generated for you.

**A first-generation codex skill silently hijacks the voice thread.** Anyone
upgrading from the first generation has one, and this engine has no way to
refuse it — see the cutover note. Left in place it does not break anything
visibly; it just makes the Live Call drive a control plane that is not here, and
explain itself perfectly while doing so. Retiring the skill is a step in the
cutover rather than a fix in the code, because the file belongs to the user and
so does the decision to keep it.

**Only configured projects can be launched by voice.** #25 added the project
catalogue this entry once deferred: `[[launch.projects]]` in `config.toml` maps
a canonical name and explicit spoken aliases to an absolute workspace, so a cold
workspace *is* now launchable by name — the launch verb takes a project
reference and a task, resolves them through the catalogue, and applies the
configured default agent unless one is named. What remains a limitation is the
catalogue's edge: a workspace with no `[[launch.projects]]` entry cannot be
launched at all — the control-plane launch action refuses raw `workspace` and
`label` fields, and a spoken path resolves to nothing. Adding the entry to
`config.toml` is the supported route, not speaking the path.

## Cutover: retire the first generation's codex skill

**Before the acceptance, and before any real use, check
`~/.codex/skills/` for a first-generation skill and move it out.** On the
reference machine it was `~/.codex/skills/gpt-voicecoding/`, six files, and it
took three launches to notice.

**Check for the first generation's whole runtime too, not only its skill, and
identify it by what it is rather than by where it was last seen.** It has been
found installed at `~/Library/Application Support/GPT-VoiceCoding/runtime/` —
inside *this* product's own directory, which is exactly where an operator will
not think to look, and where a check written against some other address returns
a confident CLEAN. Two tests settle it wherever it turns up: a `bridgectl` whose
verbs are the first generation's (`serve`, `duty-toggle`, `session-label`,
`stop`, `stops`, `install-hooks`) rather than this engine's (`status`,
`switch`, `brief`, `live`, `verify`); and a `.source-revision` that
`git cat-file -t` cannot resolve in this repository, which means it was built
from another codebase. An installed runtime whose daemon is not running is
still worth knowing about before you attribute anything.

The Live Call's voice thread runs on a codex app-server, and codex loads skills
from the user's own directory. A skill written for the first-generation bridge
describes a *different* control plane: another binary
(`…/GPT-VoiceCoding/runtime/bridgectl`), verbs this engine does not have
(`launch --list`, `launch --destinations`), and a concept it has no equivalent
for — a catalogue of "shortcuts" mapping a spoken project name to a directory.
The engine cannot prevent this: the skill is the user's file and codex loads it
before any of this engine's own instructions are in play.

**The hijack is silent, which is what makes it expensive.** The model does not
malfunction — it follows the wrong procedure correctly, and its explanations
sound reasonable, because they *are* reasonable under the rules it was given. On
the reference machine it read a retired skill's step 1, called `launch --list`,
got an argparse usage error, and then declined to improvise because that skill's
own rule says a failed launch must not be retried. Every sentence it said was
true of the system it thought it was driving.

The test is the rollout: find the call's rollout under `~/.codex/sessions/`
(its filename carries the Live Call id `bridgectl status` prints) and look for
the first generation's `bridgectl` path or `launch --list`. Either one means the
voice thread is not being driven by this engine's instructions, and nothing it
does can be attributed to this engine until the skill is gone.

## Cutover: one bot, one engine

Telegram permits exactly **one `getUpdates` consumer per bot**. Do not point the
new engine's Companion Channel at a bot something else is already polling: the
two steal each other's inbound messages, `verify` still passes because `getMe`
and `getChat` are unaffected, and only inbound goes quiet. Stop the other
consumer first, or use a distinct bot.

The first-generation bridge was named here as the likely contender, and on this
machine it is not one — its `companionChannel` is configured with an empty
`module` and no credentials, so it has no Telegram channel to hold a bot with.
Anything else polling the same bot — a second engine, a script, another machine —
still would.
