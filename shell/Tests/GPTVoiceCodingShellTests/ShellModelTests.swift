import AppKit
import Foundation
import ObjectiveC
import Observation
import ShellTestSupport
import Testing

@testable import GPTVoiceCodingShell
@testable import ShellCore

@MainActor
@Suite struct ShellModelTests {
    @Test func diagnosticHealthComesFromSupervisionNotSocketReachability() async throws {
        let shell = try ShellHarness()
        #expect(shell.model.diagnosticsText.contains(shell.model.text(.engineNotStarted)))
        try shell.fixture.writeEnvironment("A_TELEGRAM_TOKEN=ready\n")
        await shell.model.startEngineAfterInstallation()
        #expect(await waitUntil { shell.model.health == .running(pid: 2001) })
        #expect(!shell.model.panel.engineReachable)
        #expect(shell.model.diagnosticsText.contains(shell.model.text(.engineRunning, 2001)))
        await shell.model.stopEngine()
    }

    @Test func anOpenSessionIsReadWithItsTargetEveryTwoSeconds() async throws {
        let fixture = try TelegramCredentialFixture()
        let engine = DesktopControlPlane()
        let target = SessionAddress(.of(["agent": "claude", "session_id": "one"]))
        let (_, model) = makeShell(
            fixture: fixture, launcher: RecordingEngineLauncher(),
            panel: ControlPanel(client: engine))
        model.open(.session(target))
        #expect(await waitUntil { engine.briefTargets.count == 2 })
        #expect(engine.briefTargets == [target.payload, target.payload])
        model.open(.home)
        try await Task.sleep(for: .seconds(2.2))
        #expect(engine.briefTargets.count == 2)
        await model.stopEngine()
    }

    @Test func newAgentOnACallAsksOnceAndOnlyAcceptanceHangsUpAndRedials() async throws {
        let fixture = try TelegramCredentialFixture()
        let engine = DesktopControlPlane()
        let panel = ControlPanel(client: engine)
        let (_, model) = makeShell(
            fixture: fixture, launcher: RecordingEngineLauncher(), panel: panel)
        await panel.toggleLive()
        #expect(panel.phase == .onCall)
        let before = engine.requests
        await model.requestNewAgent()
        #expect(model.confirmation == .newAgent)
        #expect(engine.requests == before)
        await model.resolveConfirmation(accept: false)
        #expect(model.confirmation == nil)
        #expect(engine.requests == before)
        await model.requestNewAgent()
        await model.resolveConfirmation(accept: true)
        #expect(engine.requests == before + ["live", "status", "live", "status"])
        #expect(panel.phase == .onCall)
        await model.stopEngine()
    }

    @Test func quittingOnACallOffersToKeepItAndDoesNotStopTheEngine() async throws {
        let fixture = try TelegramCredentialFixture()
        let engine = DesktopControlPlane()
        let panel = ControlPanel(client: engine)
        let (_, model) = makeShell(
            fixture: fixture, launcher: RecordingEngineLauncher(), panel: panel)
        await panel.toggleLive()
        let reply = ShellDelegate(shell: model).applicationShouldTerminate(NSApplication.shared)
        #expect(reply == .terminateCancel)
        #expect(model.confirmation == .quit)
        #expect(!model.stopping)
        await model.resolveConfirmation(accept: false)
        #expect(panel.phase == .onCall)
        #expect(!model.stopping)
        await model.stopEngine()
    }

    @Test func anUnboundTelegramNeverReadsAsConnected() async throws {
        let fixture = try TelegramCredentialFixture()
        let engine = DesktopControlPlane()
        let panel = ControlPanel(client: engine)
        let (_, model) = makeShell(
            fixture: fixture, launcher: RecordingEngineLauncher(), panel: panel)
        await panel.refresh()
        #expect(!model.telegramConnected)
        await model.readConfiguration { ShellConfiguration(["telegram_bound": .bool(true)]) }
        #expect(model.telegramConnected)
        await model.stopEngine()
    }

