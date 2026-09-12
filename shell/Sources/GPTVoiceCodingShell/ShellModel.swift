import AppKit
import Foundation
import Observation
import ShellCore

enum SettingsGroup: CaseIterable {
    case voice, agent, telegram, call, general, diagnostics
    var title: Copy {
        switch self {
        case .voice: return .voice
        case .agent: return .callAgent
        case .telegram: return .telegram
        case .call: return .callSettings
        case .general: return .general
        case .diagnostics: return .diagnostics
        }
    }
}

enum ShellPage: Equatable {
    case home
    case session(SessionAddress)
    case settings(SettingsGroup)
    case unreadableSettings
}

enum ShellConfirmation { case quit, newAgent }

/// What the views read: the child's health on one side, the control plane on the
/// other, and nothing that mixes them.
///
/// Process parenthood and the control plane answer different questions. A process
/// being alive says nothing about whether its seams are filled, and a `status`
/// reply says nothing about whether the thing that answered is this shell's
/// child. Both are shown; neither is derived from the other.
@MainActor
@Observable
final class ShellModel {
    private static let statusInterval: TimeInterval = 1
    private static let briefInterval: TimeInterval = 2
    private(set) var page: ShellPage?
    private(set) var windowRequest = 0
    var confirmation: ShellConfirmation?
    var cardActionsVisible = false
    let text = ShellText()
    private(set) var configuration: ShellConfiguration?
    private(set) var codexVersion: String?
    var cardVisible: Bool { panel.dutyOn }
    var windowOpen: Bool { page != nil }
    var telegramConnected: Bool {
        configuration?.telegramBound == true
            && panel.status?.switches.first { $0.name == "message" }?.on == true
    }
    private let now: () -> TimeInterval
    private let sleep: (Duration) async throws -> Void
    private var poller: Task<Void, Never>?
    private var readInFlight: Task<Void, Never>?
    private var lastStatusRead: TimeInterval?
    private var lastBriefRead: TimeInterval?
    private var lastSessionRead: TimeInterval?
    private(set) var health: EngineHealth = .notStarted
    private(set) var engineOutput: [String] = []
    private(set) var location: EngineLocation
    private(set) var locationFailure: String?
    /// What the launch reconcile said, when it did not go as asked. Shown, not
    /// acted on: the panel reports installation, it does not edit it.
    private(set) var installationFailure: String?
    /// Why the engine may be running on a `PATH` that finds no coding agent.
    ///
    /// It is the *last* spawn's outcome and not an accumulated grievance: set by
    /// a spawn whose login shell ran out of time, and cleared by the next spawn
    /// that ended any other way. It describes the child running now, so a stale
    /// warning over a child that was never the one it complained about is a state
    /// this cannot reach. Its own field beside the two above rather than an
    /// `EngineHealth` case: that type is process parenthood and nothing inferred
    /// about the engine, and an engine on the wrong `PATH` is a perfectly healthy
    /// process.
    private(set) var pathFailure: String?
    /// The shell's pre-spawn answer about its write-only Telegram credential.
    /// Kept apart from `EngineHealth`, which is process parenthood only.
    private(set) var credentialState: TelegramCredentials.State
    private(set) var credentialSaveFailure: String?

    let panel: ControlPanel
    let loginItem = LoginItem()

    private let supervisor: EngineSupervisor
    private let pathOutcomes: PathOutcomes
    private let credentials: TelegramCredentials
    private var credentialStartRecovery = CredentialStartRecovery()
    private var credentialFileObserver: CredentialFileObserver?
    /// Installation reconcile and initial preflight, in their required order.
    /// A token saved immediately after app launch waits for this rather than
    /// starting the engine ahead of installation.
    private var preparation: Task<Void, Never>?

