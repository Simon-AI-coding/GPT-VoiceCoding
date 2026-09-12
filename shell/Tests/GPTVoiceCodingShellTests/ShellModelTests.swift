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
    @Test func anUnknownAutoHangupIsNotOffAndDiagnosticsOwnsTheCheckReturn() async throws {
        let fixture = try TelegramCredentialFixture()
        let (_, model) = makeShell(
            fixture: fixture, launcher: RecordingEngineLauncher(),
            runCommand: { _ in InstallationReport(ok: true, lines: []) })
        #expect(model.autoHangup == nil)
        model.open(.codexCheck)
        model.goBack()
        #expect(model.page == .settings(.diagnostics))
        model.goBack()
        #expect(model.page == .home)
        await model.stopEngine()
    }

    @Test func installationItemsReportTheirOwnOutcomeInsteadOfTheProcessExit() {
        let report = InstallationReport(
            ok: false,
            lines: ["claude-hooks: current (changed)", "codex-launch-agent: FAILED — test failure"])
        #expect(report.state(of: .claudeHooks) == .current)
        #expect(report.state(of: .codexServer) == .failed)
    }

    @Test func aSettingsRestartWaitsAcrossAllThreeActiveCallPhases() async throws {
        let fixture = try TelegramCredentialFixture()
        try fixture.writeEnvironment("A_TELEGRAM_TOKEN=ready\n")
        let engine = GatedCallPlane()
        let launcher = RecordingEngineLauncher()
        let location = EngineLocation(
            configPath: fixture.configPath,
            socketPath: fixture.directory.appendingPathComponent("engine.sock").path)
        let file = ShellConfigurationFile(location: location) { _ in
            ShellConfiguration(["voice": .string("maple")])
        }
        let (_, model) = makeShell(
            fixture: fixture, launcher: launcher, panel: ControlPanel(client: engine),
            configurationFile: file)
        await model.startEngineAfterInstallation()
        #expect(await waitUntil { model.panel.engineReachable })
        let dial = Task { await model.panel.toggleLive() }
        #expect(await waitUntil { model.panel.phase == .calling })
        await model.saveSetting(.voice, value: .string("maple"))
        for phase in [CallPhase.calling, .onCall, .ending] {
            #expect(model.panel.phase == phase)
            #expect(model.restartBlockedByCall)
            await model.restartForSettings()
            #expect(model.pendingRestart)
            #expect(launcher.stopCount == 0)
            if phase == .calling {
                engine.dial.resolve()
                await dial.value
            }
            if phase == .onCall {
                Task { await model.panel.toggleLive() }
                #expect(await waitUntil { model.panel.phase == .ending })
            }
        }
        engine.hangup.resolve()
        #expect(await waitUntil { model.panel.phase == .ready })
        await model.restartForSettings()
        #expect(await waitUntil { model.health == .running(pid: 2002) && !model.pendingRestart })
        #expect(model.configuration?.voice == "maple")
        await model.stopEngine()
    }

    @Test func runtimeSwitchesNeverWriteConfigurationOrRaiseTheRestartLine() async throws {
        let fixture = try TelegramCredentialFixture()
        let original = try Data(contentsOf: URL(fileURLWithPath: fixture.configPath))
        let (_, model) = makeShell(
            fixture: fixture, launcher: RecordingEngineLauncher(),
            panel: ControlPanel(client: DesktopControlPlane()))
        for name in SwitchReading.canonicalOrder { await model.panel.flip(name, on: false) }
        #expect(!model.pendingRestart)
        #expect(try Data(contentsOf: URL(fileURLWithPath: fixture.configPath)) == original)
        await model.stopEngine()
    }

    @Test func anExistingConfigurationIsNotRewrittenAndLaunchStaysSilent() async throws {
        let fixture = try TelegramCredentialFixture()
        try fixture.writeEnvironment("A_TELEGRAM_TOKEN=ready\n")
        let original = try Data(contentsOf: URL(fileURLWithPath: fixture.configPath))
        let location = EngineLocation(
            configPath: fixture.configPath,
            socketPath: fixture.directory.appendingPathComponent("engine.sock").path)
        let file = ShellConfigurationFile(location: location) { _ in ShellConfiguration([:]) }
        let launcher = RecordingEngineLauncher()
        let (_, model) = makeShell(
            fixture: fixture, launcher: launcher, configurationFile: file,
            runCommand: { _ in InstallationReport(ok: true, lines: []) })
        await model.begin()
        #expect(model.page == nil)
        #expect(launcher.launchCount == 1)
        #expect(try Data(contentsOf: URL(fileURLWithPath: fixture.configPath)) == original)
        #expect(!model.pendingRestart)
        await model.stopEngine()
    }

    @Test(arguments: [false, true])
    func theCodexCheckSeparatesMissingFromNotLoggedIn(installed: Bool) async throws {
        let fixture = try TelegramCredentialFixture()
        let (_, model) = makeShell(
            fixture: fixture, launcher: RecordingEngineLauncher(),
            runCommand: { command in
                InstallationReport(ok: command == .codexVersion && installed, lines: ["test check"])
            })
        await model.checkCodex()
        #expect(model.codexCheck == (installed ? .notLoggedIn : .notInstalled))
        #expect(model.codexCheckFailure != nil)
        await model.stopEngine()
    }

    @Test func absentAndUnavailableAgentModelsAreNeverChangedByReadingOrAnOfflinePick() async throws
    {
        let fixture = try TelegramCredentialFixture()
        let original = try Data(contentsOf: URL(fileURLWithPath: fixture.configPath))
        let (_, model) = makeShell(
            fixture: fixture, launcher: RecordingEngineLauncher(),
            panel: ControlPanel(client: DesktopControlPlane()))
        await model.readConfiguration { ShellConfiguration([:]) }
        #expect(model.configuration?.model == nil)
        await model.selectAgentModel("new-model")
        #expect(!model.pendingRestart)
        await model.panel.refresh()
        await model.panel.refreshModels()
        #expect(model.configuration?.model == nil)
        await model.readConfiguration {
            ShellConfiguration(["model": .string("retired-model"), "effort": .string("low")])
        }
        #expect(model.agentModelUnavailable)
        #expect(model.configuration?.model == "retired-model")
        #expect(try Data(contentsOf: URL(fileURLWithPath: fixture.configPath)) == original)
        #expect(!model.pendingRestart)
        await model.stopEngine()
    }

    @Test func aRejectedSettingLeavesTheFileAndRestartStateUntouched() async throws {
        let fixture = try TelegramCredentialFixture()
        let original = try Data(contentsOf: URL(fileURLWithPath: fixture.configPath))
        let location = EngineLocation(
            configPath: fixture.configPath,
            socketPath: fixture.directory.appendingPathComponent("engine.sock").path)
        let file = ShellConfigurationFile(location: location) { _ in
            throw ConfigurationFailure.unreadable("test invalid value")
        }
        let (_, model) = makeShell(
            fixture: fixture, launcher: RecordingEngineLauncher(), configurationFile: file)
        await model.saveSetting(.silence, value: .number(-1))
        #expect(model.settingsFailure != nil)
        #expect(!model.pendingRestart)
        #expect(try Data(contentsOf: URL(fileURLWithPath: fixture.configPath)) == original)
        await model.stopEngine()
    }

    @Test func anAbsentConfigurationStartsTheSixStepFlowAndNeverAsksForVerify() async throws {
        let fixture = try TelegramCredentialFixture()
        try FileManager.default.removeItem(atPath: fixture.configPath)
        let location = EngineLocation(
            configPath: fixture.configPath,
            socketPath: fixture.directory.appendingPathComponent("engine.sock").path)
        let file = ShellConfigurationFile(location: location) { _ in
            ShellConfiguration(["model": .string("gpt-5.6-terra"), "effort": .string("low")])
        }
        let launcher = RecordingEngineLauncher()
        let engine = DesktopControlPlane()
        let (_, model) = makeShell(
            fixture: fixture, launcher: launcher, panel: ControlPanel(client: engine),
            configurationFile: file,
            runCommand: { _ in
                InstallationReport(
                    ok: true, lines: ["claude-hooks: current", "codex-launch-agent: current"])
            })
        let template = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("app_bundle/config.example.toml")
        let cli = fixture.directory.appendingPathComponent("engine/bin/bridgectl")
        await model.begin(template: template, delegateCLI: cli)
        #expect(model.page == .onboarding(.welcome))
        #expect(launcher.launchCount == 0)
        let generated = try String(contentsOfFile: fixture.configPath, encoding: .utf8)
        #expect(generated.contains("model = \"gpt-5.6-terra\""))
        #expect(generated.contains("effort = \"low\""))
        #expect(
            generated.contains(
                "companion_channel = \"gpt_voicecoding.adapters.companion_channel:null_channel\""))
        #expect(generated.contains(cli.path))
        #expect(!generated.contains("executable ="))
        #expect(!model.pendingRestart)
        await model.advanceOnboarding()
        #expect(model.page == .onboarding(.codex))
        await model.checkCodex()
        #expect(model.codexCheck == .ready)
        await model.advanceOnboarding()
        #expect(model.page == .onboarding(.installation))
        #expect(launcher.launchCount == 1)
        #expect(model.installationReport?.ok == true)
        for step in [OnboardingStep.agent, .telegram, .testCall] {
            await model.advanceOnboarding()
            #expect(model.page == .onboarding(step))
        }
        await model.panel.toggleLive()
        #expect(model.panel.phase == .onCall)
        await model.panel.toggleLive()
        await model.advanceOnboarding()
        #expect(model.page == .home)
        #expect(!engine.requests.contains("verify"))
        await model.stopEngine()
    }

    @Test(arguments: ["low", "high"])
    func changingAgentModelKeepsOnlyAnAllowedEffortThenWaitsForRestart(effort: String) async throws
    {
        let fixture = try TelegramCredentialFixture()
        try fixture.writeEnvironment("A_TELEGRAM_TOKEN=ready\n")
        var original = try String(contentsOfFile: fixture.configPath, encoding: .utf8)
        original +=
            "\n[delegate]\nmodel = \"old-model\" # my choice\neffort = \"\(effort)\" # thinking\nunknown = true\n"
        try Data(original.utf8).write(to: URL(fileURLWithPath: fixture.configPath))
        let location = EngineLocation(
            configPath: fixture.configPath,
            socketPath: fixture.directory.appendingPathComponent("engine.sock").path)
        let file = ShellConfigurationFile(location: location) { location in
            let text = try String(contentsOfFile: location.configPath, encoding: .utf8)
            var fields: [String: JSONValue] = [:]
            for key in ["model", "effort"] {
                if let value = try MinimalTOML.string(forKey: key, inTable: "delegate", of: text) {
                    fields[key] = .string(value)
                }
            }
            return ShellConfiguration(fields)
        }
        let launcher = RecordingEngineLauncher()
        let (_, model) = makeShell(
            fixture: fixture, launcher: launcher,
            panel: ControlPanel(client: DesktopControlPlane()), configurationFile: file)
        await model.readConfiguration { try await file.load() }
        await model.startEngineAfterInstallation()
        #expect(await waitUntil { model.panel.engineReachable })
        await model.panel.refreshModels()
        await model.selectAgentModel("new-model")
        #expect(model.configuration?.model == "new-model")
        #expect(model.configuration?.effort == (effort == "high" ? "high" : nil))
        let saved = try String(contentsOfFile: fixture.configPath, encoding: .utf8)
        #expect(saved.contains("model = \"new-model\" # my choice"))
        #expect(saved.contains("# thinking\nunknown = true"))
        #expect(model.pendingRestart)
        #expect(launcher.launchCount == 1)
        await model.restartForSettings()
        #expect(await waitUntil { model.health == .running(pid: 2002) && !model.pendingRestart })
        await model.stopEngine()
    }

    @Test func leavingAnInflightTelegramValidationCancelsItBeforeAnotherCanBegin() async throws {
        let fixture = try TelegramCredentialFixture()
        let engine = HeldTelegramPlane()
        let (_, model) = makeShell(
            fixture: fixture, launcher: RecordingEngineLauncher(),
            panel: ControlPanel(client: engine))
        await model.panel.refresh()
        model.open(.settings(.telegram))
        model.telegramToken = "not-a-real-token"
        let validation = Task { await model.validateTelegram() }
        #expect(await waitUntil { engine.tokens.value == 1 })
        model.closeWindow()
        model.open(.settings(.telegram))
        model.telegramToken = "another-test-token"
        #expect(!model.canValidateTelegram)
        engine.reply.resolve()
        await validation.value
        #expect(await waitUntil { engine.cancellations.value == 1 && model.canValidateTelegram })
        #expect(model.telegramStage == .idle)
        #expect(model.telegramBinding == nil)
        #expect(!model.pendingRestart)
        #expect(!FileManager.default.fileExists(atPath: fixture.environmentPath))
        await model.stopEngine()
    }

    @Test(arguments: [
        "telegram_credentials", "telegram_network", "telegram_destination", "telegram_api",
    ])
    func aRefusedTelegramBindingDoesNotSaveOrRetainTheToken(code: String) async throws {
        let fixture = try TelegramCredentialFixture()
        let (_, model) = makeShell(
            fixture: fixture, launcher: RecordingEngineLauncher(),
            panel: ControlPanel(client: RefusingTelegramPlane(code: code)))
        await model.panel.refresh()
        model.telegramToken = "not-a-real-token"
        await model.validateTelegram()
        #expect(model.telegramStage == .entering)
        #expect(model.telegramFailure != nil)
        #expect(model.telegramToken.isEmpty)
        #expect(model.telegramBinding == nil)
        await model.saveTelegramBinding()
        #expect(!model.pendingRestart)
        #expect(!FileManager.default.fileExists(atPath: fixture.environmentPath))
        await model.stopEngine()
    }

    @Test func unbindingRemovesTheCredentialAndTelegramTablesWithoutStartingTheEngine() async throws
    {
        let fixture = try TelegramCredentialFixture()
        try fixture.writeEnvironment("A_TELEGRAM_TOKEN=before\n")
        let location = EngineLocation(
            configPath: fixture.configPath,
            socketPath: fixture.directory.appendingPathComponent("engine.sock").path)
        let file = ShellConfigurationFile(location: location) { _ in ShellConfiguration([:]) }
        let launcher = RecordingEngineLauncher()
        let (_, model) = makeShell(fixture: fixture, launcher: launcher, configurationFile: file)
        await model.unbindTelegram()
        let saved = try String(contentsOfFile: fixture.configPath, encoding: .utf8)
        #expect(
            saved.contains(
                "companion_channel = \"gpt_voicecoding.adapters.companion_channel:null_channel\""))
        #expect(!saved.contains("[adapters.settings.companion_channel]"))
        #expect(!FileManager.default.fileExists(atPath: fixture.environmentPath))
        #expect(model.pendingRestart)
        #expect(launcher.launchCount == 0)
        await model.stopEngine()
    }

    @Test func telegramBindingSavesOnlyAfterConfirmationThenWaitsForRestart() async throws {
        let fixture = try TelegramCredentialFixture()
        try fixture.writeEnvironment("A_TELEGRAM_TOKEN=before\n")
        let location = EngineLocation(
            configPath: fixture.configPath,
            socketPath: fixture.directory.appendingPathComponent("engine.sock").path)
        let file = ShellConfigurationFile(location: location) { location in
            let content = try String(contentsOfFile: location.configPath, encoding: .utf8)
            return ShellConfiguration([
                "telegram_bound": .bool(content.contains("[shell.telegram]")),
                "telegram_name": .string("My bot"),
            ])
        }
        let engine = DesktopControlPlane()
        let launcher = RecordingEngineLauncher()
        let (_, model) = makeShell(
            fixture: fixture, launcher: launcher, panel: ControlPanel(client: engine),
            configurationFile: file)
        await model.startEngineAfterInstallation()
        #expect(await waitUntil { model.panel.engineReachable })
        model.telegramToken = "a-test-token"
        await model.validateTelegram()
        #expect(model.telegramStage == .waiting)
        #expect(model.telegramBinding?.botName == "My bot")
        #expect(model.telegramToken.isEmpty)
        #expect(!model.pendingRestart)
        await model.refreshTelegramBinding()
        #expect(model.telegramStage == .confirmed)
        #expect(
            try String(contentsOfFile: fixture.environmentPath, encoding: .utf8)
                == "A_TELEGRAM_TOKEN=before\n")
        await model.saveTelegramBinding()
        #expect(
            try String(contentsOfFile: fixture.environmentPath, encoding: .utf8)
                == "A_TELEGRAM_TOKEN=a-test-token\n")
        let attributes = try FileManager.default.attributesOfItem(atPath: fixture.environmentPath)
        #expect((attributes[.posixPermissions] as? NSNumber)?.intValue == 0o600)
        let saved = try String(contentsOfFile: fixture.configPath, encoding: .utf8)
        #expect(saved.contains("chat_id = \"42\""))
        #expect(
            saved.contains(
                "companion_channel = \"gpt_voicecoding.adapters.companion_channel.telegram:telegram_channel\""
            ))
        #expect(saved.contains("[shell.telegram]\nbot_name = \"My bot\""))
        #expect(!saved.contains("a-test-token"))
        #expect(model.pendingRestart)
        #expect(launcher.launchCount == 1)
        await model.restartForSettings()
        #expect(await waitUntil { model.health == .running(pid: 2002) && !model.pendingRestart })
        await model.stopEngine()
    }
    @Test func generalChangesOnlyTheLoginItemAndNextLaunchLanguage() async throws {
        let fixture = try TelegramCredentialFixture()
        let before = try Data(contentsOf: URL(fileURLWithPath: fixture.configPath))
        let suite = "gvc-settings-\(UUID().uuidString)"
        let preferences = UserDefaults(suiteName: suite)!
        defer { preferences.removePersistentDomain(forName: suite) }
        preferences.set("en", forKey: ShellText.preferenceKey)
        var registered = false
        let login = LoginItem(read: { registered }, change: { registered = $0 })
        let launcher = RecordingEngineLauncher()
        let (_, model) = makeShell(
            fixture: fixture, launcher: launcher, loginItem: login, preferences: preferences)
        model.loginItem.set(true)
        model.setLanguage(.chinese)
        #expect(model.loginItem.enabled)
        #expect(model.selectedLanguage == .chinese)
        #expect(model.text.language == "en")
        #expect(preferences.string(forKey: ShellText.preferenceKey) == "zh-Hans")
        #expect(!model.pendingRestart)
        #expect(launcher.launchCount == 0)
        #expect(try Data(contentsOf: URL(fileURLWithPath: fixture.configPath)) == before)
        let (_, relaunched) = makeShell(
            fixture: fixture, launcher: launcher, loginItem: login, preferences: preferences)
        #expect(relaunched.text.language == "zh-Hans")
        await model.stopEngine()
        await relaunched.stopEngine()
    }
    @Test(arguments: [
        (EngineSetting.silence, "silence_end_seconds"),
        (EngineSetting.coolDown, "cool_down_seconds"),
        (EngineSetting.speechSettle, "speech_settle_seconds"),
    ])
    func callTimingSaveRunsThroughTheFileAndAnEngineRestart(setting: EngineSetting, key: String)
        async throws
    {
        let fixture = try TelegramCredentialFixture()
        try fixture.writeEnvironment("A_TELEGRAM_TOKEN=ready\n")
        let location = EngineLocation(
            configPath: fixture.configPath,
            socketPath: fixture.directory.appendingPathComponent("engine.sock").path)
        let file = ShellConfigurationFile(location: location) { _ in
            ShellConfiguration([key: .number(12)])
        }
        let launcher = RecordingEngineLauncher()
        let (_, model) = makeShell(
            fixture: fixture, launcher: launcher,
            panel: ControlPanel(client: DesktopControlPlane()), configurationFile: file)
        await model.startEngineAfterInstallation()
        #expect(await waitUntil { model.health == .running(pid: 2001) })
        await model.saveSetting(setting, value: .number(12))
        #expect(
            try String(contentsOfFile: fixture.configPath, encoding: .utf8).contains(
                "[policy]\n\(key) = 12\n"))
        #expect(model.pendingRestart)
        #expect(launcher.launchCount == 1)
        await model.restartForSettings()
        #expect(await waitUntil { model.health == .running(pid: 2002) && !model.pendingRestart })
        await model.stopEngine()
    }
    @Test func voiceSaveWritesTheFileAndWaitsForTheReplacementEngine() async throws {
        let fixture = try TelegramCredentialFixture()
        try fixture.writeEnvironment("A_TELEGRAM_TOKEN=ready\n")
        let location = EngineLocation(
            configPath: fixture.configPath,
            socketPath: fixture.directory.appendingPathComponent("engine.sock").path)
        let file = ShellConfigurationFile(location: location) { _ in
            ShellConfiguration(["voice": .string("maple")])
        }
        let launcher = RecordingEngineLauncher()
        let (_, model) = makeShell(
            fixture: fixture, launcher: launcher,
            panel: ControlPanel(client: DesktopControlPlane()), configurationFile: file)
        await model.startEngineAfterInstallation()
        #expect(await waitUntil { model.health == .running(pid: 2001) })
        await model.saveSetting(.voice, value: .string("maple"))
        let saved = try String(contentsOfFile: fixture.configPath, encoding: .utf8)
        #expect(saved.contains("[adapters.settings.call]\nvoice = \"maple\""))
        #expect(model.configuration?.voice == "maple")
        #expect(model.pendingRestart)
        #expect(launcher.launchCount == 1)
        await model.restartForSettings()
        #expect(await waitUntil { model.health == .running(pid: 2002) && !model.pendingRestart })
        #expect(launcher.stopCount == 1)
        await model.stopEngine()
    }
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
        let clock = DesktopClock()
        let (_, model) = makeShell(
            fixture: fixture, launcher: RecordingEngineLauncher(),
            panel: ControlPanel(client: engine),
            now: { clock.time }, sleep: clock.sleep)
        model.open(.home)
        // Let each visible read finish before advancing the existing test clock;
        // request arrival alone precedes the poller's bookkeeping on another task.
        #expect(await waitUntil { clock.waiting && engine.requests.count == 2 })
        await model.readInFlight?.value
        clock.advance()
        #expect(await waitUntil { clock.waiting && engine.requests.count == 3 })
        await model.readInFlight?.value
        #expect(engine.requests.filter { $0 == "brief" }.count == 1)
        clock.advance()
        #expect(await waitUntil { clock.waiting && engine.requests.count == 5 })
        await model.readInFlight?.value
        #expect(engine.requests.filter { $0 == "brief" }.count == 2)
        #expect(engine.requests.filter { $0 == "status" }.count == 3)
        model.closeWindow()
        clock.advance()
        #expect(await waitUntil { clock.waiting })
        await model.readInFlight?.value
        #expect(engine.requests.count == 5)
        clock.advance()
        #expect(await waitUntil { clock.waiting && engine.requests.count == 7 })
        await model.readInFlight?.value
        #expect(engine.requests.filter { $0 == "brief" }.count == 3)
        #expect(engine.requests.filter { $0 == "status" }.count == 4)
        engine.duty = false
        clock.advance()
        #expect(await waitUntil { clock.waiting })
        await model.readInFlight?.value
        #expect(engine.requests.count == 7)
        clock.advance()
        #expect(await waitUntil { !model.cardVisible && engine.requests.count == 9 })
        await model.readInFlight?.value
        // Cancellation does not resume the test clock's parked continuation.
        clock.advance()
        await Task.yield()
        #expect(engine.requests.count == 9)
        #expect(clock.delays == Array(repeating: .seconds(1), count: 7))
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

