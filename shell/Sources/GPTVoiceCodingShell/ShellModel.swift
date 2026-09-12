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
    case onboarding(OnboardingStep)
    case codexCheck
}

enum OnboardingStep: Int, CaseIterable {
    case welcome = 1
    case codex, installation, agent, telegram, testCall
}

enum CodexCheck { case checking, ready, notInstalled, notLoggedIn }

enum ShellConfirmation { case quit, newAgent }
enum TelegramStage { case idle, entering, validating, waiting, confirmed }

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
    let text: ShellText
    private let preferences: UserDefaults
    private(set) var selectedLanguage: ShellLanguage
    private(set) var configuration: ShellConfiguration?
    private(set) var pendingRestart = false
    private(set) var savingSettings = false
    private(set) var settingsFailure: String?
    private var settingsSavedPID: Int32?
    private let configurationFile: ShellConfigurationFile
    var telegramToken = ""
    private(set) var telegramStage: TelegramStage = .idle
    private(set) var telegramBinding: TelegramBindingReading?
    private(set) var telegramFailure: ActionFailure?
    private var pendingTelegramToken: String?
    private var telegramRequest: Task<TelegramBindingReading?, Never>?
    private var telegramCancellation: Task<Void, Never>?
    private let runCommand: @Sendable (EngineCommand) async -> InstallationReport
    private(set) var codexCheck: CodexCheck = .checking
    private(set) var codexCheckFailure: String?
    private(set) var installationReport: InstallationReport?
    private(set) var onboardingBusy = false
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
    private(set) var readInFlight: Task<Void, Never>?
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

    let panel: ControlPanel
    let loginItem: LoginItem

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
        configurationFile: ShellConfigurationFile? = nil,
        loginItem: LoginItem? = nil,
        preferences: UserDefaults = .standard,
        runCommand: (@Sendable (EngineCommand) async -> InstallationReport)? = nil,
        now: @escaping () -> TimeInterval = { ProcessInfo.processInfo.systemUptime },
        sleep: @escaping (Duration) async throws -> Void = { try await Task.sleep(for: $0) }
    ) {
        self.location = location
        self.loginItem = loginItem ?? LoginItem()
        self.preferences = preferences
        let language = ShellText.preferredLanguage(
            saved: preferences.string(forKey: ShellText.preferenceKey),
            system: Locale.preferredLanguages)
        selectedLanguage = ShellLanguage(rawValue: language)!
        text = ShellText(language: language)
        self.configurationFile =
            configurationFile
            ?? ShellConfigurationFile(location: location) {
                try await ShellConfiguration.load(location: $0, resources: Bundle.main.resourceURL)
            }
        self.locationFailure = locationFailure
        self.credentials = credentials
        credentialState = Self.preflight(credentials: credentials)
        self.panel = panel
        self.supervisor = supervisor
        self.pathOutcomes = pathOutcomes
        self.runCommand =
            runCommand ?? { command in
                await InstallationRunner(report: { pathOutcomes.record($0) }).run(command)
            }
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
        configurationFile: ShellConfigurationFile? = nil,
        loginItem: LoginItem? = nil,
        preferences: UserDefaults = .standard,
        runCommand: (@Sendable (EngineCommand) async -> InstallationReport)? = nil,
        now: @escaping () -> TimeInterval = { ProcessInfo.processInfo.systemUptime },
        sleep: @escaping (Duration) async throws -> Void = { try await Task.sleep(for: $0) }
    ) {
        self.init(
            location: location,
            locationFailure: nil,
            credentials: credentials,
            panel: panel,
            supervisor: supervisor,
            pathOutcomes: PathOutcomes(), configurationFile: configurationFile,
            loginItem: loginItem,
            preferences: preferences, runCommand: runCommand, now: now, sleep: sleep)
    }

    func begin(template: URL? = nil, delegateCLI: URL? = nil) async {
        var firstLaunch = false
        if !FileManager.default.fileExists(atPath: location.configPath) {
            do {
                guard
                    let template = template
                        ?? Bundle.main.url(forResource: "config.example", withExtension: "toml")
                else {
                    throw ConfigurationFailure.unreadable(
                        "the bundle's configuration template is missing")
                }
                let cli =
                    try delegateCLI
                    ?? URL(
                        fileURLWithPath: EngineCommand.resolve(
                            resources: Bundle.main.resourceURL, configPath: location.configPath
                        ).executable
                    )
                    .deletingLastPathComponent().appendingPathComponent(BundleLayout.engineCLIName)
                firstLaunch = try configurationFile.createIfMissing(
                    template: template, delegateCLI: cli)
            } catch {
                locationFailure = "\(error)"
                open(.unreadableSettings)
                return
            }
        }
        await readConfiguration { try await configurationFile.load() }
        guard !Task.isCancelled else { return }
        if firstLaunch {
            if locationFailure == nil { open(.onboarding(.welcome)) }
            return
        }
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
        installationReport = await runCommand(command)
        installationFailure = installationReport?.failure
    }

    func checkCodex() async {
        codexCheck = .checking
        let version = await runCommand(.codexVersion)
        guard !Task.isCancelled else { return }
        codexVersion = version.ok ? version.lines.first : nil
        guard version.ok else {
            codexCheck = .notInstalled
            codexCheckFailure = version.failure
            return
        }
        let login = await runCommand(.codexLoginStatus)
        guard !Task.isCancelled else { return }
        codexCheck = login.ok ? .ready : .notLoggedIn
        codexCheckFailure = login.failure
    }

    func advanceOnboarding() async {
        guard case .onboarding(let step) = page, !onboardingBusy else { return }
        switch step {
        case .welcome: open(.onboarding(.codex))
        case .codex:
            onboardingBusy = true
            open(.onboarding(.installation))
            await reconcileInstallation()
            if !Task.isCancelled && !stopping { await startEngineAfterInstallation() }
            onboardingBusy = false
        case .installation: open(.onboarding(.agent))
        case .agent: open(.onboarding(.telegram))
        case .telegram: open(.onboarding(.testCall))
        case .testCall: open(.home)
        }
    }

    private func healthChanged(_ health: EngineHealth) async {
        self.health = health
        await readWhatTheLauncherLearned()
        await applyCredentialAction(
            credentialStartRecovery.engineChanged(
                to: health, credentialState: credentialState))
        if case .running = health {
            await refreshStatus()
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
        if self.page == .settings(.telegram) || self.page == .onboarding(.telegram),
            page != self.page
        {
            cancelTelegramBinding()
        }
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
                let report = await runCommand(.codexVersion)
                codexVersion = report.ok ? report.lines.first : nil
            }
        }
    }

    func closeWindow() {
        cancelTelegramBinding()
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
        if page == .onboarding(.welcome) || page == .onboarding(.codex) { return }
        await withTaskGroup(of: Void.self) { group in
            if lastStatusRead.map({
                time - $0 >= (windowOpen ? Self.statusInterval : Self.briefInterval)
            }) ?? true {
                lastStatusRead = time
                group.addTask { await self.refreshStatus() }
            }
            if lastBriefRead.map({ time - $0 >= Self.briefInterval }) ?? true {
                lastBriefRead = time
                group.addTask { await self.panel.refreshRoster() }
                group.addTask { await self.refreshTelegramBinding() }
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

    var restartBlockedByCall: Bool { panel.phase.resolving || panel.phase == .onCall }

    var autoHangup: Bool? { panel.status?.switches.first { $0.name == "auto_hangup" }?.on }

    func goBack() { open(page == .codexCheck ? .settings(.diagnostics) : .home) }

    func setLanguage(_ language: ShellLanguage) {
        selectedLanguage = language
        preferences.set(language.rawValue, forKey: ShellText.preferenceKey)
    }

    func saveSetting(_ setting: EngineSetting, value: JSONValue) async {
        _ = await writeSettings { try await configurationFile.save(setting, value: value) }
    }

    private func writeSettings(_ write: () async throws -> ShellConfiguration) async -> Bool {
        guard !savingSettings else { return false }
        savingSettings = true
        defer { savingSettings = false }
        do {
            configuration = try await write()
            settingsFailure = nil
            pendingRestart = true
            if case .running(let pid) = health {
                settingsSavedPID = pid
            } else {
                settingsSavedPID = nil
            }
            return true
        } catch {
            settingsFailure = "\(error)"
            return false
        }
    }

    func selectAgentModel(_ name: String) async {
        guard panel.engineReachable, !savingSettings,
            let model = panel.models.first(where: { $0.model == name })
        else { return }
        let effort = configuration?.effort.flatMap { model.efforts.contains($0) ? $0 : nil }
        _ = await writeSettings { try await configurationFile.saveAgentModel(name, effort: effort) }
    }

    var agentEfforts: [String] {
        panel.models.first { $0.model == configuration?.model }?.efforts ?? []
    }

    var agentModelUnavailable: Bool {
        guard let name = configuration?.model, !panel.models.isEmpty else { return false }
        return !panel.models.contains { $0.model == name }
    }

    var canValidateTelegram: Bool {
        panel.engineReachable && telegramRequest == nil && telegramCancellation == nil
            && !telegramToken.isEmpty
    }

    func validateTelegram() async {
        guard canValidateTelegram else { return }
        pendingTelegramToken = telegramToken
        telegramToken = ""
        telegramStage = .validating
        telegramFailure = nil
        await requestTelegramBinding(["token": .string(pendingTelegramToken!)])
    }

    func refreshTelegramBinding() async {
        guard telegramStage == .waiting, telegramRequest == nil, telegramCancellation == nil else {
            return
        }
        await requestTelegramBinding([:])
    }

    private func requestTelegramBinding(_ payload: [String: JSONValue]) async {
        let request = Task { await panel.bindTelegram(payload) }
        telegramRequest = request
        let reading = await request.value
        guard !request.isCancelled else { return }
        telegramRequest = nil
        telegramFailure = panel.lastFailure
        telegramBinding = reading
        telegramStage = reading.map { $0.chatID == nil ? .waiting : .confirmed } ?? .entering
        if reading == nil { pendingTelegramToken = nil }
    }

    func cancelTelegramBinding() {
        let needsCancel = telegramStage == .validating || telegramStage == .waiting
        telegramToken = ""
        pendingTelegramToken = nil
        telegramBinding = nil
        telegramStage = .idle
        telegramFailure = nil
        guard needsCancel, telegramCancellation == nil else { return }
        let request = telegramRequest
        request?.cancel()
        telegramCancellation = Task {
            _ = await request?.value
            _ = await panel.bindTelegram(["cancel": .bool(true)])
            telegramRequest = nil
            telegramCancellation = nil
        }
    }

    func changeTelegram() { telegramStage = .entering }

    var telegramFailureCopy: Copy? {
        guard let telegramFailure else { return nil }
        if case .refused(let refusal) = telegramFailure {
            switch refusal.code {
            case .other("telegram_credentials"): return .telegramInvalidToken
            case .other("telegram_destination"): return .telegramDestinationFailed
            default: break
            }
        }
        return .telegramConnectionFailed
    }

    func saveTelegramBinding() async {
        guard telegramStage == .confirmed, let binding = telegramBinding,
            let token = pendingTelegramToken, !savingSettings
        else { return }
        if await writeSettings({ try await configurationFile.bindTelegram(binding, token: token) })
        {
            cancelTelegramBinding()
        }
    }

    func unbindTelegram() async {
        _ = await writeSettings { try await configurationFile.unbindTelegram() }
    }

    func restartForSettings() async {
        guard pendingRestart, !savingSettings, !restartBlockedByCall else { return }
        await supervisor.retry()
    }

    private func refreshStatus() async {
        let queriedHealth = health
        await panel.refresh()
        if !Task.isCancelled, pendingRestart, panel.engineReachable,
            case .running(let pid) = queriedHealth, health == queriedHealth, pid != settingsSavedPID
        {
            pendingRestart = false
        }
    }

    /// Whether the child has already been asked to stop, so the terminate hook
    /// does not ask twice.
    private(set) var stopping = false

    /// Stop the engine in order. `SIGTERM` leaves no socket debris for the next
    /// start to trip over.
    func stopEngine() async {
        stopping = true
        cancelTelegramBinding()
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