    convenience init() {
        // The socket path is read from the same configuration the engine is
        // spawned with, never computed from the state path.
        var resolved = EngineLocation(
            configPath: EngineLocation.defaultConfigPath(),
            socketPath: EngineLocation.defaultSocketPath())
        var failure: String?
        do {
            resolved = try EngineLocation.resolve()
        } catch let problem as ConfigurationFailure {
            failure = problem.detail
        } catch {
            failure = "\(error)"
        }
        let credentials = TelegramCredentials(configPath: resolved.configPath)
        let panel = ControlPanel(client: UnixSocketControlPlane(path: resolved.socketPath))

        let configPath = resolved.configPath
        let resources = Bundle.main.resourceURL
        // Built here, before `self` exists to capture, so what the launcher
        // learns is left in a box this model reads afterwards rather than pushed
        // through a callback it cannot yet form.
        let outcomes = PathOutcomes()
        let supervisor = EngineSupervisor(
            launcher: ProcessLauncher(
                report: { outcomes.record($0) }, credentials: credentials),
            socketPath: resolved.socketPath,
            resolveCommand: {
                try EngineCommand.resolve(resources: resources, configPath: configPath)
            })
        self.init(
            location: resolved,
            locationFailure: failure,
            credentials: credentials,
            panel: panel,
            supervisor: supervisor,
            pathOutcomes: outcomes)

        preparation = Task { await self.begin() }
    }

    private init(
        location: EngineLocation,
        locationFailure: String?,
        credentials: TelegramCredentials,
        panel: ControlPanel,
        supervisor: EngineSupervisor,
        pathOutcomes: PathOutcomes,
        now: @escaping () -> TimeInterval = { ProcessInfo.processInfo.systemUptime },
        sleep: @escaping (Duration) async throws -> Void = { try await Task.sleep(for: $0) }
    ) {
        self.location = location
        self.locationFailure = locationFailure
        self.credentials = credentials
        credentialState = Self.preflight(credentials: credentials)
        credentialSaveFailure = nil
        self.panel = panel
        self.supervisor = supervisor
        self.pathOutcomes = pathOutcomes
        self.now = now
        self.sleep = sleep
        preparation = nil
    }

    /// The shell assembly seam. Production supplies the concrete process and
    /// socket adapters above; focused tests supply a supervised inert child.
    convenience init(
        location: EngineLocation,
        credentials: TelegramCredentials,
        panel: ControlPanel,
        supervisor: EngineSupervisor,
        now: @escaping () -> TimeInterval = { ProcessInfo.processInfo.systemUptime },
        sleep: @escaping (Duration) async throws -> Void = { try await Task.sleep(for: $0) }
    ) {
        self.init(
            location: location,
            locationFailure: nil,
            credentials: credentials,
            panel: panel,
            supervisor: supervisor,
            pathOutcomes: PathOutcomes(), now: now, sleep: sleep)
    }

    private func begin() async {
        await readConfiguration {
            try await ShellConfiguration.load(
                location: self.location, resources: Bundle.main.resourceURL)
        }
        guard !Task.isCancelled else { return }
        await reconcileInstallation()
        guard !Task.isCancelled else { return }
        await startEngineAfterInstallation()
    }

    func readConfiguration(_ read: () async throws -> ShellConfiguration) async {
        do {
            configuration = try await read()
            locationFailure = nil
        } catch {
            locationFailure = (error as? ConfigurationFailure)?.detail ?? "\(error)"
            if !stopping { open(.unreadableSettings) }
        }
    }

    /// Start immediately or hold one recovery watch, after Installation has run.
    /// Split at this lifecycle boundary so tests never reconcile the real machine.
    func startEngineAfterInstallation() async {
        await supervisor.observe { [weak self] health in
            Task { @MainActor in await self?.healthChanged(health) }
        }
        await applyCredentialAction(credentialStartRecovery.prepare(for: credentialState))
    }

    /// The credential gate used at assembly and tested without starting the app.
    static func preflight(credentials: TelegramCredentials) -> TelegramCredentials.State {
        credentials.load().state
    }

