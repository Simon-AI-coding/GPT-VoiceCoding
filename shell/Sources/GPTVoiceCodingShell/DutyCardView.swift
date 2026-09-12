import ShellCore
import SwiftUI

struct DutyCardView: View {
    @Bindable var shell: ShellModel
    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            if shell.panel.engineReachable {
                VStack(alignment: .leading, spacing: 6) {
                    HStack(spacing: 6) {
                        PhaseLine(shell: shell)
                        if shell.panel.counts.waiting > 0 {
                            Text("· " + shell.text(.waitingCount, shell.panel.counts.waiting))
                        }
                        if shell.panel.counts.finished > 0 {
                            Text("· " + shell.text(.finishedCount, shell.panel.counts.finished))
                        }
                    }.font(Phosphor.small).lineLimit(1)
                    if shell.panel.counts.waiting + shell.panel.counts.finished > 0,
                        let row = shell.panel.firstCountedRow
                    {
                        SessionRowView(row: row, text: shell.text)
                    }
                }.frame(maxWidth: .infinity, alignment: .leading).contentShape(Rectangle())
                    .onTapGesture { shell.cardActionsVisible.toggle() }
                if shell.panel.phase == .couldNotConnect {
                    Text(shell.text(.callFailed)).font(Phosphor.prose).foregroundStyle(
                        Phosphor.danger)
                }
                if shell.cardActionsVisible {
                    HStack(spacing: 8) {
                        CallButton(shell: shell)
                        Button(shell.text(.home)) { shell.open(.home) }
                        Button(shell.text(.settings)) { shell.open(.settings(.voice)) }
                    }
                }
            } else {
                EngineDownView(shell: shell)
            }
            if !shell.windowOpen && shell.confirmation != nil { ConfirmationRow(shell: shell) }
        }
        .padding(.horizontal, 10).padding(.vertical, 8)
        .frame(width: Phosphor.cardWidth, alignment: .leading)
        .font(Phosphor.body).foregroundStyle(Phosphor.primary).tint(Phosphor.accent)
        .background(Phosphor.window, in: RoundedRectangle(cornerRadius: Phosphor.cardRadius))
        .overlay(RoundedRectangle(cornerRadius: Phosphor.cardRadius).stroke(Phosphor.rule))
        .buttonStyle(ConsoleButton())
        .contextMenu { Button(shell.text(.quit)) { shell.quit() } }
    }
}
