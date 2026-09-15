import ShellCore
import SwiftUI

struct AgentSettingsView: View {
    let shell: ShellModel
    var showsNote = true
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            SettingsPicker(
                title: shell.text(.model),
                value: shell.configuration?.model ?? shell.text(.notChosen),
                choices: shell.panel.models.map(\.model),
                enabled: shell.panel.engineReachable && !shell.savingSettings
            ) { name in
                Task { await shell.selectAgentModel(name) }
            }
            SettingsPicker(
                title: shell.text(.effort),
                value: shell.configuration?.effort ?? shell.text(.notChosen),
                choices: shell.agentEfforts,
                enabled: shell.panel.engineReachable && !shell.savingSettings
            ) { effort in
                Task { await shell.saveSetting(.effort, value: .string(effort)) }
            }
            if showsNote {
                Text(shell.text(.agentModelsNote)).font(Phosphor.prose).foregroundStyle(
                    Phosphor.secondary)
            }
            if showsNote && shell.agentModelUnavailable {
                Text(shell.text(.agentModelGone)).font(Phosphor.prose).foregroundStyle(
                    Phosphor.secondary)
            }
            if showsNote && (!shell.panel.engineReachable || shell.panel.models.isEmpty) {
                Text(shell.text(.agentModelsUnavailable)).font(Phosphor.prose).foregroundStyle(
                    Phosphor.secondary)
            }
        }.task(id: shell.panel.engineReachable ? shell.health : .notStarted) {
            await shell.panel.refreshModels()
        }
    }
}

struct CallSettingsView: View {
    let shell: ShellModel
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            if let autoHangup = shell.autoHangup {
                Toggle(
                    shell.text(.autoHangup),
                    isOn: Binding(
                        get: { autoHangup },
                        set: { on in Task { await shell.panel.flip("auto_hangup", on: on) } }
                    )
                ).toggleStyle(ConsoleToggle()).disabled(
                    !shell.panel.engineReachable || shell.panel.busy)
            } else {
                Text(shell.text(.autoHangup) + " · " + shell.text(.unavailable))
                    .foregroundStyle(Phosphor.secondary)
            }
            seconds(.silence, title: .silenceSeconds, value: shell.configuration?.silenceSeconds)
                .disabled(shell.autoHangup == false)
            seconds(.coolDown, title: .coolDownSeconds, value: shell.configuration?.coolDownSeconds)
            seconds(
                .speechSettle, title: .speechSettleSeconds,
                value: shell.configuration?.speechSettleSeconds)
        }
    }

    private func seconds(_ setting: EngineSetting, title: Copy, value: Double?) -> some View {
        SettingsNumber(title: shell.text(title), value: value, text: shell.text) { seconds in
            Task { await shell.saveSetting(setting, value: .number(seconds)) }
        }.disabled(shell.savingSettings)
    }
}

