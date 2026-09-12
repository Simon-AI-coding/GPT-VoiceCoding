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
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 6) {
                ForEach(OnboardingStep.allCases, id: \.self) { item in
                    Text(item.rawValue <= step.rawValue ? "■" : "□")
                        .foregroundStyle(
                            item.rawValue <= step.rawValue ? Phosphor.accent : Phosphor.muted)
                }
                Spacer()
                Text(shell.text(.onboardingStep, step.rawValue, OnboardingStep.allCases.count))
                    .font(Phosphor.label).foregroundStyle(Phosphor.tertiary)
            }.accessibilityElement(children: .ignore)
                .accessibilityLabel(
                    shell.text(.onboardingStep, step.rawValue, OnboardingStep.allCases.count))
            Text(shell.text(step.title)).font(Phosphor.display).foregroundStyle(Phosphor.bright)
            switch step {
            case .welcome:
                note(.welcomeBody)
                next(.start)
            case .codex:
                CodexCheckView(shell: shell)
                note(.folderAccessNotice)
                HStack {
                    if shell.codexCheck == .ready { next(.continueSetup) }
                    next(.skipSetup)
                }
            case .installation:
                if shell.onboardingBusy {
                    note(.checking)
                } else {
                    placement(.claudeHooks, current: .claudePlaced, absent: .claudeNotPlaced)
                    placement(.codexServer, current: .codexPlaced, absent: .codexNotPlaced)
                    note(.placementReversible)
                }
                next(.continueSetup).disabled(shell.onboardingBusy)
            case .agent:
                note(.setupAgentBody)
                AgentSettingsView(shell: shell)
                SettingsSaveStatus(shell: shell)
                next(.continueSetup).disabled(shell.savingSettings)
            case .telegram:
                note(.setupTelegramBody)
                TelegramSettingsView(shell: shell)
                SettingsSaveStatus(shell: shell)
                HStack {
                    if shell.configuration?.telegramBound == true { next(.continueSetup) }
                    next(.skipSetup)
                }.disabled(shell.savingSettings)
            case .testCall:
                note(.testCallBody)
                PhaseLine(shell: shell)
                Button(shell.text(shell.panel.phase == .onCall ? .hangUp : .placeTestCall)) {
                    Task { await shell.panel.toggleLive() }
                }.disabled(!shell.panel.engineReachable || shell.panel.phase.resolving)
                SettingsSaveStatus(shell: shell)
                HStack {
                    if shell.panel.live != nil { next(.doneSetup) }
                    next(.skipSetup)
                }
            }
        }
    }

    private func placement(_ item: InstallationReport.Item, current: Copy, absent: Copy)
        -> some View
    {
        VStack(alignment: .leading, spacing: 6) {
            let state = shell.installationReport?.state(of: item)
            note(state == .current ? current : absent)
            if state != .current && state != .absent {
                Text(shell.text(.placementFailed)).font(Phosphor.prose).foregroundStyle(
                    Phosphor.danger)
            }
        }
    }

    private func note(_ copy: Copy) -> some View {
        Text(shell.text(copy)).font(Phosphor.prose).foregroundStyle(Phosphor.secondary)
    }

    private func next(_ title: Copy) -> some View {
        Button(shell.text(title)) { Task { await shell.advanceOnboarding() } }
    }
}

struct CodexCheckView: View {
    let shell: ShellModel
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
            Button(shell.text(.recheck)) { Task { await shell.checkCodex() } }
                .disabled(shell.codexCheck == .checking)
        }.font(Phosphor.prose).foregroundStyle(Phosphor.secondary)
            .task { await shell.checkCodex() }
    }
}
