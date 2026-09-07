import Foundation

/// First launch is the install — ADR 0012.
///
/// A `.app` dragged into `/Applications` has no install step: macOS copies a
/// directory and runs nothing. So the shell runs one reconcile before it spawns
/// the engine, and the reconcile writes nothing when the machine already agrees
/// with what this build would put there.
///
/// **The shell is the trigger and nothing more.** The merge, the fingerprint and
/// the reversibility all stay in Python, in one module. A second copy of them
/// here would be the shape of #47, where the control-plane socket path is built
/// independently on both sides with no test holding the two together — so this
/// file knows one verb and how to wait for it, and nothing about settings files.
///
/// **A reconcile that fails does not stop the engine.** What the user loses is
/// reach into their Sessions, which is what the failed item was for; an app that
/// refused to start over it would take the control plane and the Live Call down
/// with it.
public enum Installation {
    /// The only verb the shell uses. `install`, `uninstall` and `status` are the
    /// operator's, through the `bridge-install` console script.
    public static let reconcileVerb = "reconcile"

    /// How long one reconcile may take before the shell stops waiting for it. It
    /// reads and writes two small files; a run that is still going after this is
    /// blocked on something, and the engine should not wait behind it.
    public static let deadline: TimeInterval = 30

    /// How long each step of stopping it is given before the next, harder one.
    /// Short: by the time this is being counted, the run has already failed.
    public static let grace: TimeInterval = 2
}

/// What one reconcile said. The lines are the run's own words, never rephrased.
public struct InstallationReport: Equatable, Sendable {
    public var ok: Bool
    public var lines: [String]

    public init(ok: Bool, lines: [String]) {
        self.ok = ok
        self.lines = lines
    }

    /// The first sentence worth showing a person, or `nil` when nothing is wrong.
    public var failure: String? {
        ok ? nil : (lines.first ?? "the installation could not be reconciled")
    }
}

/// What one run collected, across the queue that collected it.
private final class OutputBox: @unchecked Sendable {
    private let lock = NSLock()
    private var value = Data()

    func set(_ data: Data) {
        lock.lock()
        value = data
        lock.unlock()
    }

    var lines: [String] {
        lock.lock()
        defer { lock.unlock() }
        return String(decoding: value, as: UTF8.self)
            .split(separator: "\n", omittingEmptySubsequences: true)
            .map(String.init)
    }
}

/// Runs one installation verb, and comes back whatever the child does.
///
/// **Every wait here is bounded, and that is the whole design.** This runs
/// *before* the engine is spawned, so a wait without a ceiling is not a slow
/// installation — it is a product that never starts. `waitUntilExit` and
/// `readDataToEndOfFile` are both unbounded, and `SIGTERM` is a request a child
/// may ignore, so neither is used on its own: the run is waited for with a
/// deadline, then asked to stop, then made to stop, and the output is collected
/// with a ceiling of its own because a grandchild holding the pipe would keep it
/// open after the child is gone.
///
/// **The child gets the user's real `PATH`, exactly as the engine does — #272,
/// ADR 0022.** The reconcile has to resolve the one codex the user has, and it
/// cannot: launchd gives a Finder-opened app `/usr/bin:/bin:/usr/sbin:/sbin`,
/// nothing sets this app's own `PATH`, and `which codex` on that finds nothing
/// on a machine that has one. `ProcessLauncher` already solves this for the
/// engine, through ``LoginShellPath``, and #272's ruling (option A) is that the
/// shell hands the same reading to this subprocess rather than have Python read
/// the login shell a second way. So there is **one** implementation of the
/// login-and-interactive lesson on this machine, and it is the one that already
/// learned it the hard way.
///
/// **The invariant is one implementation, not one reading** (Simon's ruling on
/// #272's review). This reads the login shell and so does every engine spawn,
/// so a launch pays for two — one extra login-shell read per app launch,
/// ~0.5 s measured, inside the 30 s ``Installation/deadline`` and spent before
/// the engine starts. That cost buys nothing that could be called consistency:
/// the plist this renders is a **snapshot** until the next reconcile, while
/// ``ProcessLauncher`` re-reads on every spawn by its own documented rule, so
/// the two already diverge by design the moment somebody edits their profile.
/// Handing the reconcile's reading to the first spawn would protect one second
/// of agreement between two values that are meant to be read at different
/// times — a special case, and a copy of somebody's profile with a lifetime to
/// reason about, which is what ``LoginShellPath``'s own ruling is against.
///
/// What *is* shared is the thing that can be got wrong: `-l -i`, the sentinels,
/// the budget and the fail-open. One implementation of that lesson on this
/// machine, which is what #272 refused option (B) over.
public struct InstallationRunner: Sendable {
    /// Where the `PATH` comes from. A seam for the same reason
    /// ``ProcessLauncher``'s is: a suite that is not about the `PATH` may
    /// decline to start a login shell (`#36`).
    private let readPath: LoginShellPath.Reader

    /// What the reading came to, handed to whoever owns a surface. The launcher
    /// reports its own the same way, into the same sink, so the panel shows one
    /// answer about this machine's `PATH` and not two that can disagree.
    private let report: @Sendable (LoginShellPath.Outcome) -> Void

