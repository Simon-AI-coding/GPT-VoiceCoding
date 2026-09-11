import Foundation
import Testing

@testable import ShellCore

/// The shell's whole part in installation (ADR 0012): resolve one interpreter,
/// run one verb, report what it said. Everything the verb *does* is Python's, and
/// is tested there — a second copy of it here would be the duplication this
/// arrangement exists to avoid.
@Suite(.serialized) struct InstallationTests {
    private func withExecutable(_ names: [String], _ body: (URL) throws -> Void) rethrows {
        let directory = URL(fileURLWithPath: "/tmp/gvc-install-\(UUID().uuidString.prefix(8))")
        for name in names {
            let file = directory.appendingPathComponent(name)
            try? FileManager.default.createDirectory(
                at: file.deletingLastPathComponent(), withIntermediateDirectories: true)
            FileManager.default.createFile(
                atPath: file.path, contents: Data("#!/bin/sh\n".utf8),
                attributes: [.posixPermissions: 0o755])
        }
        defer { try? FileManager.default.removeItem(at: directory) }
        try body(directory)
    }

    @Test func itRunsTheInstallationModuleOnTheBundledInterpreter() throws {
        try withExecutable([BundleLayout.engineInterpreterRelativePath]) { resources in
            let command = try EngineCommand.resolveInstallation(
                resources: resources, verb: Installation.reconcileVerb, environment: [:],
                searchPath: [])

            #expect(
                command.executable
                    == resources.appendingPathComponent(
                        BundleLayout.engineInterpreterRelativePath
                    ).path)
            #expect(command.arguments == ["-m", "gpt_voicecoding.installation", "reconcile"])
            #expect(command.source == .bundled)
        }
    }

    @Test func itCarriesNoConfigPath() throws {
        // The engine refuses to start without a config.toml the user writes by
        // hand. An installation that waited for that file would never happen.
        try withExecutable([BundleLayout.engineInterpreterRelativePath]) { resources in
            let command = try EngineCommand.resolveInstallation(
                resources: resources, verb: Installation.reconcileVerb, environment: [:],
                searchPath: [])
            #expect(!command.arguments.contains("--config"))
        }
    }

    @Test func itUsesTheSameInterpreterSearchAsTheEngine() throws {
        try withExecutable(["python3"]) { bin in
            let command = try EngineCommand.resolveInstallation(
                resources: nil, verb: Installation.reconcileVerb, environment: [:],
                searchPath: [bin.path])
            #expect(command.executable == bin.appendingPathComponent("python3").path)
            #expect(command.source == .developerPath)
        }
    }

    @Test func aRunThatCannotStartIsReportedRatherThanThrown() async {
        let report = await InstallationRunner(readPath: LoginShellPath.unasked).run(
            EngineCommand(
                executable: "/nowhere/python3", arguments: ["-m", "x", "reconcile"],
                source: .developerPath))

        #expect(report.ok == false)
        #expect(report.failure != nil)
    }

    @Test func aSuccessfulRunCarriesItsOwnWordsAndNoFailure() async {
        // Timed, not only checked: this collected nothing on CI while passing
        // locally, because this process was holding the pipe's write end open
        // and the read could only ever end on the grace timeout. An assertion on
        // the words alone would have gone green again for the wrong reason.
        let started = Date()
        let report = await InstallationRunner(readPath: LoginShellPath.unasked).run(
            EngineCommand(
                executable: "/bin/echo", arguments: ["claude-hooks: current"],
                source: .developerPath))
        let waited = Date().timeIntervalSince(started)

        #expect(report.ok)
        #expect(report.lines == ["claude-hooks: current"])
        #expect(report.failure == nil)
        #expect(waited < Installation.grace, "the output arrived on a timeout, not on EOF")
    }

    @Test func aChildThatIgnoresSigtermIsStillStopped() async {
        // The claim is that a reconcile which hangs never becomes an engine that
        // never starts. SIGTERM is a request, and this child refuses it — so the
        // only thing that makes the claim true is the escalation after it.
        let started = Date()
        let report = await InstallationRunner(readPath: LoginShellPath.unasked).run(
            EngineCommand(
                executable: "/bin/sh",
                arguments: ["-c", "trap '' TERM; echo holding; while :; do sleep 1; done"],
                source: .developerPath),
            deadline: 1)
        let waited = Date().timeIntervalSince(started)

        #expect(report.ok == false)
        #expect(report.failure?.contains("did not finish within") == true)
        #expect(waited < 1 + (Installation.grace * 3) + 5, "the run was not bounded: \(waited)s")
    }

    @Test func aFailedRunReportsTheFirstThingItSaid() async {
        let report = await InstallationRunner(readPath: LoginShellPath.unasked).run(
            EngineCommand(
                executable: "/bin/sh",
                arguments: ["-c", "echo 'claude-hooks: FAILED — a reason'; exit 1"],
                source: .developerPath))

        #expect(report.ok == false)
        #expect(report.failure == "claude-hooks: FAILED — a reason")
    }

    // MARK: - The login PATH the reconcile resolves codex over (#272, ADR 0022)

    @Test func theChildRunsOnTheLoginShellsPath() async {
        // The whole of option A: the reconcile cannot find the user's codex on
        // the PATH launchd gives a Finder-opened app, so it is given the same
        // reading `ProcessLauncher` gives the engine.
        let report = await InstallationRunner(
            readPath: { _, _ in .said("/opt/only-here:/usr/bin:/bin") },
            environment: ["SHELL": "/bin/zsh", "PATH": "/usr/bin:/bin"]
        ).run(
            EngineCommand(
                executable: "/bin/sh", arguments: ["-c", "printf '%s' \"$PATH\""],
                source: .developerPath))

        #expect(report.lines == ["/opt/only-here:/usr/bin:/bin"])
    }

    @Test func aLoginShellThatSaysNothingLeavesTheChildOnThePathWeHad() async {
        // Fails open, exactly as the engine's spawn does: a reading that could
        // not be taken may never make a run worse than not asking at all.
        let report = await InstallationRunner(
            readPath: { _, _ in .ranOutOfTime },
            environment: ["SHELL": "/bin/zsh", "PATH": "/usr/bin:/bin"]
        ).run(
            EngineCommand(
                executable: "/bin/sh", arguments: ["-c", "printf '%s' \"$PATH\""],
                source: .developerPath))

        #expect(report.lines == ["/usr/bin:/bin"])
    }

    @Test func whatTheReadingCameToIsReportedEveryRun() async {
        // Every run, including the ones that worked — a surface clears its own
        // warning by being told the next reading was fine, and a report that
        // only fired on failure would leave a stale one up for ever.
        let recorded = Recorder()
        _ = await InstallationRunner(
            readPath: { _, _ in .said("/opt/only-here") },
            report: { recorded.record($0) },
            environment: ["SHELL": "/bin/zsh", "PATH": "/usr/bin:/bin"]
        ).run(
            EngineCommand(executable: "/bin/echo", arguments: [], source: .developerPath))

        #expect(recorded.latest == .adopted(shell: "/bin/zsh", path: "/opt/only-here"))
    }

    @Test func theLoginShellReadIsOffThePoolAndOutsideTheSubprocessDeadline() async {
        // #276's third finding. `run` promises it waits on threads of its own
        // and never on a pooled one, and the login-shell read — a semaphore
        // with a ten-second budget — used to happen above the hand-off, on
        // whatever cooperative-pool thread the caller was on. One core out of
        // about one per core, for as long as somebody's profile takes.
        //
        // Both halves of the move are pinned here because they pull opposite
        // ways: the read has to be on the dedicated thread, *and* its time
        // still must not be charged to the subprocess ceiling — which is the
        // reason the read was above the hand-off in the first place. So the
        // reader sleeps past the deadline and the child is expected to finish
        // normally regardless.
        let observed = ThreadName()
        let slower = 1.5
        let report = await InstallationRunner(
            readPath: { _, _ in
                observed.record(Thread.current.name)
                Thread.sleep(forTimeInterval: slower)
                return .said("/opt/only-here:/usr/bin:/bin")
            },
            environment: ["SHELL": "/bin/zsh", "PATH": "/usr/bin:/bin"]
        ).run(
            EngineCommand(
                executable: "/bin/sh", arguments: ["-c", "printf '%s' \"$PATH\""],
                source: .developerPath),
            deadline: slower / 2)

        // Not the caller's thread, and not any of the pool's: the one `run` made.
        #expect(observed.name == "gpt-voicecoding.installation")
        // The child ran to completion on a deadline shorter than the read.
        #expect(report.ok)
        #expect(report.lines == ["/opt/only-here:/usr/bin:/bin"])
    }

    // MARK: - Stating that PATH, rather than letting the child inherit one (#327)

    @Test func theReadingIsStatedToTheChildUnderItsOwnName() async {
        // The child may not take this from `PATH`, because a terminal has one of
        // those too and the render it wrote carried that terminal's entries. So
        // the reading arrives under a name only this app ever sets.
        let report = await InstallationRunner(
            readPath: { _, _ in .said("/opt/only-here:/usr/bin:/bin") },
            environment: ["SHELL": "/bin/zsh", "PATH": "/usr/bin:/bin"]
        ).run(
            EngineCommand(
                executable: "/bin/sh",
                arguments: ["-c", "printf '%s' \"$\(Installation.loginPathVariable)\""],
                source: .developerPath))

        #expect(report.lines == ["/opt/only-here:/usr/bin:/bin"])
    }

    @Test func aReadingThatWasNotTakenStatesNothing() async {
        // The fail-open, carried across: a reading that could not be taken states
        // nothing rather than stating a guess, and the child falls back to the
        // PATH the standing job records. Stating `PATH` here — which is launchd's
        // own after a failed read — is the one answer that must not happen.
        let report = await InstallationRunner(
            readPath: { _, _ in .ranOutOfTime },
            environment: ["SHELL": "/bin/zsh", "PATH": "/usr/bin:/bin"]
        ).run(
            EngineCommand(
                executable: "/bin/sh",
                arguments: ["-c", "printf '%s' \"$\(Installation.loginPathVariable)\""],
                source: .developerPath))

        #expect(report.lines == [])
    }

    @Test func anInheritedStatementIsNotOneAndIsTakenAway() async {
        // Stated, never inherited — and an app opened from a shell that exported
        // this name would otherwise hand the child a value nobody read. It is
        // removed on every path the reading did not produce, so the name means
        // one thing only: this launch read the login shell and got this answer.
        let report = await InstallationRunner(
            readPath: { _, _ in .ranOutOfTime },
            environment: [
                "SHELL": "/bin/zsh", "PATH": "/usr/bin:/bin",
                Installation.loginPathVariable: "/somewhere/a/terminal/exported",
            ]
        ).run(
            EngineCommand(
                executable: "/bin/sh",
                arguments: ["-c", "printf '%s' \"$\(Installation.loginPathVariable)\""],
                source: .developerPath))

        #expect(report.lines == [])
    }

    @Test func aBundledRunStillKeepsBytecodeOutOfTheBundle() async {
        // The PATH handover replaced the branch that built this environment, so
        // the rule it used to carry is pinned rather than assumed to have come
        // across with it.
        let report = await InstallationRunner(
            readPath: LoginShellPath.unasked,
            environment: ["PATH": "/usr/bin:/bin"]
        ).run(
            EngineCommand(
                executable: "/bin/sh",
                arguments: ["-c", "printf '%s' \"$PYTHONDONTWRITEBYTECODE\""],
                source: .bundled))

        #expect(report.lines == ["1"])
    }
}

/// One outcome, written on the runner's thread and read on the test's.
private final class Recorder: @unchecked Sendable {
    private let lock = NSLock()
    private var last: LoginShellPath.Outcome?

    func record(_ outcome: LoginShellPath.Outcome) { lock.withLock { last = outcome } }
    var latest: LoginShellPath.Outcome? { lock.withLock { last } }
}
