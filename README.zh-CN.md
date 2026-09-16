# GPT-VoiceCoding

**用语音操控 Claude Code 和 Codex 的 macOS 菜单栏应用**

[English](./README.md) · **简体中文**

![macOS](https://img.shields.io/badge/macOS-only-111111)
![Apple Silicon](https://img.shields.io/badge/Apple%20Silicon-required-111111)
![Licence](https://img.shields.io/badge/licence-MIT-111111)

https://github.com/user-attachments/assets/a0ea108c-8c5b-45a1-b966-877a05650c65

一通语音电话，照看你 Mac 上所有的 Claude Code 和 Codex 会话。某个会话停下来，它就给你打电话：你会听到是哪个会话停了、停在什么地方，你开口回答，这句话会送回那个会话。不在电脑前时，在 Telegram 上回复也行。通话用的是 OpenAI 的 GPT-Live 语音模型，走 `codex app-server` 的实时通道，所以用的就是你登录 `codex` 的 ChatGPT 账号，不需要 OpenAI API key。

## 工作原理

- 会话停下，它就给你打电话。它只盯着你自己启动的会话，既不替你启动，也不在会话外面再套一层。
- 你用语音回答。你会听到会话停在哪里，说出下一步怎么做，你的话会作为指令送回那个会话，会话可以照着执行。
- 不在电脑前，就在 Telegram 上回复。每次会话停下，Telegram 也会收到通知，回复那条通知，话就会送到同一个会话。

## 截图

![Duty Lamp、设置、控制台和 Telegram](docs/images/showcase.png)

Duty Lamp 是一张悬浮在所有窗口之上的小卡片，显示有几件事在等你处理，它下面是设置（Settings）。中间是通话时的控制台（Control Panel）。右边是 Telegram：你在这里回复会话停下的通知，发送 `/sessions` 会收到一排按钮。

## 安装

在 Apple Silicon Mac 上运行：

```sh
curl -fsSL https://raw.githubusercontent.com/Simon-AI-coding/GPT-VoiceCoding/main/scripts/install.sh | sh
```

脚本会从源码构建应用，用 ad-hoc 方式签名（只在本机签名，不带 Apple 开发者证书），放进 `/Applications` 并打开。项目不提供经过 Apple 公证的下载包。升级时再运行一遍同样的命令，你的配置和 Telegram 凭据都会保留。

你需要：

- Apple Silicon Mac，并装好 Apple 的 Command Line Tools（`xcode-select --install`）。
- Python 3.12 或更新版本，在 PATH 里能以 `python3` 找到。安装脚本会先检查，没找到就停下来告诉你怎么解决，不会替你安装。
- Claude Code 或 Codex，你用哪个就装哪个。两个一起用或只用一个都可以。
- Codex 已用 ChatGPT 账号登录（`codex login`）。语音通话（Live Call）需要它。
- Telegram，只在你想用这第二条回复渠道时才需要。它是可选的，第一次启动时会一步步带你设置。

## 架构

一个很薄的 Swift 菜单栏应用，从自己的应用包里启动一个 Python asyncio 引擎。这个引擎就是 **Bridge Core**：所有策略都由它决定，唯一的数据来源也在它手里。它通过接口（seam）连到其他部分，接口后面的适配器（adapter）可以替换。

两个 agent 的接入方式不同，因为它们对外开放的方式不同。Claude Code 的状态从它自己的会话名单（roster）和两个 hook 读取，写入则通过每个会话本来就会绑定的 inbox socket。Codex 的读和写都通过共享的 `codex app-server`：它由本应用启动，你自己的终端也会连上来，Live Call 的实时通道走的也是这个进程。

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/architecture-dark.svg">
  <img alt="你的终端、codex app-server、以 Bridge Core 为中心的引擎，以及它连接的 Live Call、Telegram、菜单栏应用和 bridgectl" src="docs/images/architecture-light.svg">
</picture>

会话停下后会发生什么，你在通话里回答或在 Telegram 上回复都一样：

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/session-stops-dark.svg">
  <img alt="时序图：会话停下，Bridge Core 发出 Stop Notice 并唤醒 Call Keeper，你的回答作为 Answer Relay 从通话或 Telegram 回复送回" src="docs/images/session-stops-light.svg">
</picture>

Voice（通话里和你说话的那一方）没有工具。它只根据引擎交给它的内容说话，听起来像是要办的事，就转给后台 agent（Call Agent）。通话中只有后台 agent 能执行控制面（control plane）命令。

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

引擎可以脱离菜单栏应用单独运行：`python -m gpt_voicecoding.engine --config <file>`。Live Call 的音频部分是可选依赖，用 `pip install 'gpt-voicecoding[voice]'` 安装，因为其他功能都不需要编译好的媒体库。

想深入了解，先读 [`CONTEXT.md`](CONTEXT.md) 弄清每个术语的意思，再读 [`docs/adr/0001`](docs/adr/0001-hub-and-spoke-bridge-core-with-seams.md) 了解为什么是这种结构。[`docs/control-plane.md`](docs/control-plane.md) 讲接口、命令集和配置文件，[`docs/app-bundle.md`](docs/app-bundle.md) 讲构建。这些文档是英文的。

只支持 macOS。麦克风授权、菜单栏应用和推送通道都是按 macOS 做的，也没有跨平台的计划。

## 许可证

[MIT](LICENSE)。
