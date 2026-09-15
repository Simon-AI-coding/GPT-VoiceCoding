import AppKit
import ShellCore
import SwiftUI

struct DiagnosticRow: Identifiable {
    let title: Copy
    let value: String
    var id: Copy { title }
}

extension ShellModel {
    var diagnosticHealth: String {
        switch health {
        case .notStarted: return text(.engineNotStarted)
        case .running(let pid): return text(.engineRunning, pid)
        case .restarting(let after, let attempt):
            return text(.engineRestarting, Int(after), attempt)
        case .stopped(.repeatedFailures(let attempts)):
            return text(.engineRepeatedFailures, attempts)
        case .stopped(.anotherEngineIsListening): return text(.engineAlreadyRunning)
        case .cannotSpawn: return text(.engineCannotStart)
        case .shutDown: return text(.engineStopped)
        }
    }

    var diagnosticRows: [DiagnosticRow] {
        let absent = text(.unavailable)
        let version =
            Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String
        let revision = Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String
        return [
            DiagnosticRow(
                title: .appVersion,
                value: [version, revision].compactMap { $0 }.joined(separator: " · ")),
            DiagnosticRow(title: .engineVersion, value: panel.status?.engineVersion ?? absent),
            DiagnosticRow(title: .codexVersion, value: codexVersion ?? absent),
            DiagnosticRow(title: .realtimeModel, value: configuration?.realtimeModel ?? absent),
            DiagnosticRow(title: .socket, value: location.socketPath),
            DiagnosticRow(title: .log, value: configuration?.logPath ?? absent),
        ]
    }

    var diagnosticErrors: [String] {
        var failures = [
            locationFailure, installationFailure, pathFailure, credentialState.failureDetail,
            settingsFailure, loginItem.failure, panel.lastFailure?.detail,
            panel.modelsFailure?.detail,
            codexCheckFailure,
        ]
        if case .failed(let failure) = panel.reading { failures.append(failure.detail) }
        if case .cannotSpawn(let reason) = health { failures.append(reason.detail) }
        return failures.compactMap { $0 }
    }

    var diagnosticsText: String {
        ([diagnosticHealth]
            + diagnosticRows.map { "\(text($0.title)): \($0.value)" }
            + diagnosticErrors + [text(.verifyHint), text(.verifyLimit)]
            + (panel.seams ?? []).map { "\($0.seam): \($0.outcome)" }
            + [text(.output)] + engineOutput.suffix(50)).joined(separator: "\n")
    }
}

extension ActionFailure {
    fileprivate var detail: String {
        switch self {
        case .refused(let refusal): return refusal.message
        case .unreachable(let detail), .protocolMismatch(let detail): return detail
        }
    }
}

struct DiagnosticsView: View {
    let shell: ShellModel
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .top, spacing: 8) {
                Text("●").foregroundStyle(
                    shell.panel.engineReachable ? Phosphor.accent : Phosphor.danger)
                Text(shell.diagnosticHealth).font(Phosphor.prose)
            }
            VStack(spacing: 6) {
                ForEach(shell.diagnosticRows) { row in diagnosticRow(row) }
            }
            Button(shell.text(.codexCheckTitle)) { shell.open(.codexCheck) }
            ForEach(Array(shell.diagnosticErrors.enumerated()), id: \.offset) { _, detail in
                HStack(alignment: .top, spacing: 8) {
                    Text("●").foregroundStyle(Phosphor.danger)
                    Text(detail).font(Phosphor.prose).foregroundStyle(Phosphor.secondary)
                        .textSelection(.enabled)
                }
            }
            HStack(spacing: 8) {
                Button(shell.text(shell.panel.busy ? .checking : .verify)) {
                    Task { await shell.panel.verify() }
                }.disabled(shell.panel.busy)
                Text(shell.text(.verifyHint)).font(Phosphor.prose)
                    .foregroundStyle(Phosphor.secondary)
            }
            Text(shell.text(.verifyLimit)).font(Phosphor.prose).foregroundStyle(
                Phosphor.secondary)
            if let seams = shell.panel.seams, !seams.isEmpty {
                VStack(spacing: 0) {
                    ForEach(seams) { seam in
                        HStack {
                            Text(seam.seam).foregroundStyle(Phosphor.secondary)
                            Spacer(minLength: 8)
                            Text(seam.outcome).foregroundStyle(
                                seam.outcome == "ok" ? Phosphor.accent : Phosphor.danger)
                        }.font(Phosphor.small).padding(.horizontal, 8).padding(.vertical, 5)
                            .overlay(alignment: .bottom) { ConsoleRule() }
                    }
                }
                .overlay(
                    RoundedRectangle(cornerRadius: Phosphor.cardRadius)
                        .stroke(Phosphor.rule))
            }
            Text(shell.text(.output).uppercased()).font(Phosphor.label).tracking(0.88)
                .foregroundStyle(Phosphor.tertiary)
            ScrollView([.horizontal, .vertical]) {
                Text(shell.engineOutput.suffix(50).joined(separator: "\n"))
                    .font(Phosphor.label).textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }.frame(height: Phosphor.outputHeight).padding(8).background(Phosphor.sunken)
                .overlay(
                    RoundedRectangle(cornerRadius: Phosphor.cardRadius)
                        .stroke(Phosphor.rule))
            Button(shell.text(.copyDiagnostics)) {
                NSPasteboard.general.clearContents()
                NSPasteboard.general.setString(shell.diagnosticsText, forType: .string)
            }
        }.frame(maxWidth: .infinity, alignment: .leading)
    }

    private func diagnosticRow(_ row: DiagnosticRow) -> some View {
        HStack(spacing: 8) {
            Text(shell.text(row.title)).foregroundStyle(Phosphor.tertiary)
            Spacer(minLength: 8)
            Text(row.value).foregroundStyle(Phosphor.bright).lineLimit(1)
                .truncationMode(.middle).textSelection(.enabled)
            if row.title == .log && shell.configuration?.logPath != nil {
                Button(shell.text(.open)) {
                    NSWorkspace.shared.activateFileViewerSelecting([
                        URL(fileURLWithPath: row.value)
                    ])
                }.buttonStyle(PlainHandButton()).foregroundStyle(Phosphor.accent)
            }
        }.font(Phosphor.small)
    }
}
