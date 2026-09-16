# GPT-VoiceCoding

**for Claude Code & Codex**

![macOS](https://img.shields.io/badge/macOS-only-111111)
![Apple Silicon](https://img.shields.io/badge/Apple%20Silicon-required-111111)
![Licence](https://img.shields.io/badge/licence-MIT-111111)

<!-- PENDING ASSET: the demo video. Drag scratch/demo-recorder/out/take.tvuVMJ/demo.mp4
     into any GitHub comment box, then replace this whole comment with the
     https://github.com/user-attachments/... URL it returns, on a line of its own with
     no Markdown around it, followed by:

     *Recorded unattended; the narration and the spoken lines are synthesized speech.*
-->

One voice call covers every Claude Code and Codex session on your Mac: it tells you which one
stopped and what it stopped on, and carries your spoken answer back into it. The call rides
`codex app-server`'s realtime route, so it runs on the ChatGPT account you already signed
`codex` into, and there is no API key to supply.

## How it works

- A session stops and it calls you. The bridge watches the sessions you started yourself; it
  does not launch them and does not wrap them.
- You answer by voice. You hear what the session stopped on, say what to do, and your words go
  back in as an instruction that session can act on.
- Away from the desk, you answer on Telegram. Every stop arrives there too, and a reply to it
  reaches the same session.

## Screenshots

![The Duty Lamp, Settings, the Control Panel and Telegram](docs/images/showcase.png)

The Duty Lamp floats above everything and counts what is waiting on you, with Settings below it.
In the middle is the Control Panel during a call. On the right is Telegram, where you reply to a
stop notice and `/sessions` comes back as buttons.

## Install

On an Apple Silicon Mac:

```sh
curl -fsSL https://raw.githubusercontent.com/Simon-AI-coding/GPT-VoiceCoding/main/scripts/install.sh | sh
```

It builds an ad-hoc-signed app from source, puts it in `/Applications` and opens it. There is no
notarized download. Run the same command again to upgrade; your configuration and Telegram
credentials stay where they are.

You need:

- An Apple Silicon Mac, with Apple's Command Line Tools (`xcode-select --install`).
- Python 3.12 or newer, as `python3` on your PATH. The installer checks for it and stops with a
  fix rather than installing one.
- Claude Code or Codex, whichever you use. Both work, together or alone.
- Codex signed in with a ChatGPT account (`codex login`). The Live Call needs it.
- Telegram, if you want the second route. It is optional, and first launch walks you through it.

## Architecture

A thin Swift menu-bar app spawns one Python asyncio engine from inside its own bundle. That
engine is **Bridge Core**: it owns every policy and holds the single source of truth, and
reaches everything else through seams with swappable adapters.

The two agents are reached differently because they publish themselves differently. Claude Code
is read from its own roster and two hooks, and written to through the inbox socket every session
already binds. Codex is read and written through the shared `codex app-server` this product
starts and your own terminals join, which is the same process the Live Call's realtime route
rides.

```mermaid
flowchart LR
  subgraph terminals["Your own terminals"]
    cc["Claude Code Session"]
    cx["Codex Session"]
  end

  appserver["codex app-server<br/>started by this product,<br/>joined by your Sessions"]

  subgraph engine["The engine"]
    core["Bridge Core<br/>every policy, one source of truth"]
    agents["Agent adapters"]
    callad["Call adapter"]
    chan["Companion Channel adapter"]
    plane["Control plane<br/>JSON over a Unix socket"]
    core --- agents
    core --- callad
    core --- chan
    core --- plane
  end

  live["Live Call<br/>Voice speaks, Call Agent acts"]
  tg["Telegram"]
  shell["Menu-bar app<br/>Duty Lamp, Control Panel"]
  ctl["bridgectl"]

  cc <-->|"hooks, roster, inbox socket"| agents
  cx --- appserver
  appserver --- agents
  appserver -->|"realtime route"| callad
  callad --- live
  chan --- tg
  plane --- shell
  plane --- ctl
```

What happens when a session stops:

```mermaid
sequenceDiagram
  participant S as Your Session
  participant C as Bridge Core
  participant T as Telegram
  participant K as Call Keeper
  participant V as Voice
  participant A as Call Agent
  participant U as You

  S->>C: stops, waiting on a decision
  C->>C: writes the reading to the roster
  C->>T: Stop Notice, as an Anchor you can reply to
  C->>K: wake
  K->>V: dials, briefed from the roster as it stands
  V->>U: speaks the Session Brief
  U->>V: "use email login, and give me the steps"
  V->>A: hands the job over
  A->>C: relay
  C->>S: Answer Relay
```

Or, without the call:

```mermaid
sequenceDiagram
  participant T as Telegram
  participant U as You
  participant C as Bridge Core
  participant S as Your Session

  T->>U: Stop Notice
  U->>T: replies to it
  T->>C: inbound text, naming the Anchor's Session
  C->>S: Answer Relay
```

The Voice has no tools. It speaks from what the engine hands it and passes anything that reads
as a job to the Call Agent, which is the only half on the call that can run a control-plane verb.

```
src/gpt_voicecoding/
├── core/           Bridge Core — the hub. All policy, all state.
├── seams/          The interfaces Bridge Core calls through.
├── adapters/       The implementations behind them. Protocol libraries live only here.
├── control_plane/  The JSON-over-UDS surface: framing, sockets, translation.
├── engine/         The composition root — config in, one running engine out.
├── installation/   What this product puts in files the user owns, and takes back.
└── cli/            bridgectl — a control-plane surface.
shell/              The Swift menu-bar shell.
```

The engine runs without the menu-bar app: `python -m gpt_voicecoding.engine --config <file>`.
The Live Call's audio path is an optional extra, `pip install 'gpt-voicecoding[voice]'`, because
everything else runs without a compiled media stack.

Start with [`CONTEXT.md`](CONTEXT.md) for what each term means, then
[`docs/adr/0001`](docs/adr/0001-hub-and-spoke-bridge-core-with-seams.md) for why the shape is
this one. [`docs/control-plane.md`](docs/control-plane.md) has the interface, the command set and
the configuration file; [`docs/app-bundle.md`](docs/app-bundle.md) has the build.

macOS only. The microphone grant, the menu-bar app and the push path are all macOS-shaped, and
cross-platform support is not planned.

## Licence

[MIT](LICENSE).