struct GeneralSettingsView: View {
    let shell: ShellModel
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Toggle(
                shell.text(.launchAtLogin),
                isOn: Binding(
                    get: { shell.loginItem.enabled }, set: { shell.loginItem.set($0) }
                )
            ).toggleStyle(ConsoleToggle())
            if shell.loginItem.failure != nil {
                Text(shell.text(.settingsSaveFailed)).font(Phosphor.prose)
                Button(shell.text(.openDiagnostics)) { shell.open(.settings(.diagnostics)) }
            }
            HStack {
                Text(shell.text(.appearance)).foregroundStyle(Phosphor.tertiary)
                Spacer()
                HStack(spacing: 4) {
                    ForEach(ShellAppearance.allCases, id: \.self) { appearance in
                        Button {
                            shell.setAppearance(appearance)
                        } label: {
                            Text(shell.text(appearance.title))
                                .padding(.horizontal, 10).padding(.vertical, 2)
                                .contentShape(Rectangle())
                        }.buttonStyle(PlainHandButton())
                            .foregroundStyle(
                                shell.selectedAppearance == appearance
                                    ? Phosphor.accent : Phosphor.secondary
                            )
                            .background(
                                shell.selectedAppearance == appearance
                                    ? Phosphor.accentWash : .clear,
                                in: RoundedRectangle(cornerRadius: Phosphor.controlRadius)
                            )
                            .overlay(
                                RoundedRectangle(cornerRadius: Phosphor.controlRadius)
                                    .stroke(
                                        shell.selectedAppearance == appearance
                                            ? Phosphor.ruleAccent : Phosphor.ruleStrong)
                            )
                            .accessibilityAddTraits(
                                shell.selectedAppearance == appearance ? .isSelected : [])
                    }
                }
            }.font(Phosphor.body)
            Text(shell.text(shell.selectedAppearance.hint))
                .font(Phosphor.prose).foregroundStyle(Phosphor.secondary)
            HStack(spacing: 8) {
                Text(shell.text(.language)).foregroundStyle(Phosphor.tertiary)
                Spacer(minLength: 8)
                ForEach(ShellLanguage.allCases, id: \.self) { language in
                    Button(language.label) { shell.setLanguage(language) }
                        .buttonStyle(PlainHandButton())
                        .foregroundStyle(
                            shell.selectedLanguage == language
                                ? Phosphor.accent : Phosphor.secondary
                        )
                        .padding(.horizontal, 10).padding(.vertical, 2)
                        .background(
                            shell.selectedLanguage == language ? Phosphor.accentWash : .clear,
                            in: RoundedRectangle(cornerRadius: Phosphor.controlRadius)
                        )
                        .overlay(
                            RoundedRectangle(cornerRadius: Phosphor.controlRadius)
                                .stroke(
                                    shell.selectedLanguage == language
                                        ? Phosphor.ruleAccent : Phosphor.ruleStrong))
                }
            }
            Text(shell.text(.languageHint)).font(Phosphor.prose)
                .foregroundStyle(Phosphor.secondary)
        }
    }
}

private struct SettingsNumber: View {
    let title: String
    let value: Double?
    let text: ShellText
    let save: (Double) -> Void
    @State private var draft = ""
    @State private var invalid = false
    @FocusState private var focused: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 8) {
                Text(title).foregroundStyle(Phosphor.tertiary)
                Spacer(minLength: 8)
                TextField("", text: $draft).focused($focused)
                    .textFieldStyle(.plain).foregroundStyle(Phosphor.bright)
                    .onSubmit(commit)
                    .multilineTextAlignment(.trailing).frame(minWidth: 28, idealWidth: 36)
                    .fixedSize(horizontal: true, vertical: false)
                    .padding(.horizontal, 6).padding(.vertical, 3)
                    .background(
                        Phosphor.raised, in: RoundedRectangle(cornerRadius: Phosphor.controlRadius)
                    )
                    .overlay(
                        RoundedRectangle(cornerRadius: Phosphor.controlRadius)
                            .stroke(Phosphor.ruleStrong))
                Text(text(.secondsUnit)).foregroundStyle(Phosphor.tertiary)
            }
            if invalid { Text(text(.numberRequired)).font(Phosphor.prose) }
        }
        .onAppear { reset() }
        .onChange(of: value) { if !focused { reset() } }
        .onChange(of: focused) { if !focused { commit() } }
    }

    private func reset() { draft = value.map { $0.formatted(.number.grouping(.never)) } ?? "" }
    private func commit() {
        let number = try? Double(draft, format: .number)
        invalid = number == nil || number?.isFinite == false
        if let number, !invalid, number != value { save(number) }
    }
}

struct VoiceSettingsView: View {
    let shell: ShellModel
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            SettingsPicker(
                title: shell.text(.voice),
                value: shell.configuration?.voice ?? shell.text(.notChosen),
                choices: shell.configuration?.voices ?? [], enabled: !shell.savingSettings
            ) { value in Task { await shell.saveSetting(.voice, value: .string(value)) } }
            SettingsPicker(
                title: shell.text(.realtimeModel),
                value: shell.configuration?.realtimeModel ?? shell.text(.notChosen),
                choices: shell.configuration?.realtimeModels ?? [], enabled: !shell.savingSettings
            ) { value in Task { await shell.saveSetting(.realtimeModel, value: .string(value)) } }
            Text(shell.text(.voiceNote)).font(Phosphor.prose).foregroundStyle(Phosphor.secondary)
        }
    }
}

