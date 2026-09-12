import ShellCore
import SwiftUI

struct TelegramSettingsView: View {
    @Bindable var shell: ShellModel

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            if shell.telegramStage == .idle, shell.configuration?.telegramBound == true {
                Text(shell.configuration?.telegramName ?? "").foregroundStyle(Phosphor.bright)
                Text("••••••••").foregroundStyle(Phosphor.secondary)
                note(.telegramMaskedNote)
                HStack {
                    Button(shell.text(.telegramChange)) { shell.changeTelegram() }
                        .disabled(!shell.panel.engineReachable)
                    Button(shell.text(.telegramUnbind)) { Task { await shell.unbindTelegram() } }
                        .disabled(shell.savingSettings)
                }
            } else {
                switch shell.telegramStage {
                case .idle, .entering:
                    SecureField(shell.text(.telegramToken), text: $shell.telegramToken)
                        .textFieldStyle(.roundedBorder).disabled(!shell.panel.engineReachable)
                    note(.telegramTokenHelp)
                    Button(shell.text(.telegramValidate)) {
                        Task { await shell.validateTelegram() }
                    }
                    .disabled(!shell.canValidateTelegram)
                case .validating:
                    Text(shell.text(.checking)).foregroundStyle(Phosphor.secondary)
                case .waiting, .confirmed:
                    if let binding = shell.telegramBinding {
                        Text(binding.botName).foregroundStyle(Phosphor.bright)
                        if shell.telegramStage == .waiting {
                            note(.telegramNamed)
                            if let url = binding.startURL {
                                Link(shell.text(.telegramOpen), destination: url)
                            }
                            note(.telegramWaiting)
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
}
