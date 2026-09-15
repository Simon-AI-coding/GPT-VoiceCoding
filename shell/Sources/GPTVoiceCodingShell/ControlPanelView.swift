import ShellCore
import SwiftUI

/// Three pages, one normal window. No view owns a poller.
struct ControlPanelView: View {
    @Bindable var shell: ShellModel

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            if shell.page != .home, !isOnboarding {
                Button {
                    shell.goBack()
                } label: {
                    Label(shell.text(.back), systemImage: "chevron.left")
                }
            }
            switch shell.page {
            case .home, nil: HomeView(shell: shell)
            case .session(let target): SessionBriefView(shell: shell, target: target)
            case .settings(let group): SettingsView(shell: shell, group: group)
            case .onboarding(let step): OnboardingView(shell: shell, step: step)
            case .codexCheck: CodexCheckView(shell: shell)
            case .unreadableSettings:
                Text(shell.text(.somethingOff)).font(Phosphor.headline)
                Text(shell.text(.unreadableSettings)).font(Phosphor.prose)
                Button(shell.text(.openDiagnostics)) { shell.open(.settings(.diagnostics)) }
            }
            if shell.confirmation != nil && !shell.confirmationOnLamp {
                ConfirmationRow(shell: shell)
            }
        }
        .padding(Phosphor.padding).padding(.top, 16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .font(Phosphor.body).foregroundStyle(Phosphor.primary)
        .background(Phosphor.window).tint(Phosphor.accent)
        .buttonStyle(ConsoleButton())
    }

    private var isOnboarding: Bool {
        if case .onboarding = shell.page { return true }
        return false
    }
}

private struct HomeView: View {
    @Bindable var shell: ShellModel
    @State private var rosterContentHeight: CGFloat = 0
    private var panel: ControlPanel { shell.panel }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Toggle(
                shell.text(.duty),
                isOn: Binding(
                    get: { panel.dutyOn },
                    set: { on in
                        Task { await shell.setDuty(on) }
                    })
            ).toggleStyle(ConsoleToggle()).disabled(!panel.engineReachable || panel.busy)
                .padding(.vertical, 4)
            ConsoleRule()
            if panel.engineReachable {
                callBlock
                telegram
                agent
                HStack(alignment: .top, spacing: 12) {
                    count(panel.counts.sessions, .sessions, colour: Phosphor.primary)
                    count(panel.counts.waiting, .waiting, colour: Phosphor.accent)
                    count(panel.counts.finished, .finished, colour: Phosphor.secondary)
                    count(panel.counts.childProcesses, .children, colour: Phosphor.secondary)
                }
                ConsoleRule()
                roster
            } else {
                EngineDownView(shell: shell)
            }
            ConsoleRule()
            HStack {
                Button(shell.text(.settings)) { shell.open(.settings(.voice)) }
                    .buttonStyle(PlainHandButton()).foregroundStyle(Phosphor.accent)
                Spacer()
                Button(shell.text(.quit)) { shell.quit() }
                    .buttonStyle(ConsoleButton(destructive: true))
            }
        }
    }

    private var callBlock: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 12) {
                PhaseLine(shell: shell)
                Spacer(minLength: 8)
                Toggle(shell.text(.voice), isOn: switchBinding("voice"))
                    .toggleStyle(ConsoleToggle(compact: true)).disabled(panel.busy)
                CallButton(shell: shell)
            }
            if !switchBinding("voice").wrappedValue {
                Text(shell.text(.voiceOff)).font(Phosphor.prose).foregroundStyle(Phosphor.secondary)
            }
            if panel.phase == .couldNotConnect {
                Text(shell.text(.callFailed)).font(Phosphor.prose).foregroundStyle(Phosphor.danger)
            }
            if let id = panel.status?.callID {
                Text(shell.text(.callID, id)).font(Phosphor.label).foregroundStyle(Phosphor.muted)
                    .textSelection(.enabled)
            }
            ConsoleRule()
        }
    }

    private var telegram: some View {
        VStack(alignment: .leading, spacing: 6) {
            let bound = shell.configuration?.telegramBound == true
            HStack(spacing: 10) {
                if !bound {
                    Text("› " + shell.text(.telegramOff)).foregroundStyle(Phosphor.muted)
                    Spacer(minLength: 4)
                    Button(shell.text(.connectSettings)) { shell.open(.settings(.telegram)) }
                        .buttonStyle(PlainHandButton()).foregroundStyle(Phosphor.accent)
                }
                Toggle(
                    shell.text(shell.telegramConnected ? .telegramOn : .telegramOff),
                    isOn: Binding(
                        get: { shell.telegramConnected },
                        set: { on in
                            Task { await panel.flip("message", on: on) }
                        })
                ).toggleStyle(ConsoleToggle(hidesLabel: !bound))
                    .disabled(!bound || panel.busy)
            }
            ConsoleRule()
        }
    }

    private var agent: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(shell.text(.agentLabel).uppercased()).font(Phosphor.label)
                .tracking(0.88).foregroundStyle(Phosphor.tertiary)
            let reading = panel.status?.callAgent
            HStack(spacing: 6) {
                Text(reading?.model ?? shell.configuration?.model ?? shell.text(.notChosen))
                    .foregroundStyle(Phosphor.bright).textSelection(.enabled)
                if let effort = reading?.effort ?? shell.configuration?.effort {
                    Text("· " + effort).foregroundStyle(Phosphor.secondary)
                }
            }
            if let reading {
                if let percent = reading.contextPercent { Text(shell.text(.context, percent)) }
                if let total = reading.total { tokenLine(.total, total) }
                if let last = reading.last { tokenLine(.lastTurn, last) }
                Button(shell.text(.newAgent)) { Task { await shell.requestNewAgent() } }
                    .disabled(panel.busy || panel.phase.resolving)
            } else {
                Text(shell.text(.noAgent)).font(Phosphor.prose).foregroundStyle(Phosphor.secondary)
            }
            if panel.nextCallStartsFresh { Text(shell.text(.freshAgent)).font(Phosphor.prose) }
        }.consoleCard()
    }

    private func tokenLine(_ label: Copy, _ tokens: [String: Int]) -> some View {
        Text(
            shell.text(label) + " · "
                + shell.text(
                    .tokens,
                    tokens["input"] ?? 0, tokens["output"] ?? 0, tokens["reasoning"] ?? 0,
                    tokens["cached"] ?? 0)
        )
        .font(Phosphor.label).foregroundStyle(Phosphor.secondary)
    }

    private func count(_ number: Int, _ label: Copy, colour: Color) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(number, format: .number).font(Phosphor.title).foregroundStyle(colour)
            Text(shell.text(label)).font(Phosphor.label).foregroundStyle(Phosphor.tertiary)
                .fixedSize(horizontal: false, vertical: true)
        }.frame(maxWidth: .infinity, alignment: .leading)
    }

    private var roster: some View {
        VStack(alignment: .leading, spacing: 8) {
            if panel.displayedRoster.isEmpty {
                VStack(spacing: 6) {
                    Text("○").font(Phosphor.title).foregroundStyle(Phosphor.muted)
                    Text(shell.text(.emptyTitle)).font(Phosphor.headline)
                    Text(shell.text(.emptyBody)).font(Phosphor.prose).foregroundStyle(
                        Phosphor.tertiary)
                }.multilineTextAlignment(.center)
                    .frame(maxWidth: .infinity).padding(.vertical, 26)
            } else {
                Text(shell.text(.rosterTitle).uppercased()).font(Phosphor.label)
                    .tracking(0.88).foregroundStyle(Phosphor.tertiary)
                ScrollView {
                    VStack(spacing: 0) {
                        ForEach(panel.displayedRoster) { row in
                            Button {
                                shell.open(.session(row.target))
                            } label: {
                                SessionRowView(row: row, text: shell.text, now: shell.messageNow)
                                    .padding(.vertical, 6)
                            }.buttonStyle(PlainHandButton())
                            ConsoleRule()
                        }
                    }
                    .onGeometryChange(for: CGFloat.self) {
                        $0.size.height
                    } action: {
                        rosterContentHeight = $0
                    }
                }.frame(height: min(rosterContentHeight, Phosphor.rosterHeight))
                    .onHover { panel.setPointerInRoster($0) }
            }
        }
    }

    private func switchBinding(_ name: String) -> Binding<Bool> {
        Binding(
            get: { panel.status?.switches.first { $0.name == name }?.on ?? false },
            set: { on in Task { await panel.flip(name, on: on) } })
    }
}