struct SettingsPicker: View {
    let title: String
    let value: String
    let choices: [String]
    var enabled = true
    let select: (String) -> Void
    @State private var expanded = false

    var body: some View {
        HStack(spacing: 8) {
            Text(title).foregroundStyle(Phosphor.tertiary)
            Spacer(minLength: 8)
            Button {
                expanded.toggle()
            } label: {
                HStack(spacing: 8) {
                    Text(value).foregroundStyle(enabled ? Phosphor.bright : Phosphor.secondary)
                    Text("⌄").font(Phosphor.label).foregroundStyle(Phosphor.tertiary)
                }.padding(.horizontal, 8).padding(.vertical, 3)
                    .background(
                        Phosphor.raised, in: RoundedRectangle(cornerRadius: Phosphor.controlRadius)
                    )
                    .overlay(
                        RoundedRectangle(cornerRadius: Phosphor.controlRadius)
                            .stroke(enabled ? Phosphor.ruleStrong : Phosphor.rule)
                    )
            }.buttonStyle(PlainHandButton()).disabled(!enabled || choices.isEmpty)
        }
        .overlay(alignment: .topTrailing) {
            if expanded {
                choicesList.offset(y: 28)
            }
        }
        .zIndex(expanded ? 1 : 0)
    }

    private var choicesList: some View {
        VStack(alignment: .leading, spacing: 2) {
            ForEach(choices, id: \.self) { choice in
                Button {
                    expanded = false
                    if choice != value { select(choice) }
                } label: {
                    HStack(spacing: 6) {
                        Text(choice == value ? "›" : " ").foregroundStyle(Phosphor.accent)
                        Text(choice).foregroundStyle(
                            choice == value ? Phosphor.bright : Phosphor.secondary)
                        Spacer(minLength: 12)
                    }.padding(.horizontal, 8).padding(.vertical, 4)
                        .background(
                            choice == value ? Phosphor.accentWash : .clear,
                            in: RoundedRectangle(cornerRadius: Phosphor.controlRadius))
                }.buttonStyle(PlainHandButton())
            }
        }.padding(4)
            .background(
                Phosphor.raised, in: RoundedRectangle(cornerRadius: Phosphor.controlRadius)
            )
            .overlay(
                RoundedRectangle(cornerRadius: Phosphor.controlRadius)
                    .stroke(Phosphor.ruleStrong))
    }
}

struct SettingsSaveStatus: View {
    let shell: ShellModel
    var body: some View {
        if shell.settingsFailure != nil {
            Text(shell.text(.settingsSaveFailed)).font(Phosphor.prose).foregroundStyle(
                Phosphor.secondary)
            Button(shell.text(.openDiagnostics)) { shell.open(.settings(.diagnostics)) }
        }
        if shell.pendingRestart {
            ConsoleRule()
            HStack(spacing: 8) {
                HStack(alignment: .top, spacing: 6) {
                    Text("!").foregroundStyle(Phosphor.accent)
                    Text(shell.text(.restartRequired)).font(Phosphor.prose)
                }.frame(maxWidth: .infinity, alignment: .leading)
                Button {
                    Task { await shell.restartForSettings() }
                } label: {
                    Text(shell.text(shell.restartBlockedByCall ? .restartAfterCall : .restartNow))
                        .font(Phosphor.small.weight(.medium)).fixedSize()
                        .padding(.horizontal, 10).padding(.vertical, 3)
                        .foregroundStyle(
                            shell.restartBlockedByCall ? Phosphor.muted : Phosphor.accentInk
                        )
                        .background(
                            shell.restartBlockedByCall ? .clear : Phosphor.accent,
                            in: RoundedRectangle(cornerRadius: Phosphor.controlRadius)
                        )
                        .overlay(
                            RoundedRectangle(cornerRadius: Phosphor.controlRadius)
                                .strokeBorder(
                                    shell.restartBlockedByCall ? Phosphor.rule : .clear,
                                    style: StrokeStyle(lineWidth: 1, dash: [3, 3])))
                }.buttonStyle(PlainHandButton()).disabled(
                    shell.restartBlockedByCall || shell.savingSettings)
            }
        }
    }
}
