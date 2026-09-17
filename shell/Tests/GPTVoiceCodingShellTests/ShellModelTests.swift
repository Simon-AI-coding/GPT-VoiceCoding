import AppKit
import Foundation
import ObjectiveC
import Observation
import ShellTestSupport
import SwiftUI
import Testing

@testable import GPTVoiceCodingShell
@testable import ShellCore

@MainActor
@Suite struct ShellModelTests {
    @Test func lampCallingCannotCancelTheDial() async throws {
        let fixture = try TelegramCredentialFixture()
        let engine = GatedCallPlane()
        let (_, model) = makeShell(
            fixture: fixture, launcher: RecordingEngineLauncher(),
            panel: ControlPanel(client: engine))
        await model.panel.refresh()
        let dial = Task { await model.activateLampCell() }
        #expect(await waitUntil { model.panel.phase == .calling })
        await model.activateLampCell()
        #expect(model.panel.phase == .calling)
        #expect(!model.lampCellEnabled)
        engine.dial.resolve()
        await dial.value
        #expect(model.confirmation == nil)
        #expect(model.panel.phase == .onCall)
        #expect(model.panel.lastFailure == nil)
        await model.stopEngine()
    }

    @Test func lampHangupConfirmationKeepsTheCallUntilAcceptedAndExpires() async throws {
        let fixture = try TelegramCredentialFixture()
        let engine = LampControlPlane(up: true)
        var time: TimeInterval = 0
        let (_, model) = makeShell(
            fixture: fixture, launcher: RecordingEngineLauncher(),
            panel: ControlPanel(client: engine), now: { time })
        await model.panel.refresh()
        await model.panel.refreshRoster()
        await model.activateLampCell()
        #expect(model.confirmation == .hangUp)
        #expect(model.confirmationOnLamp)
        #expect(model.panel.phase == .onCall)
        time = 5.9
        model.updateLamp()
        #expect(model.confirmation == .hangUp)
        time = 6
        model.updateLamp()
        #expect(model.confirmation == nil)
        #expect(model.panel.phase == .onCall)
        await model.activateLampCell()
        await model.activateLampCell()
        #expect(model.confirmation == nil)
        await model.activateLampCell()
        await model.resolveConfirmation(accept: false)
        #expect(model.panel.phase == .onCall)
        await model.activateLampCell()
        await model.resolveConfirmation(accept: true)
        #expect(model.panel.phase == .ready)
        await model.stopEngine()
    }

    @Test func appearanceIsAnImmediatePersistedShellPreference() throws {
        let fixture = try TelegramCredentialFixture()
        let preferences = testPreferences()
        let (_, first) = makeShell(
            fixture: fixture, launcher: RecordingEngineLauncher(),
            preferences: preferences)
        #expect(first.selectedAppearance == .system)
        first.setAppearance(.dark)
        #expect(first.selectedAppearance == .dark)
        #expect(!first.pendingRestart)
        let (_, second) = makeShell(
            fixture: fixture, launcher: RecordingEngineLauncher(),
            preferences: preferences)
        #expect(second.selectedAppearance == .dark)
        second.setAppearance(.light)
        #expect(second.selectedAppearance.native?.name == .aqua)
        second.setAppearance(.system)
        #expect(second.selectedAppearance.native == nil)
        #expect(!second.pendingRestart)
    }