    /// First launch is the install (ADR 0012), and every launch after it is a
    /// reconcile that writes nothing when the machine already agrees.
    ///
    /// It runs **before** the engine so that a Session started right after the
    /// app opens finds the hook already there. It does not gate the engine: a
    /// reconcile that failed costs reach into Sessions, and refusing to start
    /// over it would cost the control plane and the Live Call too.
    private func reconcileInstallation() async {
        let resources = Bundle.main.resourceURL
        let command: EngineCommand
        do {
            command = try EngineCommand.resolveInstallation(
                resources: resources, verb: Installation.reconcileVerb)
        } catch let problem as EngineCommandFailure {
            installationFailure = problem.detail
            return
        } catch {
            installationFailure = "\(error)"
            return
        }

        // Not `Task.detached`: the runner waits on a subprocess, and a detached
        // task doing that holds a cooperative-pool thread — one of about as many
        // as this machine has cores — for the whole run. The runner owns its own
        // threads and hands this one back; see its note.
        //
        // It reports into the *same* `PathOutcomes` box the launcher writes to,
        // so the panel shows one answer about this machine's `PATH`. The
        // reconcile needs that reading for a reason the engine does not: it has
        // to find the user's own codex, and it cannot do that on the `PATH`
        // launchd hands an app opened from Finder (#272, ADR 0022).
        let outcomes = pathOutcomes
        installationFailure = await InstallationRunner(
            report: { outcomes.record($0) }
        ).run(command).failure
    }

    private func healthChanged(_ health: EngineHealth) async {
        self.health = health
        await readWhatTheLauncherLearned()
        await applyCredentialAction(
            credentialStartRecovery.engineChanged(
                to: health, credentialState: credentialState))
        if case .running = health {
            await panel.refresh()
            synchronizePolling()
        }
    }

    /// What the last spawn left behind: the engine's own words, and what asking
    /// the login shell came to.
    ///
    /// Read rather than pushed, and read at exactly the two moments the panel is
    /// about to be looked at — a health change, and each visible desktop pass.
    /// That is already how `engineOutput` reaches this model, and one
    /// mechanism read twice is easier to be right about than two.
    private func readWhatTheLauncherLearned() async {
        engineOutput = await supervisor.lines()
        pathFailure = pathOutcomes.latest?.reason
        credentialState = Self.preflight(credentials: credentials)
    }

    private func credentialSourceChanged() async {
        let state = Self.preflight(credentials: credentials)
        credentialState = state
        await applyCredentialAction(
            credentialStartRecovery.credentialChanged(to: state, health: health))
    }

    private func applyCredentialAction(_ action: CredentialStartRecovery.Action) async {
        switch action {
        case .none:
            return
        case .start:
            await supervisor.start()
        case .stopWatching:
            stopCredentialObservation()
        case .watch:
            do {
                credentialFileObserver = try CredentialFileObserver(
                    path: credentials.environmentPath,
                    changed: { [weak self] in
                        Task { @MainActor in await self?.credentialSourceChanged() }
                    })
            } catch {
                return
            }
            // Close the gap between the preflight read and arming the observer.
            await credentialSourceChanged()
        }
    }

    private func stopCredentialObservation() {
        credentialFileObserver?.cancel()
        credentialFileObserver = nil
    }

    func open(_ page: ShellPage) {
        panel.setPointerInRoster(false)
        if self.page != page {
            panel.clearSession()
            lastSessionRead = nil
        }
        self.page = page
        windowRequest += 1
        cardActionsVisible = false
        synchronizePolling()
        if page == .settings(.diagnostics) {
            Task {
                let report = await InstallationRunner().run(.codexVersion)
                codexVersion = report.ok ? report.lines.first : nil
            }
        }
    }

    func closeWindow() {
        page = nil
        panel.setPointerInRoster(false)
        synchronizePolling()
    }

    func setDuty(_ on: Bool) async {
        await panel.flip("duty", on: on)
        synchronizePolling()
    }

    private func synchronizePolling() {
        guard !stopping, windowOpen || cardVisible else {
            poller?.cancel()
            poller = nil
            readInFlight?.cancel()
            readInFlight = nil
            lastStatusRead = nil
            lastBriefRead = nil
            return
        }
        guard poller == nil else { return }
        poller = Task { [weak self] in
            while !Task.isCancelled {
                guard let self else { return }
                let started = self.now()
                self.panel.tick()
                if self.readInFlight == nil {
                    self.readInFlight = Task {
                        await self.readVisibleSurfaces(at: started)
                        guard !Task.isCancelled else { return }
                        self.readInFlight = nil
                        self.synchronizePolling()
                    }
                }
                try? await self.sleep(.seconds(max(0, started + Self.statusInterval - self.now())))
            }
        }
    }

