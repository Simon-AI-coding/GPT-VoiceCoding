# GPT-VoiceCoding

Voice-controlling terminal coding agents through a realtime voice call: the system
speaks agent progress to you, and carries your spoken instructions back to the
agents.

> **Status: early build.** Every part is now built and tested — Bridge Core, the
> control plane, the composition root, the Codex and Claude Agent adapters with
> all three Relays, the bridge-owned Live Call, the Companion Channel, the
> menu-bar shell, and the signed app bundle. Starting and closing sessions are
> deliberately not among them: v1.0 is a bridge over the sessions you start
> yourself, and the Session Launcher is parked for a later release. It has not
> been through a release. If you want software with users on it, the
> first-generation implementation lives at
> [GPT-VoiceCoding-legacy](https://github.com/okqixiaobao727-design/GPT-VoiceCoding-legacy).

## Install

On an Apple Silicon Mac, run this in your terminal:

```sh
curl -fsSL https://raw.githubusercontent.com/okqixiaobao727-design/GPT-VoiceCoding/main/scripts/install.sh | sh
```

You need Apple's Command Line Tools (`xcode-select --install`), Python **3.12 or
newer** available as `python3` on your PATH, and HTTPS access to GitHub and PyPI.
The script checks these in order and stops with a fix if one is missing; it
does not install Homebrew or Python. It builds the full ad-hoc-signed app from
source under `~/Library/Application Support/GPT-VoiceCoding/source`, quits a
running copy, places the new bundle in `/Applications`, and opens it. There is
no notarized download.

**Upgrade by running the same command again.** Your existing `config.toml` and
Telegram credential file stay in place. First launch creates a configuration
only if none exists, then walks through Codex login, the installed helpers,
Call Agent, optional Telegram, and a test call. Have Codex installed and log in
with `codex login` when the guide asks. Later launches are silent. Settings that
need an engine restart show one explicit **Restart now** action.

Telegram setup (in the guide or Settings) first checks the bot token. The token
identifies the bot, not the receiving chat. Search for the displayed `@username`
in whichever Telegram client you already use and send `/start` after the token
check. Return to the app, click **I've sent /start — check**, and save after you
see the confirmation message in that chat. The app does not open a Telegram
client for you or check for that message until you click.

The Duty Card starts at the top right of the available screen on a fresh
installation; subsequent launches restore its saved position.

## What it is

You are away from the keyboard. A Claude Code or Codex session stops and needs
you. GPT-VoiceCoding tells you — out loud, in a voice call it holds open — and
carries your spoken answer back into the session. It watches the sessions you
start, relays your words into them, and answers their permission prompts with
your verdict. It does not start them — you do, with your own `claude` or `codex`
command, and the bridge covers whatever is running.

The voice call is owned by this system directly, over `codex app-server`'s realtime
route. There is no API key to supply.

## Shape

A thin Swift menu-bar app spawns a single Python asyncio engine from inside its own
bundle. That engine is **Bridge Core**: it owns every policy and holds the single
source of truth, and reaches everything else — the call, the coding agents, the
Companion Channel — through seams with swappable adapters.

```
src/gpt_voicecoding/
├── core/           Bridge Core — the hub. All policy, all state.
├── seams/          The interfaces Bridge Core calls through, and the control plane's vocabulary.
├── adapters/       The implementations behind them. Protocol libraries live only here.
├── control_plane/  The JSON-over-UDS surface: framing, sockets, translation.
├── engine/         The composition root — config in, one running engine out.
├── installation/   What this product puts in files the user owns, and takes back.
└── cli/            bridgectl — a control-plane surface.
shell/              The Swift menu-bar shell (see ADR 0005).
app_bundle/         The build pipeline. Builds the product; is not part of it.
```

One command turns a clean checkout into a signed `.app`:

```bash
scripts/build-app.sh
```

See [`docs/app-bundle.md`](docs/app-bundle.md).

The engine runs standalone, without the menu-bar shell:
`python -m gpt_voicecoding.engine --config <file>`. The Live Call's audio path is
an optional extra — `pip install 'gpt-voicecoding[voice]'` — because everything
else runs without a compiled media stack. See
[`docs/control-plane.md`](docs/control-plane.md) for the interface, the command
set and the configuration file.

Start with [`docs/adr/0001`](docs/adr/0001-hub-and-spoke-bridge-core-with-seams.md).

## Platform

macOS only. The microphone grant, the menu-bar shell and the push path are all
macOS-shaped; cross-platform support is not planned.

## Reading order

1. [`CONTEXT.md`](CONTEXT.md) — the vocabulary. Every term in this repo means what
   it says there.
2. [`docs/adr/`](docs/adr/README.md) — the decisions, and where each one came from.
3. [`docs/control-plane.md`](docs/control-plane.md) — the interface every surface
   speaks, and what the engine is configured with.
4. [`docs/app-bundle.md`](docs/app-bundle.md) — how the `.app` is built and
   signed, and every decision that pipeline holds.

## Contributing

v0 targets developers: build from source, no signed release, no notarization
(ADR 0005). Issues and pull requests are welcome once there is something to build
on — the build issues land next.

## Licence

[MIT](LICENSE).