    @Test func aSlowStatusReplyCannotStopTheDialTimer() async throws {
        let fixture = try TelegramCredentialFixture()
        let engine = SlowDesktopPlane()
        let clock = DesktopClock()
        let panel = ControlPanel(client: engine, now: { clock.time })
        let (_, model) = makeShell(
            fixture: fixture, launcher: RecordingEngineLauncher(),
            panel: panel, now: { clock.time }, sleep: clock.sleep)
        let dial = Task { await panel.toggleLive() }
        #expect(await waitUntil { panel.phase == .calling })
        model.open(.home)
        #expect(await waitUntil { clock.waiting })
        clock.advance()
        #expect(await waitUntil { panel.elapsed == 1 })
        engine.status.resolve()
        engine.live.resolve()
        await dial.value
        await model.stopEngine()
        clock.advance()
    }

    @Test func aConfigurationFailureOpensTheFixedPageAndKeepsDetailInDiagnostics() async throws {
        let shell = try ShellHarness()
        await shell.model.readConfiguration {
            throw ConfigurationFailure.unreadable("broken TOML in a private path")
        }
        #expect(shell.model.page == .unreadableSettings)
        #expect(shell.model.locationFailure == "broken TOML in a private path")
        #expect(shell.model.diagnosticsText.contains("broken TOML in a private path"))
        await shell.model.stopEngine()
    }

    @Test func configuredAgentValuesAndTelegramBindingAreReadWithoutAnAgent() async throws {
        let shell = try ShellHarness()
        await shell.model.readConfiguration {
            ShellConfiguration([
                "model": .string("chosen-model"), "effort": .string("high"),
                "telegram_bound": .bool(true), "realtime_model": .string("chosen-realtime"),
                "log_path": .string("/tmp/chosen.log"),
            ])
        }
        #expect(shell.model.configuration?.model == "chosen-model")
        #expect(shell.model.configuration?.effort == "high")
        #expect(shell.model.configuration?.telegramBound == true)
        #expect(shell.model.panel.status?.callAgent == nil)
        #expect(shell.model.page == nil)
        #expect(shell.model.diagnosticsText.contains("/tmp/chosen.log"))
        await shell.model.stopEngine()
    }

    @Test func onePollerFollowsVisibilityAndSharesTheTwoSecondBriefRead() async throws {
        let fixture = try TelegramCredentialFixture()
        let engine = DesktopControlPlane()
        let (_, model) = makeShell(
            fixture: fixture, launcher: RecordingEngineLauncher(),
            panel: ControlPanel(client: engine))
        model.open(.home)
        #expect(await waitUntil { engine.requests.count == 5 })
        #expect(engine.requests.filter { $0 == "brief" }.count == 2)
        #expect(engine.requests.filter { $0 == "status" }.count == 3)
        model.closeWindow()
        #expect(await waitUntil { engine.requests.count == 7 })
        #expect(engine.requests.count == 7)
        engine.duty = false
        #expect(await waitUntil { !model.cardVisible })
        let statusTimes = engine.times(for: "status")
        let briefTimes = engine.times(for: "brief")
        // Exercise the real timer at the dialer boundary; allow scheduler jitter,
        // but distinguish one-second window reads from two-second card reads.
        for interval in zip(statusTimes, statusTimes.dropFirst()).map({ $1 - $0 }).prefix(2) {
            #expect((0.8..<1.8).contains(interval))
        }
        for interval in zip(statusTimes, statusTimes.dropFirst()).map({ $1 - $0 }).dropFirst(2) {
            #expect((1.8..<2.8).contains(interval))
        }
        for interval in zip(briefTimes, briefTimes.dropFirst()).map({ $1 - $0 }) {
            #expect((1.8..<2.8).contains(interval))
        }
        let count = engine.requests.count
        try await Task.sleep(for: .seconds(2.2))
        #expect(engine.requests.count == count)
        await model.stopEngine()
    }