    /// One visibility-aware pass. The two surfaces never own timers.
    private func readVisibleSurfaces(at time: TimeInterval) async {
        await withTaskGroup(of: Void.self) { group in
            if lastStatusRead.map({
                time - $0 >= (windowOpen ? Self.statusInterval : Self.briefInterval)
            }) ?? true {
                lastStatusRead = time
                group.addTask { await self.panel.refresh() }
            }
            if lastBriefRead.map({ time - $0 >= Self.briefInterval }) ?? true {
                lastBriefRead = time
                group.addTask { await self.panel.refreshRoster() }
            }
            if case .session(let target) = page,
                lastSessionRead.map({ time - $0 >= Self.briefInterval }) ?? true
            {
                lastSessionRead = time
                group.addTask { await self.panel.refreshSession(target) }
            }
        }
        guard !Task.isCancelled else { return }
        await readWhatTheLauncherLearned()
        await applyCredentialAction(
            credentialStartRecovery.credentialChanged(to: credentialState, health: health))
    }

    func retryEngine() async {
        await supervisor.retry()
    }

    func clearCredentialSaveFailure() {
        credentialSaveFailure = nil
    }

    /// Replace the write-only token, then retry or replace the child that
    /// inherited the old environment. The supervisor orders a running child's
    /// exit before its successor, so two engines never overlap on the one
    /// Telegram `getUpdates` consumer.
    func saveTelegramToken(_ token: String) async -> Bool {
        await preparation?.value
        do {
            let reading = try credentials.save(token: token)
            credentialState = reading.state
            credentialSaveFailure = nil
            credentialStartRecovery.cancel()
            stopCredentialObservation()
        } catch let failure as TelegramCredentialSaveFailure {
            credentialSaveFailure = failure.detail
            return false
        } catch {
            credentialSaveFailure = "Telegram credentials could not be saved: \(error)"
            return false
        }
        await supervisor.retry()
        return true
    }

    /// Whether the child has already been asked to stop, so the terminate hook
    /// does not ask twice.
    private(set) var stopping = false

    /// Stop the engine in order. `SIGTERM` leaves no socket debris for the next
    /// start to trip over.
    func stopEngine() async {
        stopping = true
        synchronizePolling()
        preparation?.cancel()
        credentialStartRecovery.cancel()
        stopCredentialObservation()
        await supervisor.shutDown()
    }

    /// Quit. The engine is stopped on the way out by the terminate hook, which
    /// is also what catches a quit that did not come from this menu.
    func quit() {
        if panel.phase == .onCall {
            if !cardVisible && !windowOpen { open(.home) }
            confirmation = .quit
        } else {
            NSApplication.shared.terminate(nil)
        }
    }

    func requestNewAgent() async {
        if panel.phase == .onCall { confirmation = .newAgent } else { await panel.newCallAgent() }
    }

    func resolveConfirmation(accept: Bool) async {
        let pending = confirmation
        confirmation = nil
        guard accept else { return }
        switch pending {
        case .quit:
            await stopEngine()
            NSApplication.shared.terminate(nil)
        case .newAgent: await panel.newCallAgent()
        case nil: break
        }
    }

}

/// The launcher's last word on the `PATH`, carried from whatever thread spawned
/// to this `@MainActor` model.
///
/// A box rather than a callback for two reasons: the launcher is built inside
/// `ShellModel.init`, before there is a `self` to hop back to, and the model
/// already reads the engine's stderr off the supervisor this way. Last one wins
/// and nothing accumulates — this answers "what is the child running now on",
/// which has exactly one answer.
private final class PathOutcomes: @unchecked Sendable {
    private let lock = NSLock()
    private var last: LoginShellPath.Outcome?

    func record(_ outcome: LoginShellPath.Outcome) { lock.withLock { last = outcome } }
    var latest: LoginShellPath.Outcome? { lock.withLock { last } }
}