private func testPreferences() -> UserDefaults {
    UserDefaults(suiteName: "gvc-shell-tests-\(UUID().uuidString)")!
}

@MainActor
private func makeShell(
    fixture: TelegramCredentialFixture, launcher: any EngineLaunching,
    panel: ControlPanel? = nil,
    configurationFile: ShellConfigurationFile? = nil,
    loginItem: LoginItem? = nil,
    preferences: UserDefaults = testPreferences(),
    runCommand: (@Sendable (EngineCommand) async -> InstallationReport)? = nil,
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
        supervisor: supervisor, configurationFile: configurationFile, loginItem: loginItem,
        preferences: preferences, runCommand: runCommand, now: now, sleep: sleep)
    return (supervisor, model)
}

@MainActor
private final class DesktopClock {
    var time: TimeInterval = 0
    private(set) var delays: [Duration] = []
    private var continuation: CheckedContinuation<Void, Never>?
    var waiting: Bool { continuation != nil }
    func sleep(_ duration: Duration) async throws {
        delays.append(duration)
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
    private var calls: [Request] = []
    private var dutyOn = true
    private var callUp = false
    var requests: [String] { lock.withLock { calls.map { $0.action.rawValue } } }
    var briefTargets: [JSONValue] {
        lock.withLock {
            calls.filter { $0.action == .brief }.compactMap { $0.payload?["target"] }
        }
    }
    var duty: Bool {
        get { lock.withLock { dutyOn } }
        set { lock.withLock { dutyOn = newValue } }
    }
    func ask(_ request: Request) async throws -> Reply {
        let up = lock.withLock {
            calls.append(request)
            if request.action == .live { callUp.toggle() }
            return callUp
        }
        if request.action == .bindTelegram {
            let data: [String: Any] =
                request.payload?["cancel"]?.bool == true
                ? [:]
                : [
                    "bot_name": "My bot", "username": "my_bot",
                    "chat_id": request.payload?["token"] == nil ? "42" : NSNull(),
                    "chat_type": request.payload?["token"] == nil ? "private" : NSNull(),
                ]
            return try Reply.of(
                JSONSerialization.data(withJSONObject: [
                    "ok": true, "protocol": controlPlaneProtocolVersion,
                    "action": request.action.rawValue, "data": data,
                ]))
        }
        if request.action == .models {
            return try Reply.of(
                JSONSerialization.data(withJSONObject: [
                    "ok": true, "protocol": controlPlaneProtocolVersion,
                    "action": request.action.rawValue,
                    "data": ["models": [["model": "new-model", "efforts": ["high"]]]],
                ]))
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

private actor GatedCallPlane: ControlPlaneDialing {
    nonisolated let dial = OneShot<Void>()
    nonisolated let hangup = OneShot<Void>()
    private var up = false
    func ask(_ request: Request) async throws -> Reply {
        if request.action == .live {
            if up { await hangup.value() } else { await dial.value() }
            up.toggle()
        }
        return try Reply.of(
            JSONSerialization.data(withJSONObject: [
                "ok": true, "action": request.action.rawValue,
                "protocol": controlPlaneProtocolVersion,
                "data": ["state": up ? "up" : "down", "call_id": up ? "call" as Any : NSNull()],
            ]))
    }
}

private struct RefusingTelegramPlane: ControlPlaneDialing {
    let code: String
    func ask(_ request: Request) async throws -> Reply {
        try Reply.of(
            JSONSerialization.data(withJSONObject: [
                "ok": request.action != .bindTelegram, "action": request.action.rawValue,
                "protocol": controlPlaneProtocolVersion, "data": [:],
                "error": request.action == .bindTelegram
                    ? ["code": code, "message": "test refusal"] : NSNull(),
            ]))
    }
}

private struct HeldTelegramPlane: ControlPlaneDialing {
    let reply = OneShot<Void>()
    let tokens = LaunchCounter()
    let cancellations = LaunchCounter()
    func ask(_ request: Request) async throws -> Reply {
        if request.payload?["token"] != nil {
            _ = tokens.increment()
            await reply.value()
        }
        if request.payload?["cancel"]?.bool == true { _ = cancellations.increment() }
        return try Reply.of(
            JSONSerialization.data(withJSONObject: [
                "ok": true, "action": request.action.rawValue,
                "protocol": controlPlaneProtocolVersion,
                "data": ["bot_name": "Held bot", "username": "held_bot"],
            ]))
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