    @Test func desktopReminderReplacesWithoutReplayAndStaysWhileHovered() async throws {
        let fixture = try TelegramCredentialFixture()
        let engine = LampControlPlane()
        var time: TimeInterval = 0
        let (_, model) = makeShell(
            fixture: fixture, launcher: RecordingEngineLauncher(),
            panel: ControlPanel(client: engine), now: { time })
        await model.panel.refresh()
        await model.panel.refreshRoster()
        model.updateLamp()
        #expect(model.lampBubble == nil)
        engine.reminder = "second"
        await model.panel.refreshRoster()
        model.updateLamp()
        #expect(model.lampBubble?.row?.name == "second")
        time = 4
        engine.reminder = "third"
        await model.panel.refreshRoster()
        model.updateLamp()
        #expect(model.lampBubble?.row?.name == "third")
        time = 8
        model.updateLamp()
        #expect(model.lampBubble != nil)
        model.setLampPointer(inside: true)
        time = 20
        model.updateLamp()
        #expect(model.lampBubble?.row?.name == "third")
        model.setLampPointer(inside: false)
        #expect(model.lampBubble == nil)
        await model.panel.refreshRoster()
        model.updateLamp()
        #expect(model.lampBubble == nil)
        model.setLampPointer(inside: true)
        #expect(model.lampBubble?.row?.name == "Latest finished")
        model.openLampBrief(try #require(model.lampBubble?.row).target)
        #expect(model.page == .session(model.panel.firstCountedRow!.target))
        await model.stopEngine()
    }

    @Test func clickingTheDisplayedBubbleKeepsItsTargetAcrossReplacement() async throws {
        let fixture = try TelegramCredentialFixture()
        let engine = LampControlPlane()
        let (_, model) = makeShell(
            fixture: fixture, launcher: RecordingEngineLauncher(),
            panel: ControlPanel(client: engine))
        await model.panel.refresh()
        await model.panel.refreshRoster()
        model.updateLamp()
        engine.messageTime = "2026-09-15T00:00:01Z"
        engine.reminder = "displayed"
        await model.panel.refreshRoster()
        model.updateLamp()
        let displayed = try #require(model.lampBubble?.row)
        engine.messageTime = "2026-09-15T00:00:02Z"
        engine.reminder = "replacement"
        await model.panel.refreshRoster()
        model.updateLamp()
        #expect(model.lampBubble?.row?.target != displayed.target)
        let firstTime = try #require(displayed.messageAt)
        let replacementTime = try #require(model.lampBubble?.row?.messageAt)
        #expect(replacementTime.timeIntervalSince(firstTime) == 1)
        model.openLampBrief(displayed.target)
        #expect(model.page == .session(displayed.target))
        await model.stopEngine()
    }

    @Test func remindersExpireAndReconnectionDoesNotReplayTheBaseline() async throws {
        let fixture = try TelegramCredentialFixture()
        let engine = LampControlPlane()
        var time: TimeInterval = 0
        let (_, model) = makeShell(
            fixture: fixture, launcher: RecordingEngineLauncher(),
            panel: ControlPanel(client: engine), now: { time })
        await model.panel.refresh()
        await model.panel.refreshRoster()
        model.updateLamp()
        engine.reminder = "new"
        await model.panel.refreshRoster()
        model.updateLamp()
        time = 4.99
        model.updateLamp()
        #expect(model.lampBubble != nil)
        time = 5
        model.updateLamp()
        #expect(model.lampBubble == nil)
        engine.reachable = false
        await model.panel.refreshRoster()
        model.updateLamp()
        engine.reminder = "while disconnected"
        engine.reachable = true
        await model.panel.refreshRoster()
        model.updateLamp()
        #expect(model.lampBubble == nil)
        await model.stopEngine()
        await model.setDuty(false)
        #expect(!model.cardVisible)
        engine.reminder = "while duty was off"
        await model.setDuty(true)
        await model.panel.refreshRoster()
        model.updateLamp()
        #expect(model.lampBubble == nil)
        engine.reminder = "after reopening"
        await model.panel.refreshRoster()
        model.updateLamp()
        #expect(model.lampBubble?.row?.name == "after reopening")
        engine.reminder = nil
        await model.panel.refreshRoster()
        model.updateLamp()
        #expect(model.lampBubble == nil)
    }

    @Test func onlyFinishedStillAlternatesAndConnectingRestartsTheClock() async throws {
        let fixture = try TelegramCredentialFixture()
        let gate = OneShot<Void>()
        let engine = LampControlPlane(gate: gate)
        var time: TimeInterval = 0
        let panel = ControlPanel(client: engine, now: { time })
        let (_, model) = makeShell(
            fixture: fixture, launcher: RecordingEngineLauncher(),
            panel: panel, now: { time })
        await panel.refresh()
        await panel.refreshRoster()
        let dial = Task { await panel.toggleLive() }
        #expect(await waitUntil { panel.phase == .calling })
        #expect(model.lampTime == nil)
        time = 3
        panel.tick()
        #expect(model.lampTime == "00:03")
        time = 6
        panel.tick()
        #expect(model.lampTime == nil)
        gate.resolve(())
        await dial.value
        #expect(panel.phase == .onCall)
        #expect(panel.elapsed == 0)
        #expect(model.lampTime == nil)
        time = 9
        panel.tick()
        #expect(model.lampTime == "00:03")
        await model.stopEngine()
    }

    @Test func failureBubbleDoesNotExtendTheCallPhase() async throws {
        let fixture = try TelegramCredentialFixture()
        let engine = LampControlPlane(failCall: true)
        var time: TimeInterval = 0
        let panel = ControlPanel(client: engine, now: { time })
        let (_, model) = makeShell(
            fixture: fixture, launcher: RecordingEngineLauncher(),
            panel: panel, now: { time })
        await panel.refresh()
        await panel.refreshRoster()
        model.updateLamp()
        await panel.toggleLive()
        time = 1
        model.updateLamp()
        #expect(model.lampBubble == .failure)
        time = 6
        panel.tick()
        model.updateLamp()
        #expect(panel.phase == .ready)
        #expect(model.lampBubble == nil)
        await model.stopEngine()
    }

    @Test func lampStatesRenderAtTheFixedSizeInBothAppearances() async throws {
        let states: [(String, Int, Int, CallPhase, Int, String)] = [
            ("01-empty", 0, 0, .ready, 0, ""),
            ("02-waiting", 3, 0, .ready, 0, ""),
            ("03-both-counts", 2, 2, .ready, 0, ""),
            ("04-finished", 0, 2, .ready, 0, ""),
            ("05-capped", 10, 10, .ready, 0, ""),
            ("06-calling", 0, 0, .calling, 7, ""),
            ("07-live-counts", 2, 2, .onCall, 0, ""),
            ("08-live-time", 0, 0, .onCall, 84, ""),
            ("09-ending", 0, 0, .ending, 0, ""),
            ("10-failure", 0, 0, .couldNotConnect, 0, ""),
            ("11-engine", 0, 0, .ready, 0, "engine"),
            ("12-both-agents", 2, 0, .onCall, 0, ""),
            ("13-hover", 2, 2, .ready, 0, "hover"),
            ("14-actions-ready", 2, 2, .ready, 0, "actions"),
            ("15-actions-live", 0, 0, .onCall, 84, "actions"),
            ("16-quit", 2, 2, .ready, 0, "quit"),
            ("17-confirm", 0, 0, .onCall, 84, "confirm"),
            ("18-reminder", 3, 0, .ready, 0, "reminder"),
            ("19-hangup-hover", 0, 0, .onCall, 84, "hangup-hover"),
            ("20-hangup-confirm", 0, 0, .onCall, 84, "hangup-confirm"),
        ]
        for (label, waiting, finished, phase, elapsed, interaction) in states {
            let fixture = try TelegramCredentialFixture()
            let gate = phase.resolving ? OneShot<Void>() : nil
            let engine = LampControlPlane(
                waiting: waiting, finished: finished,
                up: phase == .onCall || phase == .ending, gate: gate,
                failCall: phase == .couldNotConnect)
            var time: TimeInterval = 0
            let panel = ControlPanel(client: engine, now: { time })
            let (_, model) = makeShell(
                fixture: fixture, launcher: RecordingEngineLauncher(),
                panel: panel, now: { time })
            await panel.refresh()
            await panel.refreshRoster()
            model.updateLamp()
            let operation: Task<Void, Never>?
            if phase.resolving || phase == .couldNotConnect {
                operation = Task { await panel.toggleLive() }
                #expect(await waitUntil { panel.phase == phase })
            } else {
                operation = nil
            }
            time = TimeInterval(elapsed)
            panel.tick()
            model.updateLamp()
            try #require(panel.phase == phase)
            switch interaction {
            case "engine":
                engine.reachable = false
                await panel.refresh()
                model.updateLamp()
            case "hover": model.setLampPointer(inside: true)
            case "actions": model.setLampPointer(inside: true)
            case "quit": model.toggleLampQuit()
            case "confirm": model.quit()
            case "hangup-hover": model.lampCellHovered = true
            case "hangup-confirm": await model.activateLampCell()
            case "reminder":
                engine.reminder = "atlas · auth flow"
                await panel.refreshRoster()
                model.updateLamp()
            default: break
            }
            #expect(panel.phase == phase)
            for appearance in [ShellAppearance.dark, .light] {
                model.setAppearance(appearance)
                let plate = NSHostingView(rootView: DutyCardView(shell: model))
                plate.appearance = appearance.native
                #expect(plate.fittingSize == NSSize(width: 72, height: 26))
                let board = VStack(alignment: .trailing, spacing: Phosphor.bubbleGap) {
                    HStack(spacing: Phosphor.actionGap) {
                        if model.cardActionsVisible || model.cardQuitVisible {
                            LampActionsView(shell: model)
                        }
                        DutyCardView(shell: model)
                    }
                    if let bubble = model.lampBubble {
                        LampBubbleView(shell: model, bubble: bubble)
                    }
                    Spacer(minLength: 0)
                }.padding(24).frame(width: 380, height: 200, alignment: .topTrailing)
                    .background(Phosphor.sunken)
                try await renderLamp(
                    board, appearance: appearance, name: "lamp-\(label)-\(appearance.rawValue)")
            }
            gate?.resolve(())
            await operation?.value
            await model.stopEngine()
        }
    }

    @Test func nativeLampAttachmentsKeepTheAnchorAndShareTheAppearance() async throws {
        _ = NSApplication.shared
        let frameName = "Lamp-native-test-\(UUID().uuidString)"
        let fixture = try TelegramCredentialFixture()
        let engine = LampControlPlane(waiting: 2, finished: 2)
        let (_, model) = makeShell(
            fixture: fixture, launcher: RecordingEngineLauncher(),
            panel: ControlPanel(client: engine))
        await model.panel.refresh()
        await model.panel.refreshRoster()
        model.updateLamp()
        await model.stopEngine()
        let desktop = DesktopWindows(shell: model, savedFrameName: frameName)
        let surfaces = desktop.surfaces
        defer {
            for surface in surfaces {
                surface.orderOut(nil)
                surface.setFrameAutosaveName("")
            }
            NSWindow.removeFrame(usingName: frameName)
            withExtendedLifetime(desktop) {}
        }
        let lamp = try #require(
            surfaces.first {
                $0 is DutyPanel && $0.frame.size == NSSize(width: 72, height: 26) && $0.isVisible
            })
        let anchor = lamp.frame
        #expect(!lamp.isKeyWindow && !lamp.isMainWindow)
        #expect(lamp.contentView?.frame.size == NSSize(width: 72, height: 26))
        #expect(lamp.contentView?.hitTest(NSPoint(x: 73, y: 10)) == nil)
        let lampHost = try #require(lamp.contentView as? LampHostingView)
        #expect(lampHost.cursor(at: NSPoint(x: 60, y: 13)) === NSCursor.pointingHand)
        #expect(lampHost.cursor(at: NSPoint(x: 10, y: 13)) === NSCursor.pointingHand)
        model.toggleLampQuit()
        #expect(await waitUntil { surfaces.filter { $0 is DutyPanel && $0.isVisible }.count == 2 })
        let quitStrip = try #require(
            surfaces.first { $0 !== lamp && $0 is DutyPanel && $0.isVisible })
        #expect(quitStrip.frame.maxX == anchor.minX - Phosphor.actionGap)
        model.toggleLampQuit()
        model.setLampPointer(inside: true)
        #expect(model.cardActionsVisible)
        #expect(await waitUntil { surfaces.filter { $0 is DutyPanel && $0.isVisible }.count == 3 })
        let actions = try #require(
            surfaces.first {
                $0 !== lamp && $0 is DutyPanel && $0.isVisible && $0.frame.height == 26
            })
        let actionWidth = actions.frame.width
        #expect(actions.frame.maxX == anchor.minX - Phosphor.actionGap)
        #expect(actions.frame.height == 26)
        await model.panel.toggleLive()
        try #require(await waitUntil { model.panel.phase == .onCall })
        await Task.yield()
        #expect(actions.frame.width == actionWidth)
        let bubble = try #require(
            surfaces.first { $0 !== lamp && $0 !== actions && $0 is DutyPanel && $0.isVisible })
        #expect(bubble.frame.width == 280)
        #expect(bubble.frame.maxX == anchor.maxX)
        #expect(bubble.frame.maxY == anchor.minY - Phosphor.bubbleGap)
        // A drag withdraws the strip and the bubble, and they return where the Lamp lands.
        lampHost.onDrag(true)
        #expect(!actions.isVisible && !bubble.isVisible)
        let landed = NSPoint(x: anchor.minX - 50, y: anchor.minY - 40)
        lamp.setFrameOrigin(landed)
        model.setLampPointer(inside: false)
        model.setLampPointer(inside: true)
        for _ in 0..<5 { await Task.yield() }
        #expect(!actions.isVisible && !bubble.isVisible)
        lampHost.onDrag(false)
        model.setLampPointer(inside: true)
        #expect(await waitUntil { actions.isVisible && bubble.isVisible })
        #expect(actions.frame.maxX == landed.x - Phosphor.actionGap)
        #expect(bubble.frame.maxY == landed.y - Phosphor.bubbleGap)
        lamp.setFrameOrigin(anchor.origin)
        #expect(await waitUntil { bubble.frame.maxY == anchor.minY - Phosphor.bubbleGap })
        for appearance in [ShellAppearance.dark, .light, .system] {
            model.setAppearance(appearance)
            #expect(
                await waitUntil {
                    surfaces.allSatisfy { $0.appearance?.name == appearance.native?.name }
                })
        }
        model.quit(fromLamp: true)
        #expect(model.confirmationOnLamp)
        #expect(model.lampBubble == .confirmation)
        #expect(await waitUntil { bubble.isKeyWindow })
        #expect(!lamp.canBecomeKey && !actions.canBecomeKey)
        // Exercise AppKit's Escape command without nesting its event loop inside Swift Testing.
        #expect(bubble.tryToPerform(#selector(NSResponder.cancelOperation(_:)), with: nil))
        #expect(await waitUntil { model.confirmation == nil })
        #expect(model.panel.phase == .onCall)
        #expect(await waitUntil { !bubble.isKeyWindow })
        await model.activateLampCell()
        #expect(model.confirmation == .hangUp)
        #expect(await waitUntil { bubble.isKeyWindow })
        #expect(bubble.tryToPerform(#selector(NSResponder.cancelOperation(_:)), with: nil))
        #expect(await waitUntil { model.confirmation == nil })
        #expect(model.panel.phase == .onCall)
        #expect(await waitUntil { !bubble.isKeyWindow })
        model.setLampPointer(inside: false)
        model.dismissLampActions()
        #expect(await waitUntil { surfaces.filter { $0 is DutyPanel && $0.isVisible }.count == 1 })
        #expect(lamp.frame == anchor)
        let policy = NSApp.activationPolicy()
        model.open(.settings(.general))
        let control = try #require(surfaces.first { !($0 is DutyPanel) })
        #expect(await waitUntil { control.isVisible && control.frame.height > 100 })
        #expect(NSApp.activationPolicy() == policy)
        #expect(control is NSPanel && !control.styleMask.contains(.nonactivatingPanel))
        #expect(control.collectionBehavior.contains([.canJoinAllSpaces, .fullScreenAuxiliary]))
        #expect(control.level > .normal && control.level < lamp.level)
        #expect(control.canBecomeKey)
        #expect(control.standardWindowButton(.closeButton)?.isHidden == true)
        // Hovering while the panel is open expands the strip but shows no Session bubble.
        model.setLampPointer(inside: true)
        #expect(model.cardActionsVisible)
        #expect(model.lampBubble == nil)
        #expect(await waitUntil { surfaces.filter { $0 is DutyPanel && $0.isVisible }.count == 2 })
        model.setLampPointer(inside: false)
        let top = control.frame.maxY
        #expect(control.frame.maxX == anchor.maxX)
        #expect(top == anchor.minY - Phosphor.bubbleGap)
        #expect(control.frame.width == Phosphor.windowWidth)
        for group in SettingsGroup.allCases {
            model.open(.settings(group))
            for _ in 0..<5 { await Task.yield() }
            #expect(control.frame.maxY == top)
            #expect(control.frame.maxX == anchor.maxX)
            #expect(control.frame.width == Phosphor.windowWidth)
        }
        // The open panel moves on its own; the Lamp and it never drag each other.
        #expect(control.isMovable)
        let placed = control.frame
        lamp.setFrameOrigin(NSPoint(x: anchor.minX - 40, y: anchor.minY - 30))
        for _ in 0..<5 { await Task.yield() }
        #expect(control.frame == placed)
        control.setFrameOrigin(NSPoint(x: placed.minX - 60, y: placed.minY - 20))
        model.open(.settings(.voice))
        for _ in 0..<5 { await Task.yield() }
        #expect(control.frame.origin == NSPoint(x: placed.minX - 60, y: control.frame.minY))
        #expect(control.frame.maxY == placed.maxY - 20)
        #expect(lamp.frame.origin == NSPoint(x: anchor.minX - 40, y: anchor.minY - 30))
        lamp.setFrameOrigin(anchor.origin)
        // Escape closes the panel; a second open lands under the Lamp again.
        #expect(control.tryToPerform(#selector(NSResponder.cancelOperation(_:)), with: nil))
        #expect(await waitUntil { !model.windowOpen && !control.isVisible })
        model.toggleControlPanel()
        #expect(await waitUntil { control.isVisible })
        #expect(control.frame.maxY == top)
        model.toggleControlPanel()
        #expect(await waitUntil { !control.isVisible })
        await model.setDuty(false)
        #expect(await waitUntil { !surfaces.contains(where: \.isVisible) })
        // With Duty off, every door still opens the panel at the Lamp's place.
        model.open(.home)
        #expect(await waitUntil { control.isVisible })
        #expect(!lamp.isVisible)
        #expect(control.frame.maxX == anchor.maxX)
        #expect(control.frame.maxY == top)
        model.closeWindow()
        #expect(await waitUntil { !surfaces.contains(where: \.isVisible) })
    }

    @Test func theLampClickTogglesTheControlPanelAndItsOpenPanelHidesSessionBubbles()
        async throws
    {
        let fixture = try TelegramCredentialFixture()
        let engine = LampControlPlane()
        let (_, model) = makeShell(
            fixture: fixture, launcher: RecordingEngineLauncher(),
            panel: ControlPanel(client: engine))
        await model.panel.refresh()
        await model.panel.refreshRoster()
        model.updateLamp()
        #expect(!model.cardActionsVisible)
        model.setLampPointer(inside: true)
        #expect(model.cardActionsVisible)
        #expect(model.lampBubble?.row != nil)
        model.toggleLampQuit()
        #expect(model.cardQuitVisible && !model.cardActionsVisible)
        model.toggleControlPanel()
        #expect(model.page == .home)
        #expect(!model.cardQuitVisible && model.cardActionsVisible)
        #expect(model.lampBubble == nil)
        // A reminder that arrives while the panel is open is not replayed after it closes.
        engine.reminder = "while open"
        await model.panel.refreshRoster()
        model.updateLamp()
        #expect(model.lampBubble == nil)
        model.setLampPointer(inside: false)
        model.toggleControlPanel()
        #expect(model.page == nil)
        model.updateLamp()
        #expect(model.lampBubble == nil)
        // The asks the user must answer still show over the panel.
        model.open(.home)
        model.confirmation = .hangUp
        #expect(model.lampBubble == .confirmation)
        model.confirmation = nil
        engine.reachable = false
        await model.panel.refresh()
        model.updateLamp()
        #expect(model.lampBubble == .engine)
        model.closeWindow()
        await model.stopEngine()
    }

    @Test func generalAppearanceChoicesRenderInBothLanguagesAndThemes() async throws {
        for language in ["en", "zh-Hans"] {
            let fixture = try TelegramCredentialFixture()
            let preferences = testPreferences()
            preferences.set(language, forKey: ShellText.preferenceKey)
            let (_, model) = makeShell(
                fixture: fixture, launcher: RecordingEngineLauncher(), preferences: preferences)
            await model.stopEngine()
            model.open(.settings(.general))
            model.setLanguage(language == "en" ? .chinese : .english)
            for appearance in [ShellAppearance.system, .dark, .light] {
                model.setAppearance(appearance)
                for group in SettingsGroup.allCases {
                    model.open(.settings(group))
                    try await renderLamp(
                        ControlPanelView(shell: model).frame(width: Phosphor.windowWidth),
                        appearance: appearance,
                        name: "settings-\(group)-\(language)-\(appearance.rawValue)")
                }
            }
        }
    }

    private func renderLamp<V: View>(_ content: V, appearance: ShellAppearance, name: String)
        async throws
    {
        let view = NSHostingView(
            rootView: content.environment(\.colorScheme, appearance == .light ? .light : .dark))
        view.appearance = appearance.native
        view.frame.size = view.fittingSize
        view.layoutSubtreeIfNeeded()
        await Task.yield()
        view.frame.size = view.fittingSize
        view.layoutSubtreeIfNeeded()
        #expect(view.frame.width > 0 && view.frame.height > 0)
        if let directory = ProcessInfo.processInfo.environment["GPTVC_RENDER_OUTPUT"] {
            let bitmap = try #require(view.bitmapImageRepForCachingDisplay(in: view.bounds))
            view.cacheDisplay(in: view.bounds, to: bitmap)
            let png = try #require(bitmap.representation(using: .png, properties: [:]))
            try png.write(to: URL(fileURLWithPath: directory).appendingPathComponent(name + ".png"))
        }
    }

    @Test(arguments: [false, true])
    func homeRendersWithinTheDesignWidthInBothLanguages(populated: Bool) async throws {
        for language in ["en", "zh-Hans"] {
            let fixture = try TelegramCredentialFixture()
            let preferences = testPreferences()
            preferences.set(language, forKey: ShellText.preferenceKey)
            let (_, model) = makeShell(
                fixture: fixture, launcher: RecordingEngineLauncher(),
                panel: ControlPanel(client: RenderControlPlane(populated: populated)),
                preferences: preferences)
            await model.readConfiguration {
                ShellConfiguration([
                    "model": .string("gpt-5.6-terra"), "effort": .string("low"),
                    "telegram_bound": .bool(populated),
                ])
            }
            await model.panel.refresh()
            await model.panel.refreshRoster()
            model.open(.home)
            for scheme in [ColorScheme.light, .dark] {
                let view = NSHostingView(
                    rootView: ControlPanelView(shell: model)
                        .frame(width: Phosphor.windowWidth)
                        .environment(\.colorScheme, scheme))
                view.frame.size = view.fittingSize
                view.layoutSubtreeIfNeeded()
                await Task.yield()
                view.frame.size = view.fittingSize
                view.layoutSubtreeIfNeeded()
                #expect(view.frame.width == Phosphor.windowWidth)
                #expect(view.frame.height > 0)
                if let directory = ProcessInfo.processInfo.environment["GPTVC_RENDER_OUTPUT"] {
                    let bitmap = try #require(view.bitmapImageRepForCachingDisplay(in: view.bounds))
                    view.cacheDisplay(in: view.bounds, to: bitmap)
                    let png = try #require(bitmap.representation(using: .png, properties: [:]))
                    try png.write(
                        to: URL(fileURLWithPath: directory)
                            .appendingPathComponent("home-\(language)-\(scheme)-\(populated).png"))
                }
            }
            await model.stopEngine()
        }
    }

    @Test func onboardingStepsRenderAtTheDesignWidthAndMinimumHeight() async throws {
        let template = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("app_bundle/config.example.toml")
        for language in ["en", "zh-Hans"] {
            let fixture = try TelegramCredentialFixture()
            try FileManager.default.removeItem(atPath: fixture.configPath)
            let location = EngineLocation(
                configPath: fixture.configPath,
                socketPath: fixture.directory.appendingPathComponent("engine.sock").path)
            let file = ShellConfigurationFile(location: location) { _ in
                ShellConfiguration([
                    "model": .string("gpt-5.6-terra"), "effort": .string("low"),
                ])
            }
            let preferences = testPreferences()
            preferences.set(language, forKey: ShellText.preferenceKey)
            let (_, model) = makeShell(
                fixture: fixture, launcher: RecordingEngineLauncher(),
                panel: ControlPanel(client: DesktopControlPlane()), configurationFile: file,
                preferences: preferences,
                runCommand: { _ in
                    InstallationReport(
                        ok: true, lines: ["claude-hooks: current", "codex-launch-agent: stale"])
                })
            let cli = fixture.directory.appendingPathComponent("engine/bin/bridgectl")
            await model.begin(template: template, delegateCLI: cli)

            for step in OnboardingStep.allCases {
                if step == .codex { await model.checkCodex() }
                let view = NSHostingView(
                    rootView: ControlPanelView(shell: model).frame(width: Phosphor.windowWidth))
                view.frame.size = view.fittingSize
                view.layoutSubtreeIfNeeded()
                #expect(view.frame.width == Phosphor.windowWidth)
                #expect(view.frame.height >= 300)
                try await renderLamp(
                    ControlPanelView(shell: model).frame(width: Phosphor.windowWidth),
                    appearance: .dark,
                    name: "onboarding-\(step.rawValue)-\(language)-dark")
                if step != .testCall { await model.advanceOnboarding() }
            }
            model.open(.onboarding(.welcome))
            try await renderLamp(
                ControlPanelView(shell: model).frame(width: Phosphor.windowWidth),
                appearance: .light, name: "onboarding-1-\(language)-light")
            await model.stopEngine()
        }
    }

    @Test func sessionBriefRendersAtTheDesignWidth() async throws {
        for language in ["en", "zh-Hans"] {
            let fixture = try TelegramCredentialFixture()
            let preferences = testPreferences()
            preferences.set(language, forKey: ShellText.preferenceKey)
            let (_, model) = makeShell(
                fixture: fixture, launcher: RecordingEngineLauncher(),
                panel: ControlPanel(client: RenderControlPlane(populated: true)),
                preferences: preferences)
            let target = SessionAddress(
                .of(["agent": "codex", "session_id": "atlas · auth flow"]))
            await model.panel.refreshRoster()
            model.open(.session(target))
            await model.panel.refreshSession(target)

            for appearance in ShellAppearance.allCases where appearance != .system {
                try await renderLamp(
                    ControlPanelView(shell: model).frame(width: Phosphor.windowWidth),
                    appearance: appearance,
                    name: "session-brief-\(language)-\(appearance.rawValue)")
            }
            await model.stopEngine()
        }
    }

    @Test func sessionBriefFormatsInlineMarkdownWithoutShowingItsMarkers() {
        let rendered = SessionBriefView.formatted("Yes, **send this one** now.")
        #expect(String(rendered.characters) == "Yes, send this one now.")
    }

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

    @Test(arguments: [ShellPage.settings(.telegram), .onboarding(.telegram)])
    func telegramWaitsForTheUsersCheckInsteadOfPolling(page: ShellPage) async throws {
        let fixture = try TelegramCredentialFixture()
        let engine = DesktopControlPlane()
        let clock = DesktopClock()
        let (_, model) = makeShell(
            fixture: fixture, launcher: RecordingEngineLauncher(),
            panel: ControlPanel(client: engine), now: { clock.time }, sleep: clock.sleep)
        model.open(page)
        #expect(await waitUntil { clock.waiting && model.panel.engineReachable })
        await model.readInFlight?.value
        model.telegramToken = "a-test-token"
        await model.validateTelegram()
        for _ in 0..<2 {
            clock.advance()
            #expect(await waitUntil { clock.waiting })
            await model.readInFlight?.value
        }
        #expect(model.telegramStage == .waiting)
        #expect(engine.requests.filter { $0 == "bind_telegram" }.count == 1)
        await model.refreshTelegramBinding()
        #expect(model.telegramStage == .confirmed)
        await model.stopEngine()
        clock.advance()
    }

    @Test func aManualTelegramCheckWithoutAMessageCanBeRetriedWithoutSaving() async throws {
        let fixture = try TelegramCredentialFixture()
        let engine = DesktopControlPlane()
        engine.telegramMessageAvailable = false
        let (_, model) = makeShell(
            fixture: fixture, launcher: RecordingEngineLauncher(),
            panel: ControlPanel(client: engine))
        await model.panel.refresh()
        model.telegramToken = "a-test-token"
        await model.validateTelegram()
        #expect(!model.telegramCheckFinished)
        await model.refreshTelegramBinding()
        #expect(model.telegramStage == .waiting)
        #expect(model.telegramCheckFinished)
        #expect(model.canCheckTelegram)
        await model.saveTelegramBinding()
        #expect(!FileManager.default.fileExists(atPath: fixture.environmentPath))
        #expect(!model.pendingRestart)
        engine.telegramMessageAvailable = true
        await model.refreshTelegramBinding()
        #expect(model.telegramStage == .confirmed)
        #expect(!model.telegramCheckFinished)
        #expect(!model.canCheckTelegram)
        model.cancelTelegramBinding()
        #expect(!model.telegramCheckFinished)
        await model.stopEngine()
    }

    @Test func anInflightManualTelegramCheckCannotDuplicateOrSurviveCancellation() async throws {
        let fixture = try TelegramCredentialFixture()
        let engine = HeldTelegramPlane(holdValidation: false)
        let (_, model) = makeShell(
            fixture: fixture, launcher: RecordingEngineLauncher(),
            panel: ControlPanel(client: engine))
        await model.panel.refresh()
        model.telegramToken = "a-test-token"
        await model.validateTelegram()
        let checking = Task { await model.refreshTelegramBinding() }
        #expect(await waitUntil { engine.checks.value == 1 })
        #expect(model.checkingTelegram)
        #expect(!model.canCheckTelegram)
        await model.refreshTelegramBinding()
        #expect(engine.checks.value == 1)
        model.cancelTelegramBinding()
        engine.reply.resolve()
        await checking.value
        #expect(await waitUntil { engine.cancellations.value == 1 })
        #expect(model.telegramStage == .idle)
        #expect(model.telegramBinding == nil)
        #expect(!model.telegramCheckFinished)
        #expect(!model.checkingTelegram)
        #expect(!FileManager.default.fileExists(atPath: fixture.environmentPath))
        await model.stopEngine()
    }

    @Test(arguments: [false, true])
    func manualTelegramInstructionsRenderInBothLanguages(onboarding: Bool) async throws {
        for language in ["en", "zh-Hans"] {
            let fixture = try TelegramCredentialFixture()
            let preferences = testPreferences()
            preferences.set(language, forKey: ShellText.preferenceKey)
            let (_, model) = makeShell(
                fixture: fixture, launcher: RecordingEngineLauncher(),
                panel: ControlPanel(client: DesktopControlPlane()), preferences: preferences)
            await model.panel.refresh()
            model.open(onboarding ? .onboarding(.telegram) : .settings(.telegram))
            model.telegramToken = "a-test-token"
            await model.validateTelegram()
            let view = NSHostingView(
                rootView: ControlPanelView(shell: model).frame(width: Phosphor.windowWidth))
            view.frame.size = view.fittingSize
            view.layoutSubtreeIfNeeded()
            await Task.yield()
            view.frame.size = view.fittingSize
            view.layoutSubtreeIfNeeded()
            #expect(view.frame.width == Phosphor.windowWidth)
            #expect(view.frame.height > 0)
            if let directory = ProcessInfo.processInfo.environment["GPTVC_RENDER_OUTPUT"] {
                let bitmap = try #require(view.bitmapImageRepForCachingDisplay(in: view.bounds))
                view.cacheDisplay(in: view.bounds, to: bitmap)
                let png = try #require(bitmap.representation(using: .png, properties: [:]))
                try png.write(
                    to: URL(fileURLWithPath: directory)
                        .appendingPathComponent("telegram-\(language)-\(onboarding).png"))
            }
            await model.stopEngine()
        }
    }
    @Test func generalLanguageWaitsForRelaunchWithoutRaisingAnEngineRestart() async throws {
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
        model.setAppearance(.dark)
        #expect(!model.pendingRestart)
        model.setLanguage(.english)
        #expect(!model.pendingRestart)
        model.setLanguage(.chinese)
        await model.restartForSettings()
        #expect(model.text.language == "en")
        #expect(model.text(.settings) == "Settings")
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
        let clock = DesktopClock()
        let target = SessionAddress(.of(["agent": "claude", "session_id": "one"]))
        let (_, model) = makeShell(
            fixture: fixture, launcher: RecordingEngineLauncher(),
            panel: ControlPanel(client: engine), now: { clock.time }, sleep: clock.sleep)
        model.open(.session(target))
        #expect(await waitUntil { clock.waiting })
        await model.readInFlight?.value
        #expect(engine.briefTargets == [target.payload])
        for second in 1...2 {
            clock.advance()
            #expect(await waitUntil { clock.waiting })
            await model.readInFlight?.value
            #expect(engine.briefTargets.count == (second == 1 ? 1 : 2))
        }
        #expect(engine.briefTargets == [target.payload, target.payload])
        model.open(.home)
        for _ in 0..<2 {
            clock.advance()
            #expect(await waitUntil { clock.waiting })
            await model.readInFlight?.value
        }
        #expect(engine.briefTargets.count == 2)
        await model.stopEngine()
        clock.advance()
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

    @Test func onePollerFollowsVisibilityButStillDetectsExternalDutyOn() async throws {
        let fixture = try TelegramCredentialFixture()
        let engine = DesktopControlPlane()
        let clock = DesktopClock()
        let (_, model) = makeShell(
            fixture: fixture, launcher: RecordingEngineLauncher(),
            panel: ControlPanel(client: engine),
            wallNow: { Date(timeIntervalSince1970: 1_000_000 + clock.time) },
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
        #expect(model.messageNow == Date(timeIntervalSince1970: 1_000_001))
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
        clock.advance()
        #expect(await waitUntil { clock.waiting })
        await model.readInFlight?.value
        #expect(engine.requests.count == 9)
        clock.advance()
        #expect(await waitUntil { clock.waiting && engine.requests.count == 10 })
        await model.readInFlight?.value
        #expect(engine.requests.last == "status")
        #expect(!model.cardVisible)
        #expect(!model.windowOpen)
        let briefsWhileHidden = engine.requests.filter { $0 == "brief" }.count

        // The engine changes independently, as with bridgectl or another surface.
        engine.duty = true
        clock.advance()
        #expect(await waitUntil { clock.waiting })
        await model.readInFlight?.value
        clock.advance()
        #expect(await waitUntil { clock.waiting && model.cardVisible })
        await model.readInFlight?.value
        #expect(!model.windowOpen)
        #expect(engine.requests.filter { $0 == "brief" }.count == briefsWhileHidden)
        clock.advance()
        #expect(
            await waitUntil {
                clock.waiting
                    && engine.requests.filter { $0 == "brief" }.count == briefsWhileHidden + 1
            })
        await model.readInFlight?.value

        await model.stopEngine()
        let requestsAtShutdown = engine.requests.count
        clock.advance()
        await Task.yield()
        #expect(engine.requests.count == requestsAtShutdown)
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

private struct RenderControlPlane: ControlPlaneDialing {
    let populated: Bool
    func ask(_ request: Request) async throws -> Reply {
        if request.action == .brief, request.payload?["target"] != nil {
            return try Reply.of(
                JSONSerialization.data(withJSONObject: [
                    "ok": true, "action": request.action.rawValue,
                    "protocol": controlPlaneProtocolVersion,
                    "data": [
                        "session": [
                            "name": "atlas · auth flow",
                            "state_word": "waiting for your decision",
                            "newest": [
                                "text":
                                    "I can rotate the key now. Two jobs are still holding the old one, so I need to know what happens to them before I go ahead.",
                                "occurred_at": "2026-09-15T04:00:00Z",
                            ],
                            "decision": [
                                "prompt":
                                    "Should the old sessions keep working after the key rotates, or expire right away?",
                                "options": [
                                    ["text": "Keep them working until they expire on their own"],
                                    ["text": "Expire them all now"],
                                ],
                            ],
                        ]
                    ],
                ]))
        }
        let names = ["atlas · auth flow", "harbor · checkout", "lumen · docs site"]
        let rows: [[String: Any]] =
            populated
            ? names.map { name in
                [
                    "target": ["agent": "codex", "session_id": name], "name": name,
                    "state": "decision", "state_word": "waiting for your decision",
                    "newest": "Should the old sessions keep working after the key rotates?",
                ]
            } : []
        let data: [String: Any] = [
            "switches": ["duty": true, "voice": true, "message": true],
            "sessions": rows.map { _ in ["lifecycle": "live", "brief_state": "decision"] },
            "roster": ["rows": rows],
        ]
        return try Reply.of(
            JSONSerialization.data(withJSONObject: [
                "ok": true, "action": request.action.rawValue,
                "protocol": controlPlaneProtocolVersion, "data": data,
            ]))
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
    wallNow: @escaping () -> Date = Date.init,
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
        preferences: preferences, runCommand: runCommand, wallNow: wallNow, now: now, sleep: sleep)
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
    private var messageAvailable = true
    var telegramMessageAvailable: Bool {
        get { lock.withLock { messageAvailable } }
        set { lock.withLock { messageAvailable = newValue } }
    }
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
            let confirmed = request.payload?["token"] == nil && telegramMessageAvailable
            let data: [String: Any] =
                request.payload?["cancel"]?.bool == true
                ? [:]
                : [
                    "bot_name": "My bot", "username": "my_bot",
                    "chat_id": confirmed ? "42" : NSNull(),
                    "chat_type": confirmed ? "private" : NSNull(),
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
    private var attempt: String?
    private var cancelled = false
    nonisolated let dial = OneShot<Void>()
    nonisolated let hangup = OneShot<Void>()
    private var up = false
    func ask(_ request: Request) async throws -> Reply {
        var acceptedCancellation = false
        if request.action == .live {
            if let cancelling = request.payload?["cancel_dial"]?.string {
                acceptedCancellation = cancelling == attempt
                if acceptedCancellation {
                    cancelled = true
                    dial.resolve()
                }
            } else {
                attempt = request.payload?["attempt_id"]?.string
                cancelled = false
                if up { await hangup.value() } else { await dial.value() }
                if !cancelled { up.toggle() }
            }
        }
        return try Reply.of(
            JSONSerialization.data(withJSONObject: [
                "ok": true, "action": request.action.rawValue,
                "protocol": controlPlaneProtocolVersion,
                "data": [
                    "state": up ? "up" : "down", "call_id": up ? "call" as Any : NSNull(),
                    "cancelled": acceptedCancellation,
                ],
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
    var holdValidation = true
    let reply = OneShot<Void>()
    let tokens = LaunchCounter()
    let checks = LaunchCounter()
    let cancellations = LaunchCounter()
    func ask(_ request: Request) async throws -> Reply {
        if request.payload?["token"] != nil {
            _ = tokens.increment()
            if holdValidation { await reply.value() }
        }
        if request.action == .bindTelegram, request.payload?["token"] == nil,
            request.payload?["cancel"] == nil
        {
            _ = checks.increment()
            if !holdValidation { await reply.value() }
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

private final class LampControlPlane: ControlPlaneDialing, @unchecked Sendable {
    private let lock = NSLock()
    private var reminderID: String? = "first"
    private var messageTimestamp: String?
    var messageTime: String? {
        get { lock.withLock { messageTimestamp } }
        set { lock.withLock { messageTimestamp = newValue } }
    }
    private var available = true
    private var dutyOn = true
    private var callUp: Bool
    let waiting: Int
    let finished: Int
    let gate: OneShot<Void>?
    let failCall: Bool
    var reminder: String? {
        get { lock.withLock { reminderID } }
        set { lock.withLock { reminderID = newValue } }
    }
    var reachable: Bool {
        get { lock.withLock { available } }
        set { lock.withLock { available = newValue } }
    }
    init(
        waiting: Int = 0, finished: Int = 1, up: Bool = false,
        gate: OneShot<Void>? = nil, failCall: Bool = false
    ) {
        self.waiting = waiting
        self.finished = finished
        self.callUp = up
        self.gate = gate
        self.failCall = failCall
    }
    func ask(_ request: Request) async throws -> Reply {
        if !reachable { throw ControlPlaneFailure.engineUnreachable("test disconnection") }
        if request.action == .live {
            if let gate { await gate.value() }
            lock.withLock { if !failCall { callUp.toggle() } }
        }
        let data: [String: Any] = lock.withLock {
            if request.action == .switch, let on = request.payload?["on"]?.bool { dutyOn = on }
            func row(_ name: String, state: String = "finished", agent: String = "codex")
                -> [String: Any]
            {
                var document: [String: Any] = [
                    "target": ["agent": agent, "session_id": name], "name": name,
                    "state": state,
                    "state_word": state == "decision" ? "waiting for your decision" : "finished",
                    "newest": "Should the old sessions keep working after the key rotates?",
                ]
                if let messageTimestamp { document["message_at"] = messageTimestamp }
                return document
            }
            if request.action == .brief {
                var roster: [String: Any] = [
                    "rows":
                        (0..<finished).map { row($0 == 0 ? "Latest finished" : "Finished \($0)") }
                        + (0..<waiting).map {
                            row(
                                "atlas · auth flow \($0)", state: "decision",
                                agent: $0.isMultiple(of: 2) ? "claude" : "codex")
                        }
                ]
                if let reminderID {
                    roster["desktop_reminder"] = ["id": reminderID, "row": row(reminderID)]
                }
                return ["roster": roster]
            }
            return [
                "switches": ["duty": dutyOn],
                "sessions": (0..<waiting).map { _ in
                    ["lifecycle": "live", "brief_state": "decision"]
                }
                    + (0..<finished).map { _ in ["lifecycle": "live", "brief_state": "finished"] },
                "state": callUp ? "up" : "down", "call_id": callUp ? "call" as Any : NSNull(),
            ]
        }
        return try Reply.of(
            JSONSerialization.data(withJSONObject: [
                "ok": true, "action": request.action.rawValue,
                "protocol": controlPlaneProtocolVersion, "data": data,
            ]))
    }
}