    @Test func appKitInitializerIsAnObjectiveCEntryPoint() {
        var methodCount: UInt32 = 0
        guard let methods = class_copyMethodList(ShellDelegate.self, &methodCount) else {
            Issue.record("ShellDelegate exposes no Objective-C methods")
            return
        }
        defer { free(methods) }

        let ownsInitializer = (0..<Int(methodCount)).contains {
            method_getName(methods[$0]) == #selector(NSObject.init)
        }

        #expect(ownsInitializer)
    }

    @Test func quittingBeforeAnyViewExistsStopsTheEngine() async throws {
        let shell = try ShellHarness()
        try shell.fixture.writeEnvironment("A_TELEGRAM_TOKEN=ready\n")
        await shell.model.startEngineAfterInstallation()
        #expect(await waitUntil { shell.launcher.launchCount == 1 })
        let delegate = ShellDelegate(shell: shell.model)

        let reply = delegate.applicationShouldTerminate(NSApplication.shared)

        #expect(reply == .terminateLater)
        #expect(await waitUntil { shell.launcher.stopCount == 1 })
    }

    @Test func theMenuMarkStaysTheSameWhenEngineHealthChanges() async throws {
        let fixture = try TelegramCredentialFixture()
        try fixture.writeEnvironment("A_TELEGRAM_TOKEN=ready\n")
        let launcher = RecordingEngineLauncher()
        let (_, model) = makeShell(fixture: fixture, launcher: launcher)
        let delegate = ShellDelegate(shell: model)
        let before = DesignMark.menu.image.tiffRepresentation

        await delegate.shell.startEngineAfterInstallation()

        #expect(await waitUntil { launcher.launchCount == 1 })
        #expect(DesignMark.menu.image.isTemplate)
        #expect(DesignMark.menu.image.tiffRepresentation == before)
        await delegate.shell.stopEngine()
    }

    @Test func preparationArrivingAfterTerminationCannotLaunchAnEngine() async throws {
        let fixture = try TelegramCredentialFixture()
        try fixture.writeEnvironment("A_TELEGRAM_TOKEN=ready\n")
        let launcher = RecordingEngineLauncher()
        let (_, model) = makeShell(fixture: fixture, launcher: launcher)
        await model.stopEngine()

        await model.startEngineAfterInstallation()

        #expect(launcher.launchCount == 0)
    }

    @Test func aRepairedCredentialStartsThePreflightHeldEngineExactlyOnce() async throws {
        let shell = try ShellHarness()
        await shell.model.startEngineAfterInstallation()

        #expect(shell.model.credentialState == .missing)
        #expect(shell.launcher.launchCount == 0)

        try shell.fixture.writeEnvironment("A_TELEGRAM_TOKEN=ready\n")
        #expect(await waitUntil { shell.launcher.launchCount >= 1 })
        try shell.fixture.writeEnvironment("A_TELEGRAM_TOKEN=still-ready\n")
        try? await Task.sleep(for: .milliseconds(50))

        #expect(shell.model.credentialState == .ready)
        #expect(shell.launcher.launchCount == 1)
        await shell.model.stopEngine()
    }

    @Test func invalidIntermediateCredentialsDoNotReleaseThePreflightHold() async throws {
        let shell = try ShellHarness()
        await shell.model.startEngineAfterInstallation()

        try shell.fixture.writeEnvironment("not-an-assignment\n")
        #expect(
            await waitUntil {
                if case .unreadable = shell.model.credentialState { return true }
                return false
            })
        #expect(shell.launcher.launchCount == 0)