    /// Where this runner's one sentence goes. Not the engine's log: ADR 0004
    /// gives that to the engine, and this runs before there is one.
    private let log: @Sendable (String) -> Void

    private let environment: [String: String]

    public init(
        readPath: @escaping LoginShellPath.Reader = LoginShellPath.readFromLoginShell,
        report: @escaping @Sendable (LoginShellPath.Outcome) -> Void = { _ in },
        log: @escaping @Sendable (String) -> Void = ProcessLauncher.unifiedLog,
        environment: [String: String] = ProcessInfo.processInfo.environment
    ) {
        self.readPath = readPath
        self.report = report
        self.log = log
        self.environment = environment
    }

    /// Waits on threads of its own, and never on a pooled one.
    ///
    /// Everything below is blocking — semaphores and `readDataToEndOfFile` — and
    /// whose thread it blocks is not a detail. Swift concurrency's cooperative
    /// pool holds about one thread per core, so a `Task` that blocks one for the
    /// length of a subprocess is a core the whole process cannot use; CI found
    /// this by slowing every unrelated test in the suite by four times. So the
    /// waiting happens on threads created for it and thrown away after, and the
    /// public entry point is `async` and gives its caller's thread back.
    ///
    /// **The login-shell read is one of those waits** (#276). It used to happen
    /// here, before the hand-off, for a reason that was about the *deadline* and
    /// not about the thread: a reading started inside `runBlocking` would put a
    /// login shell's whole profile inside a subprocess ceiling it is not part
    /// of. But `LoginShellPath.apply` blocks on a semaphore for up to
    /// ``LoginShellPath/timeout`` — ten seconds — so keeping it above the
    /// continuation held a pooled thread for exactly as long as the sentence
    /// above says it never does. It is on the dedicated thread now, and the
    /// deadline is still not charged for it, because `runBlocking` starts its
    /// clock when it is called and the read has already returned by then.
    ///
    /// `deadline` is a parameter for one reason: a test that proved the ceiling
    /// by waiting out the real one would take longer than the whole suite.
    public func run(
        _ command: EngineCommand, deadline: TimeInterval = Installation.deadline
    ) async -> InstallationReport {
        return await withCheckedContinuation { continuation in
            let thread = Thread {
                let path = LoginShellPath.apply(to: environment, read: readPath, log: log)
                // Every run, including the ones that worked, for the reason the
                // launcher reports every spawn: a surface clears its own warning
                // by being told the next reading was fine.
                report(path.outcome)
                continuation.resume(
                    returning: Self.runBlocking(
                        command, environment: path.environment, deadline: deadline))
            }
            thread.name = "gpt-voicecoding.installation"
            thread.start()
        }
    }

    private static func runBlocking(
        _ command: EngineCommand, environment: [String: String], deadline: TimeInterval
    ) -> InstallationReport {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: command.executable)
        process.arguments = command.arguments
        var childEnvironment = environment
        if command.source == .bundled {
            // Nothing may write into the bundle at runtime, and a `.pyc` beside a
            // signed file is a modification of a signed bundle.
            childEnvironment["PYTHONDONTWRITEBYTECODE"] = "1"
        }
        process.environment = childEnvironment

        let output = Pipe()
        process.standardOutput = output
        process.standardError = output

        let exited = DispatchSemaphore(value: 0)
        process.terminationHandler = { _ in exited.signal() }

        do {
            try process.run()
        } catch {
            return InstallationReport(ok: false, lines: ["\(command.executable): \(error)"])
        }

        // This process keeps its own copy of the pipe's write end, and the read
        // below ends when *every* write end is closed — so leaving ours open
        // means the child can exit and the read still never returns. Foundation
        // closes it on some platform versions and not others, which is worse
        // than never closing it: it passes on the machine you wrote it on.
        try? output.fileHandleForWriting.close()

        // Drained from the start, on a thread of its own: a pipe nobody reads
        // fills, and a child blocked on a full pipe is a child that never exits.
        // A thread rather than a queue for the reason in the type's own note —
        // this read has no ceiling of its own, so it must not sit on a shared
        // worker while it waits.
        let collected = OutputBox()
        let drained = DispatchSemaphore(value: 0)
        let reading = output.fileHandleForReading
        let reader = Thread {
            collected.set(reading.readDataToEndOfFile())
            drained.signal()
        }
        reader.name = "gpt-voicecoding.installation.read"
        reader.start()

        if exited.wait(timeout: .now() + deadline) == .timedOut {
            process.terminate()
            if exited.wait(timeout: .now() + Installation.grace) == .timedOut {
                kill(process.processIdentifier, SIGKILL)
                _ = exited.wait(timeout: .now() + Installation.grace)
            }
            _ = drained.wait(timeout: .now() + Installation.grace)
            return InstallationReport(
                ok: false,
                lines: [
                    "the installation reconcile did not finish within "
                        + "\(Int(deadline)) seconds and was stopped"
                ] + collected.lines)
        }

        _ = drained.wait(timeout: .now() + Installation.grace)
        return InstallationReport(ok: process.terminationStatus == 0, lines: collected.lines)
    }
}