struct SessionRowView: View {
    let row: BriefRow
    let text: ShellText
    var now = Date()
    var body: some View {
        HStack(spacing: 8) {
            AgentMark(agent: row.target.agent)
            Text(text.name(row.name)).foregroundStyle(Phosphor.primary).layoutPriority(1)
            Text(row.stateWord).foregroundStyle(Phosphor.state(row.state)).layoutPriority(1)
            Text("· " + row.newest).foregroundStyle(Phosphor.tertiary)
                .frame(maxWidth: .infinity, alignment: .leading)
            Text(text.messageAge(row.messageAt, at: now))
                .font(Phosphor.small).monospacedDigit().foregroundStyle(Phosphor.muted)
                .frame(width: Phosphor.messageTimeWidth, alignment: .trailing).layoutPriority(2)
        }.lineLimit(1).contentShape(Rectangle())
    }
}

private struct SessionBriefView: View {
    let shell: ShellModel
    let target: SessionAddress
    var body: some View {
        if let brief = shell.panel.sessionBrief {
            VStack(alignment: .leading, spacing: 12) {
                Text(shell.text(.session)).font(Phosphor.label).foregroundStyle(Phosphor.tertiary)
                HStack {
                    AgentMark(agent: target.agent, size: Phosphor.briefMark)
                    Text(shell.text.name(brief.name)).font(Phosphor.headline)
                }
                Text(brief.stateWord).foregroundStyle(Phosphor.accent)
                ScrollView {
                    VStack(alignment: .leading, spacing: 12) {
                        HStack {
                            Text(shell.text(.newest))
                            Spacer()
                            Text(
                                shell.text.messageAge(
                                    brief.messageAt, at: shell.messageNow, long: true)
                            )
                            .monospacedDigit()
                        }.font(Phosphor.label).foregroundStyle(Phosphor.tertiary)
                        Text(brief.newest).font(Phosphor.prose)
                        if let prompt = brief.prompt {
                            Divider()
                            Text(shell.text(.pending)).font(Phosphor.label).foregroundStyle(
                                Phosphor.tertiary)
                            Text(prompt).font(Phosphor.prose)
                            ForEach(Array(brief.options.enumerated()), id: \.offset) { _, option in
                                Text(option).font(Phosphor.prose)
                            }
                            Text(shell.text(.answerElsewhere)).font(Phosphor.prose)
                                .foregroundStyle(Phosphor.secondary)
                        }
                    }.frame(maxWidth: .infinity, alignment: .leading).textSelection(.enabled)
                }.frame(idealHeight: Phosphor.rosterHeight, maxHeight: Phosphor.rosterHeight)
            }
        } else {
            Text(shell.text(shell.panel.sessionFailure == nil ? .loading : .sessionUnavailable))
                .font(Phosphor.prose)
        }
    }
}