        try shell.fixture.writeEnvironment("A_TELEGRAM_TOKEN=ready\n")
        #expect(await waitUntil { shell.launcher.launchCount >= 1 })
        await shell.model.stopEngine()
    }

    @Test func aCredentialRepairedBeforeSpawnFailureIsPublishedCanStartAgain() async throws {
        let fixture = try TelegramCredentialFixture()
        let launcher = CredentialRacingLauncher(fixture: fixture)
        let (_, model) = makeShell(fixture: fixture, launcher: launcher)
        await model.startEngineAfterInstallation()

        try fixture.writeEnvironment("A_TELEGRAM_TOKEN=first-repair\n")

        #expect(await waitUntil { launcher.launchCount >= 2 })
        #expect(await waitUntil { model.credentialState == .ready })
        try? await Task.sleep(for: .milliseconds(50))
        #expect(launcher.launchCount == 2)
        await model.stopEngine()
    }

    @Test func aNonCredentialSpawnFailureDoesNotBecomeACredentialRetry() async throws {
        let fixture = try TelegramCredentialFixture()
        let launcher = NonCredentialFailingLauncher()
        let (_, model) = makeShell(fixture: fixture, launcher: launcher)
        await model.startEngineAfterInstallation()

        try fixture.writeEnvironment("A_TELEGRAM_TOKEN=first-repair\n")
        #expect(await waitUntil { launcher.launchCount >= 1 })
        #expect(
            await waitUntil {
                if case .cannotSpawn = model.health { return true }
                return false
            })

        try fixture.writeEnvironment("A_TELEGRAM_TOKEN=duplicate-ready\n")
        try? await Task.sleep(for: .milliseconds(50))

        #expect(launcher.launchCount == 1)
        await model.stopEngine()
    }

    @Test func repairingTheCredentialCannotRestartAnAlreadyRunningEngine() async throws {
        let shell = try ShellHarness()
        await shell.model.startEngineAfterInstallation()
        await shell.supervisor.start()
        #expect(await waitUntil { shell.launcher.launchCount >= 1 })
        #expect(
            await waitUntil {
                if case .running = shell.model.health { return true }
                return false
            })

        try shell.fixture.writeEnvironment("A_TELEGRAM_TOKEN=ready\n")
        try? await Task.sleep(for: .milliseconds(50))

        #expect(shell.launcher.launchCount == 1)
        await shell.model.stopEngine()
    }

    @Test func panelSaveKeepsItsSingleOrderlyStartWhilePreflightIsHeld() async throws {
        let shell = try ShellHarness()
        await shell.model.startEngineAfterInstallation()

        #expect(await shell.model.saveTelegramToken("ready"))
        #expect(await waitUntil { shell.launcher.launchCount >= 1 })
        try? await Task.sleep(for: .milliseconds(50))

        #expect(shell.launcher.launchCount == 1)
        await shell.model.stopEngine()
    }

    @Test func panelSaveReplacesARunningEngineExactlyOnce() async throws {
        let shell = try ShellHarness()
        try shell.fixture.writeEnvironment("A_TELEGRAM_TOKEN=ready\n")
        await shell.model.startEngineAfterInstallation()
        #expect(await waitUntil { shell.launcher.launchCount == 1 })

        #expect(await shell.model.saveTelegramToken("replacement"))

        #expect(await waitUntil { shell.launcher.stopCount == 1 })
        #expect(await waitUntil { shell.launcher.launchCount == 2 })
        await shell.model.stopEngine()
    }

    @Test func aPreflightHeldEngineStartsOnceWhenTheCredentialBecomesReady() {
        var recovery = CredentialStartRecovery()

        #expect(recovery.prepare(for: .missing) == .watch)
        #expect(recovery.credentialChanged(to: .ready, health: .notStarted) == .start)
        #expect(recovery.credentialChanged(to: .ready, health: .notStarted) == .none)
        #expect(
            recovery.engineChanged(to: .running(pid: 123), credentialState: .ready)
                == .stopWatching)
        #expect(recovery.credentialChanged(to: .ready, health: .notStarted) == .none)
    }

    @Test func anEarlySecondRepairRestartsOnlyATypedCredentialFailure() {
        var credentialFailure = CredentialStartRecovery()

        #expect(credentialFailure.prepare(for: .missing) == .watch)
        #expect(
            credentialFailure.credentialChanged(to: .ready, health: .notStarted) == .start)
        #expect(
            credentialFailure.engineChanged(
                to: .cannotSpawn(.credentials(.missing)), credentialState: .ready)
                == .start)
        #expect(credentialFailure.credentialChanged(to: .ready, health: .notStarted) == .none)

        var launchFailure = CredentialStartRecovery()
        #expect(launchFailure.prepare(for: .missing) == .watch)
        #expect(launchFailure.credentialChanged(to: .ready, health: .notStarted) == .start)
        #expect(
            launchFailure.engineChanged(
                to: .cannotSpawn(.launch("permission denied")), credentialState: .ready)
                == .stopWatching)
    }

    @Test func invalidCredentialChangesKeepThePreflightHoldOpen() {
        var recovery = CredentialStartRecovery()
        let invalidStates: [TelegramCredentials.State] = [
            .missing,
            .unsafe(.permissions(path: "/tmp/unsafe")),
            .unreadable(
                .environment(path: "/tmp/malformed", problem: .missingAssignment(line: 1))),
        ]

        #expect(recovery.prepare(for: .missing) == .watch)
        for state in invalidStates {
            #expect(recovery.credentialChanged(to: state, health: .notStarted) == .none)
        }
        #expect(recovery.credentialChanged(to: .ready, health: .notStarted) == .start)
    }

    @Test func aCredentialChangeCannotRestartAnEngineThatIsAlreadyRunning() {
        var recovery = CredentialStartRecovery()

        #expect(recovery.prepare(for: .missing) == .watch)
        #expect(
            recovery.credentialChanged(to: .ready, health: .running(pid: 123))
                == .stopWatching)
        #expect(recovery.credentialChanged(to: .ready, health: .notStarted) == .none)
    }

    @Test func aMissingNamedVariableStopsAtPreflight() throws {
        let fixture = try TelegramCredentialFixture()

        let state = ShellModel.preflight(credentials: fixture.credentials)

        #expect(state == .missing)
    }

}

