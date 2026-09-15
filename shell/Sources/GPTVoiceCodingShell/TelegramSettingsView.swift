import ShellCore
import SwiftUI

struct TelegramSettingsView: View {
    @Bindable var shell: ShellModel
    var onboarding = false

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            if shell.telegramStage == .idle, shell.configuration?.telegramBound == true {
                valueRow(shell.text(.telegramBot), shell.configuration?.telegramName ?? "")
                HStack(spacing: 8) {
                    Text(shell.text(.telegramTokenLabel)).foregroundStyle(Phosphor.tertiary)
                    Spacer(minLength: 8)
                    Text("••••••••••••").foregroundStyle(Phosphor.secondary)
                        .padding(.horizontal, 8).padding(.vertical, 4)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(
                            Phosphor.raised,
                            in: RoundedRectangle(cornerRadius: Phosphor.controlRadius)
                        )
                        .overlay(
                            RoundedRectangle(cornerRadius: Phosphor.controlRadius)
                                .stroke(Phosphor.ruleStrong))
                    Button(shell.text(.telegramChange)) { shell.changeTelegram() }
                        .disabled(!shell.panel.engineReachable)
                }
                Button(shell.text(.telegramUnbind)) { Task { await shell.unbindTelegram() } }
                    .buttonStyle(ConsoleButton(destructive: true)).disabled(shell.savingSettings)
                note(.telegramMaskedNote)
            } else {
                switch shell.telegramStage {
                case .idle, .entering:
                    if onboarding {
                        HStack(spacing: 8) {
                            Text(shell.text(.telegramTokenLabel)).foregroundStyle(Phosphor.tertiary)
                            Spacer(minLength: 8)
                            SecureField(shell.text(.telegramToken), text: $shell.telegramToken)
                                .textFieldStyle(.plain).multilineTextAlignment(.trailing)
                                .foregroundStyle(Phosphor.bright)
                                .disabled(!shell.panel.engineReachable)
                        }.padding(.bottom, 6).overlay(alignment: .bottom) { ConsoleRule() }
                    } else {
                        HStack(spacing: 8) {
                            SecureField(shell.text(.telegramToken), text: $shell.telegramToken)
                                .textFieldStyle(.plain).padding(.horizontal, 8).padding(
                                    .vertical, 4
                                )
                                .background(
                                    Phosphor.raised,
                                    in: RoundedRectangle(cornerRadius: Phosphor.controlRadius)
                                )
                                .overlay(
                                    RoundedRectangle(cornerRadius: Phosphor.controlRadius)
                                        .stroke(Phosphor.ruleStrong)
                                )
                                .disabled(!shell.panel.engineReachable)
                            Button(shell.text(.telegramValidate)) {
                                Task { await shell.validateTelegram() }
                            }.buttonStyle(ConsoleButton(prominent: true))
                                .disabled(!shell.canValidateTelegram)
                        }
                        note(.telegramTokenHelp)
                    }
                case .validating:
                    HStack(spacing: 8) {
                        Text("●").foregroundStyle(Phosphor.accent)
                        Text(shell.text(.checking)).foregroundStyle(Phosphor.secondary)
                    }
                case .waiting, .confirmed:
                    if let binding = shell.telegramBinding {
                        valueRow(shell.text(.telegramBot), binding.botName)
                        if shell.telegramStage == .waiting {
                            note(.telegramNamed)
                            Text("@\(binding.username)").textSelection(.enabled)
                            note(.telegramWaiting)
                            Button(shell.text(shell.checkingTelegram ? .checking : .telegramCheck))
                            {
                                Task { await shell.refreshTelegramBinding() }
                            }
                            .disabled(!shell.canCheckTelegram)
                            if shell.telegramCheckFinished { note(.telegramNotFound) }
                        } else {
                            note(.telegramConfirmed)
                            Button(shell.text(.save)) { Task { await shell.saveTelegramBinding() } }
                                .disabled(shell.savingSettings)
                        }
                    }
                }
                if shell.telegramStage != .idle {
                    Button(shell.text(.cancel)) { shell.cancelTelegramBinding() }
                        .disabled(shell.savingSettings)
                }
            }
            if !shell.panel.engineReachable { note(.telegramEngineDown) }
            if let failure = shell.telegramFailureCopy {
                note(failure)
                Button(shell.text(.openDiagnostics)) { shell.open(.settings(.diagnostics)) }
            }
        }
    }

    private func note(_ copy: Copy) -> some View {
        Text(shell.text(copy)).font(Phosphor.prose).foregroundStyle(Phosphor.secondary)
    }

    private func valueRow(_ title: String, _ value: String) -> some View {
        HStack(spacing: 8) {
            Text(title).foregroundStyle(Phosphor.tertiary)
            Spacer(minLength: 8)
            Text(value).foregroundStyle(Phosphor.bright).textSelection(.enabled)
        }
    }
}