struct EngineDownView: View {
    let shell: ShellModel
    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(shell.text(.notWatching)).font(Phosphor.headline)
            Text(shell.text(.engineDown)).font(Phosphor.prose).foregroundStyle(Phosphor.secondary)
            Button(shell.text(.openDiagnostics)) { shell.open(.settings(.diagnostics)) }
        }
    }
}

struct PhaseLine: View {
    let shell: ShellModel
    private var colour: Color {
        switch shell.panel.phase {
        case .ready, .ending: return Phosphor.tertiary
        case .calling, .onCall: return Phosphor.accent
        case .couldNotConnect: return Phosphor.danger
        }
    }
    var body: some View {
        HStack(spacing: 6) {
            Circle().fill(colour)
                .frame(width: 6, height: 6)
                .phaseAnimator([false, true]) { dot, pulse in
                    dot.opacity(shell.panel.phase.resolving && pulse ? 0.3 : 1)
                } animation: { _ in
                    Phosphor.pulse
                }
            Text(shell.text.phase(shell.panel.phase)).foregroundStyle(colour)
                .id(shell.panel.phase).transition(.opacity)
            if let elapsed = shell.panel.elapsed {
                Text(String(format: "%02d:%02d", elapsed / 60, elapsed % 60)).monospacedDigit()
            }
        }.animation(Phosphor.motion, value: shell.panel.phase)
    }
}

struct CallButton: View {
    let shell: ShellModel
    var body: some View {
        Button {
            Task { await shell.panel.toggleLive() }
        } label: {
            // Both labels participate in layout; neither language changes width by phase.
            ZStack {
                Text(shell.text(.call)).hidden()
                Text(shell.text(.hangUp)).hidden()
                Text(
                    shell.text(
                        shell.panel.phase == .onCall || shell.panel.phase == .ending
                            ? .hangUp : .call))
            }
        }.buttonStyle(
            ConsoleButton(
                prominent: shell.panel.phase != .onCall,
                destructive: shell.panel.phase == .onCall)
        )
        .disabled(shell.panel.phase.resolving || shell.panel.busy)
    }
}

struct ConfirmationRow: View {
    @Bindable var shell: ShellModel
    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(shell.text(shell.confirmation == .quit ? .quitAsk : .newAgentAsk)).font(
                Phosphor.prose)
            HStack {
                Button(shell.text(shell.confirmation == .quit ? .keepCall : .keepAgent)) {
                    Task { await shell.resolveConfirmation(accept: false) }
                }.keyboardShortcut(.cancelAction)
                Button(shell.text(shell.confirmation == .quit ? .quit : .confirmAgent)) {
                    Task { await shell.resolveConfirmation(accept: true) }
                }
            }
        }.consoleCard()
    }
}

struct SettingsView: View {
    let shell: ShellModel
    let group: SettingsGroup
    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            VStack(alignment: .leading, spacing: 8) {
                ForEach(SettingsGroup.allCases, id: \.self) { item in
                    Button {
                        shell.open(.settings(item))
                    } label: {
                        Text(shell.text(item.title)).frame(maxWidth: .infinity, alignment: .leading)
                            .foregroundStyle(item == group ? Phosphor.accent : Phosphor.secondary)
                    }.buttonStyle(PlainHandButton()).padding(.vertical, 4)
                }
            }.frame(width: Phosphor.navigationWidth)
            Divider()
            VStack(alignment: .leading, spacing: 12) {
                Text(shell.text(group.title)).font(Phosphor.headline)
                switch group {
                case .diagnostics: DiagnosticsView(shell: shell)
                case .voice: VoiceSettingsView(shell: shell)
                case .call: CallSettingsView(shell: shell)
                case .general: GeneralSettingsView(shell: shell)
                case .telegram: TelegramSettingsView(shell: shell)
                case .agent: AgentSettingsView(shell: shell)
                }
                SettingsSaveStatus(shell: shell)
            }.frame(maxWidth: .infinity, alignment: .leading)
        }
    }
}