@MainActor
private final class ShellHarness {
    let fixture: TelegramCredentialFixture
    let launcher: RecordingEngineLauncher
    let supervisor: EngineSupervisor
    let model: ShellModel

    init() throws {
        let fixture = try TelegramCredentialFixture()
        let launcher = RecordingEngineLauncher()
        let (supervisor, model) = makeShell(fixture: fixture, launcher: launcher)
        self.fixture = fixture
        self.launcher = launcher
        self.supervisor = supervisor
        self.model = model
    }
}

@MainActor
private func makeShell(
    fixture: TelegramCredentialFixture, launcher: any EngineLaunching,
    panel: ControlPanel? = nil,
    now: @escaping () -> TimeInterval = { ProcessInfo.processInfo.systemUptime },
    sleep: @escaping (Duration) async throws -> Void = { try await Task.sleep(for: $0) }
) -> (supervisor: EngineSupervisor, model: ShellModel) {
    let socketPath = fixture.directory.appendingPathComponent("engine.sock").path
    let supervisor = EngineSupervisor(
        launcher: launcher,
        socketPath: socketPath,
        resolveCommand: {
            EngineCommand(executable: "/usr/bin/true", arguments: [], source: .developerPath)
        })
    let model = ShellModel(
        location: EngineLocation(configPath: fixture.configPath, socketPath: socketPath),
        credentials: fixture.credentials,
        panel: panel ?? ControlPanel(client: UnreachableControlPlane()),
        supervisor: supervisor, now: now, sleep: sleep)
    return (supervisor, model)
}

@MainActor
private final class DesktopClock {
    var time: TimeInterval = 0
    private var continuation: CheckedContinuation<Void, Never>?
    var waiting: Bool { continuation != nil }
    func sleep(_ duration: Duration) async throws {
        await withCheckedContinuation { continuation = $0 }
    }
    func advance() {
        time += 1
        let held = continuation
        continuation = nil
        held?.resume()
    }
}

