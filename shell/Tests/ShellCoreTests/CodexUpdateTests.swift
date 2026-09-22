import Foundation
import Testing

@testable import ShellCore

@Suite(.serialized) struct CodexUpdateTests {
    actor Checks {
        var calls = 0
        var active = 0
        var peak = 0
        var release: CheckedContinuation<Void, Never>?

        func run(_ attempted: String) async -> InstallationReport {
            calls += 1
            active += 1
            peak = max(peak, active)
            if calls == 1 { await withCheckedContinuation { release = $0 } }
            active -= 1
            return InstallationReport(
                ok: true,
                lines: [
                    #"{"status":"unchanged","failure":"","attempted":"","watch_paths":[]}"#
                ])
        }

        func unblock() {
            release?.resume()
            release = nil
        }
    }

    @Test @MainActor func repeatedEventsSerializeAndQuitDiscardsQueuedCheck() async {
        let checks = Checks()
        let updater = CodexUpdate(check: { await checks.run($0) }, report: { _ in })
        updater.checkForUpdate()
        while await checks.calls == 0 { await Task.yield() }
        updater.checkForUpdate()
        updater.checkForUpdate()
        let stopping = Task { await updater.stop() }
        await Task.yield()
        await checks.unblock()
        await stopping.value
        updater.checkForUpdate()
        #expect(await checks.calls == 1)
        #expect(await checks.peak == 1)
    }

    actor FileChecks {
        let paths: [String]
        var calls = 0
        init(_ paths: [String]) { self.paths = paths }
        func run() -> InstallationReport {
            calls += 1
            let json = try! JSONSerialization.data(withJSONObject: [
                "status": "unchanged", "failure": "", "attempted": "", "watch_paths": paths,
            ])
            return InstallationReport(ok: true, lines: [String(decoding: json, as: UTF8.self)])
        }
    }

    @Test @MainActor func fileWritesAndReplacedDirectoriesKeepTriggeringChecks() async throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        let package = root.appendingPathComponent("package")
        let file = package.appendingPathComponent("codex")
        try FileManager.default.createDirectory(at: package, withIntermediateDirectories: true)
        try Data("first".utf8).write(to: file)
        defer { try? FileManager.default.removeItem(at: root) }
        let checks = FileChecks([package.path])
        let updater = CodexUpdate(check: { _ in await checks.run() }, report: { _ in })
        updater.checkForUpdate()
        try await wait(checks, after: 1)
        var previous = await checks.calls
        try Data("second".utf8).write(to: file)
        try await wait(checks, after: previous)
        previous = await checks.calls
        try FileManager.default.removeItem(at: package)
        try FileManager.default.createDirectory(at: package, withIntermediateDirectories: true)
        try Data("third".utf8).write(to: file)
        try await wait(checks, after: previous)
        previous = await checks.calls
        try Data("fourth".utf8).write(to: file)
        try await wait(checks, after: previous)
        await updater.stop()
        previous = await checks.calls
        try Data("after stop".utf8).write(to: file)
        try await Task.sleep(for: .milliseconds(350))
        #expect(await checks.calls == previous)
    }

    private func wait(_ checks: FileChecks, after previous: Int) async throws {
        let deadline = ContinuousClock.now + .seconds(5)
        while await checks.calls <= previous && ContinuousClock.now < deadline {
            try await Task.sleep(for: .milliseconds(20))
        }
        #expect(await checks.calls > previous)
    }

}
