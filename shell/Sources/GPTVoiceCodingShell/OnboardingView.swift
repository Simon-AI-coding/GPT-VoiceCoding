import ShellCore
import SwiftUI

extension OnboardingStep {
    var title: Copy {
        switch self {
        case .welcome: return .welcomeTitle
        case .codex: return .codexCheckTitle
        case .installation: return .placedTitle
        case .agent: return .callAgent
        case .telegram: return .telegram
        case .testCall: return .testCallTitle
        }
    }
}

struct OnboardingView: View {
    let shell: ShellModel
    let step: OnboardingStep

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(spacing: 6) {
                ForEach(OnboardingStep.allCases, id: \.self) { item in
                    Text(item.rawValue <= step.rawValue ? "■" : "□")
                        .foregroundStyle(
                            item.rawValue <= step.rawValue ? Phosphor.accent : Phosphor.muted)
                }
                Text(shell.text(.onboardingStep, step.rawValue, OnboardingStep.allCases.count))
                    .font(Phosphor.small).foregroundStyle(Phosphor.tertiary).padding(.leading, 4)
            }.accessibilityElement(children: .ignore)
                .accessibilityLabel(
                    shell.text(.onboardingStep, step.rawValue, OnboardingStep.allCases.count))
            Text(shell.text(step.title)).font(Phosphor.display).foregroundStyle(Phosphor.bright)
            switch step {
            case .welcome:
                note(.welcomeBody)
            case .codex:
                CodexCheckView(shell: shell, showsAction: false)
            case .installation:
                if shell.onboardingBusy {
                    note(.checking)
                } else {
                    let claude = shell.installationReport?.state(of: .claudeHooks)
                    let codex = shell.installationReport?.state(of: .codexServer)
                    if Self.placementPresentation(
                        state: claude, current: .claudePlaced, absent: .claudeNotPlaced
                    ).copy != .claudePlaced
                        || Self.placementPresentation(
                            state: codex, current: .codexPlaced, absent: .codexNotPlaced
                        ).copy != .codexPlaced
                    {
                        placement(.claudeHooks, current: .claudePlaced, absent: .claudeNotPlaced)
                        placement(.codexServer, current: .codexPlaced, absent: .codexNotPlaced)
                        note(.placementReversible)
                    } else {
                        note(.placedBody)
                    }
                    note(.folderAccessNotice)
                }
            case .agent:
                note(.setupAgentBody)
                AgentSettingsView(shell: shell, showsNote: false)
                SettingsSaveStatus(shell: shell)
            case .telegram:
                note(.setupTelegramBody)
                TelegramSettingsView(shell: shell, onboarding: true)
                SettingsSaveStatus(shell: shell)
            case .testCall:
                note(.testCallBody)
                PhaseLine(shell: shell).padding(.horizontal, 12).padding(.vertical, 10)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .overlay(
                        RoundedRectangle(cornerRadius: Phosphor.cardRadius)
                            .stroke(Phosphor.rule, style: StrokeStyle(lineWidth: 1, dash: [3, 3])))
                SettingsSaveStatus(shell: shell)
            }
            Spacer(minLength: 8)
            footer
        }
        .frame(minHeight: 248, alignment: .topLeading)
    }

    @ViewBuilder private var footer: some View {
        HStack(spacing: 8) {
            if step == .codex || step == .telegram || step == .testCall {
                Button(shell.text(.skipSetup)) { Task { await shell.advanceOnboarding() } }
                    .buttonStyle(PlainHandButton()).foregroundStyle(Phosphor.accent)
            }
            Spacer(minLength: 8)
            switch step {
            case .welcome:
                advanceButton(.start)
            case .codex:
                if shell.codexCheck == .ready {
                    advanceButton(.continueSetup)
                } else {
                    Button(shell.text(.recheck)) { Task { await shell.checkCodex() } }
                        .buttonStyle(ConsoleButton(prominent: true, large: true))
                        .disabled(shell.codexCheck == .checking)
                }
            case .installation:
                advanceButton(.continueSetup).disabled(shell.onboardingBusy)
            case .agent:
                advanceButton(.continueSetup).disabled(shell.savingSettings)
            case .telegram:
                if shell.configuration?.telegramBound == true {
                    advanceButton(.continueSetup)
                } else if shell.telegramStage == .idle || shell.telegramStage == .entering {
                    Button(shell.text(.telegramValidate)) {
                        Task { await shell.validateTelegram() }
                    }.buttonStyle(ConsoleButton(prominent: true, large: true))
                        .disabled(!shell.canValidateTelegram)
                }
            case .testCall:
                Button(shell.text(.doneSetup)) { Task { await shell.advanceOnboarding() } }
                    .buttonStyle(ConsoleButton())
                Button(
                    shell.text(shell.panel.phase == .onCall ? .hangUp : .placeTestCall)
                ) {
                    Task { await shell.panel.toggleLive() }
                }.buttonStyle(ConsoleButton(prominent: true, large: true))
                    .disabled(!shell.panel.engineReachable || shell.panel.phase.resolving)
            }
        }
    }

    private func placement(_ item: InstallationReport.Item, current: Copy, absent: Copy)
        -> some View
    {
        VStack(alignment: .leading, spacing: 6) {
            let presentation = Self.placementPresentation(
                state: shell.installationReport?.state(of: item), current: current, absent: absent)
            note(presentation.copy)
            if presentation.failed {
                Text(shell.text(.placementFailed)).font(Phosphor.prose).foregroundStyle(
                    Phosphor.danger)
            }
        }
    }

    static func placementPresentation(
        state: InstallationReport.ItemState?, current: Copy, absent: Copy
    ) -> (copy: Copy, failed: Bool) {
        switch state {
        case .current?, .stale?: return (current, false)
        case .absent?: return (absent, false)
        case .failed?, nil: return (absent, true)
        }
    }

    private func note(_ copy: Copy) -> some View {
        Text(shell.text(copy)).font(Phosphor.prose).foregroundStyle(Phosphor.secondary)
    }

    private func advanceButton(_ title: Copy) -> some View {
        Button(shell.text(title)) { Task { await shell.advanceOnboarding() } }
            .buttonStyle(ConsoleButton(prominent: true, large: true))
    }
}

