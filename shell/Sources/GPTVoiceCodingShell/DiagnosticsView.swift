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
            credentialSaveFailure, panel.lastFailure?.detail,
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
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                Text(shell.diagnosticHealth)
                    .font(Phosphor.prose)
                ForEach(shell.diagnosticRows) { row in
                    VStack(alignment: .leading, spacing: 4) {
                        Text(shell.text(row.title)).font(Phosphor.label).foregroundStyle(
                            Phosphor.tertiary)
                        Text(row.value).textSelection(.enabled)
                        if row.title == .socket
                            || row.title == .log && shell.configuration?.logPath != nil
                        {
                            Button(shell.text(.open)) {
                                NSWorkspace.shared.activateFileViewerSelecting([
                                    URL(fileURLWithPath: row.value)
                                ])
                            }
                        }
                    }
                }
                ForEach(Array(shell.diagnosticErrors.enumerated()), id: \.offset) { _, detail in
                    Text(detail).font(Phosphor.prose).foregroundStyle(Phosphor.danger)
                        .textSelection(.enabled)
                }
                Button(shell.text(shell.panel.busy ? .checking : .verify)) {
                    Task { await shell.panel.verify() }
                }.disabled(shell.panel.busy)
                Text(shell.text(.verifyHint)).font(Phosphor.prose)
                Text(shell.text(.verifyLimit)).font(Phosphor.prose).foregroundStyle(
                    Phosphor.secondary)
                ForEach(shell.panel.seams ?? []) { seam in
                    HStack {
                        Text(seam.seam)
                        Spacer()
                        Text(seam.outcome)
                    }
                }
                Text(shell.text(.output)).font(Phosphor.label).foregroundStyle(Phosphor.tertiary)
                ScrollView([.horizontal, .vertical]) {
                    Text(shell.engineOutput.suffix(50).joined(separator: "\n"))
                        .font(Phosphor.label).textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }.frame(height: Phosphor.outputHeight).background(Phosphor.sunken)
                Button(shell.text(.copyDiagnostics)) {
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString(shell.diagnosticsText, forType: .string)
                }
            }.frame(maxWidth: .infinity, alignment: .leading)
        }.frame(idealHeight: Phosphor.rosterHeight + Phosphor.outputHeight)
    }
}