private final class DesktopControlPlane: ControlPlaneDialing, @unchecked Sendable {
    private let lock = NSLock()
    private var calls: [(Request, TimeInterval)] = []
    private var dutyOn = true
    private var callUp = false
    var requests: [String] { lock.withLock { calls.map { $0.0.action.rawValue } } }
    var briefTargets: [JSONValue] {
        lock.withLock {
            calls.filter { $0.0.action == .brief }.compactMap { $0.0.payload?["target"] }
        }
    }
    func times(for action: String) -> [TimeInterval] {
        lock.withLock { calls.filter { $0.0.action.rawValue == action }.map { $0.1 } }
    }
    var duty: Bool {
        get { lock.withLock { dutyOn } }
        set { lock.withLock { dutyOn = newValue } }
    }
    func ask(_ request: Request) async throws -> Reply {
        let up = lock.withLock {
            calls.append((request, ProcessInfo.processInfo.systemUptime))
            if request.action == .live { callUp.toggle() }
            return callUp
        }
        let document: [String: Any] = [
            "ok": true, "protocol": controlPlaneProtocolVersion,
            "action": request.action.rawValue,
            "data": [
                "switches": ["duty": duty, "message": true],
                "state": up ? "up" : "down", "call_id": up ? "a-call" : NSNull(),
            ],
        ]
        return try Reply.of(JSONSerialization.data(withJSONObject: document))
    }
}

private struct UnreachableControlPlane: ControlPlaneDialing {
    func ask(_ request: Request) async throws -> Reply {
        throw ControlPlaneFailure.engineUnreachable("not connected in this shell test")
    }
}

private struct SlowDesktopPlane: ControlPlaneDialing {
    let status = OneShot<Void>()
    let live = OneShot<Void>()
    func ask(_ request: Request) async throws -> Reply {
        if request.action == .status { await status.value() }
        if request.action == .live { await live.value() }
        return try Reply.of(
            JSONSerialization.data(withJSONObject: [
                "ok": true,
                "action": request.action.rawValue, "protocol": controlPlaneProtocolVersion,
                "data": [:],
            ]))
    }
}

private final class RecordingEngineLauncher: EngineLaunching, @unchecked Sendable {
    private let launches = LaunchCounter()
    private let stops = LaunchCounter()

    var launchCount: Int { launches.value }
    var stopCount: Int { stops.value }

    func launch(_ command: EngineCommand) throws -> EngineProcess {
        let attempt = launches.increment()
        return HeldEngineProcess(pid: Int32(attempt + 2000), stopRequests: stops)
    }

}

private final class CredentialRacingLauncher: EngineLaunching, @unchecked Sendable {
    private let fixture: TelegramCredentialFixture
    private let launches = LaunchCounter()

    var launchCount: Int { launches.value }

    init(fixture: TelegramCredentialFixture) {
        self.fixture = fixture
    }

    func launch(_ command: EngineCommand) throws -> EngineProcess {
        let attempt = launches.increment()
        if attempt == 1 {
            try fixture.writeEnvironment("not-an-assignment\n")
            let failure = TelegramCredentialPreflightFailure(fixture.credentials.load().state)
            try fixture.writeEnvironment("A_TELEGRAM_TOKEN=second-repair\n")
            throw failure
        }
        return HeldEngineProcess(pid: Int32(attempt + 3000))
    }

}

private final class NonCredentialFailingLauncher: EngineLaunching, @unchecked Sendable {
    private let launches = LaunchCounter()

    var launchCount: Int { launches.value }

    func launch(_ command: EngineCommand) throws -> EngineProcess {
        _ = launches.increment()
        throw NonCredentialLaunchFailure()
    }
}

private struct NonCredentialLaunchFailure: Error {}

private final class LaunchCounter: @unchecked Sendable {
    private let lock = NSLock()
    private var count = 0

    var value: Int { lock.withLock { count } }

    func increment() -> Int {
        lock.withLock {
            count += 1
            return count
        }
    }
}

private final class HeldEngineProcess: EngineProcess, @unchecked Sendable {
    let processIdentifier: Int32
    private let exit = OneShot<Int32>()
    private let stopRequests: LaunchCounter?

    init(pid: Int32, stopRequests: LaunchCounter? = nil) {
        processIdentifier = pid
        self.stopRequests = stopRequests
    }

    var hasExited: Bool { exit.isResolved }

    func waitForExit(
        deliveringStderr: @Sendable (Data) async -> Void
    ) async -> Int32 {
        await exit.value()
    }

    func requestStop() {
        _ = stopRequests?.increment()
        exit.resolve(-SIGTERM)
    }

    func forceStop() {
        requestStop()
    }
}