struct CodexCheckView: View {
    let shell: ShellModel
    var showsAction = true
    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            switch shell.codexCheck {
            case .checking: Text(shell.text(.checking))
            case .ready: Text(shell.text(.codexReady))
            case .notInstalled:
                Text(shell.text(.codexNotInstalled))
                Text(shell.text(.codexInstallFix)).textSelection(.enabled)
            case .notLoggedIn:
                Text(shell.text(.codexNotLoggedIn))
                Text(shell.text(.codexLoginFix)).textSelection(.enabled)
            }
            codexRows
            if showsAction {
                Button(shell.text(.recheck)) { Task { await shell.checkCodex() } }
                    .disabled(shell.codexCheck == .checking)
            }
        }.font(Phosphor.prose).foregroundStyle(Phosphor.secondary)
            .task { await shell.checkCodex() }
    }

    private var codexRows: some View {
        VStack(spacing: 0) {
            row(.installed, value: installedValue)
            row(.loggedIn, value: loggedInValue)
        }
    }

    private var installedValue: Copy? {
        switch shell.codexCheck {
        case .checking: return nil
        case .notInstalled: return .no
        case .ready, .notLoggedIn: return .yes
        }
    }

    private var loggedInValue: Copy? {
        switch shell.codexCheck {
        case .checking, .notInstalled: return nil
        case .notLoggedIn: return .no
        case .ready: return .yes
        }
    }

    private func row(_ title: Copy, value: Copy?) -> some View {
        HStack {
            Text(shell.text(title)).foregroundStyle(Phosphor.tertiary)
            Spacer()
            Text(value.map { shell.text($0) } ?? "—")
                .foregroundStyle(value == .no ? Phosphor.danger : Phosphor.accent)
        }.font(Phosphor.body).padding(.vertical, 6)
            .overlay(alignment: .bottom) { ConsoleRule() }
    }
}
