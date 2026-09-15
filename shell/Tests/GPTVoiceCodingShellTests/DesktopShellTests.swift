import AppKit
import Foundation
import Observation
import ShellTestSupport
import SwiftUI
import Testing

@testable import GPTVoiceCodingShell
@testable import ShellCore

@MainActor
@Suite struct DesktopShellTests {
    @Test func backgroundCursorIsANoOpWhenThePrivateSymbolsAreGone() {
        #expect(!BackgroundCursor.enable(resolve: { _ in nil }))
    }

    @Test func backgroundCursorResolvesItsPrivateSymbolsOnThisMacOS() {
        let resolved = { (name: String) in dlsym(dlopen(nil, RTLD_NOW), name) != nil }
        #expect(resolved("CGSMainConnectionID"))
        #expect(resolved("CGSSetConnectionProperty"))
    }

    @Test func controlWindowUsesLampScreenAndKeepsItsTopWhenContentChanges() {
        let screen = NSRect(x: -1440, y: 100, width: 1440, height: 900)
        let lamp = NSRect(x: -100, y: 900, width: 72, height: 26)
        let initial = ControlWindow.frame(
            NSRect(x: 0, y: 0, width: 480, height: 350),
            height: 350, anchor: lamp, screen: screen)
        #expect(initial.maxX == lamp.maxX)
        #expect(initial.maxY == lamp.minY - Phosphor.bubbleGap)
        #expect(screen.contains(initial))
        let taller = ControlWindow.frame(initial, height: 600, anchor: nil, screen: screen)
        #expect(taller.maxY == initial.maxY)
        #expect(taller.minX == initial.minX)
        let oversized = ControlWindow.frame(
            initial, height: 1500, anchor: lamp, screen: screen)
        #expect(screen.contains(oversized))
        #expect(oversized.height == screen.height)
    }

    @Test func controlWindowLeavesItsMeasuredContentBelowTheTitleBar() {
        let style: NSWindow.StyleMask = [
            .titled, .closable, .miniaturizable, .resizable, .fullSizeContentView,
        ]
        let contentHeight: CGFloat = 350
        let frameHeight = ControlWindow.frameHeight(
            contentHeight: contentHeight, styleMask: style)
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 480, height: frameHeight),
            styleMask: style, backing: .buffered, defer: true)

        #expect(window.contentLayoutRect.height == contentHeight)
    }

    @Test func controlWindowFollowsContentThatGrowsAndShrinksWhileShown() async {
        let content = GrowingContent()
        let control = ControlWindow(content: GrowingView(content: content))
        defer { control.dismiss() }
        control.present(anchor: nil, screen: nil)
        #expect(control.window.contentLayoutRect.height == 100)
        content.rows = 6
        #expect(await waitUntil { control.window.contentLayoutRect.height == 300 })
        content.rows = 3
        #expect(await waitUntil { control.window.contentLayoutRect.height == 150 })
    }

    @Test func controlWindowOpensAtTheHeightItsContentReachedWhileHidden() {
        let content = GrowingContent()
        let control = ControlWindow(content: GrowingView(content: content))
        defer { control.dismiss() }
        content.rows = 5
        control.present(anchor: nil, screen: nil)
        #expect(control.window.contentLayoutRect.height == 250)
    }

    @Test func aStaleLoginItemIsPresentedAsPlacedInsteadOfFailed() {
        let presentation = OnboardingView.placementPresentation(
            state: .stale, current: .codexPlaced, absent: .codexNotPlaced)

        #expect(presentation.copy == .codexPlaced)
        #expect(!presentation.failed)
    }

    @Test func messageAgeUsesOneUnitAndNeverInventsMissingTime() {
        let text = ShellText(language: "en")
        let stamp = Date(timeIntervalSince1970: 1_000_000)
        for (seconds, expected) in [
            (0, "now"), (9, "now"), (10, "10s"), (59, "59s"), (60, "1m"), (3600, "1h"),
            (86400, "1d"), (691200, "8d"),
        ] {
            #expect(
                text.messageAge(stamp, at: stamp.addingTimeInterval(Double(seconds))) == expected)
        }
        #expect(text.messageAge(nil, at: stamp) == "—")
        #expect(text.messageAge(stamp, at: stamp.addingTimeInterval(-10)) == "now")
        #expect(text.messageAge(stamp, at: stamp.addingTimeInterval(60), long: true) == "1m ago")
        let chinese = ShellText(language: "zh-Hans")
        for (seconds, short, detail) in [
            (0, "now", "刚刚"), (10, "10s", "10秒前"), (60, "1m", "1分钟前"),
            (3600, "1h", "1小时前"), (691200, "8d", "8天前"),
        ] {
            let current = stamp.addingTimeInterval(Double(seconds))
            #expect(chinese.messageAge(stamp, at: current) == short)
            #expect(chinese.messageAge(stamp, at: current, long: true) == detail)
        }
    }

    @Test func theReadingRegionKeepsDiagonalTravelIntoTheWiderBubble() {
        let lamp = NSRect(x: 228, y: 400, width: 72, height: 26)
        let bubble = NSRect(x: 20, y: 320, width: 280, height: 72)
        for point in [NSPoint(x: 230, y: 402), NSPoint(x: 224, y: 396), NSPoint(x: 150, y: 390)] {
            #expect(DesktopWindows.containsReadingPoint(point, lamp: lamp, bubble: bubble))
        }
        #expect(
            !DesktopWindows.containsReadingPoint(NSPoint(x: 19, y: 396), lamp: lamp, bubble: bubble)
        )
        #expect(
            !DesktopWindows.containsReadingPoint(NSPoint(x: 224, y: 396), lamp: lamp, bubble: nil))
    }

    @Test func missingNamesAreLocalisedButCoreStateWordsAreNot() {
        let row = BriefRow(.of(["state_word": "finished"]))
        let brief = SessionBriefReading(.of(["state_word": "finished"]))
        #expect(ShellText(language: "en").name(row.name) == "Session")
        #expect(ShellText(language: "zh-Hans").name(brief.name) == "会话")
        #expect(row.stateWord == "finished")
        #expect(brief.stateWord == "finished")
    }

    @Test func allFourDesignMarksLoadFromTheResourceBundle() {
        for mark in DesignMark.allCases {
            #expect(mark.image.size.width > 0)
            #expect(mark.image.isTemplate == (mark != .app))
        }
    }

    @Test func theDutyPanelNeverActivatesAndKeepsItsPositionAcrossSpaces() {
        let name = "DutyCard-test-\(UUID().uuidString)"
        defer { NSWindow.removeFrame(usingName: name) }
        let panel = DutyPanel(savedFrameName: name)
        #expect(panel.styleMask.contains(.nonactivatingPanel))
        #expect(panel.collectionBehavior.contains([.canJoinAllSpaces, .fullScreenAuxiliary]))
        #expect(panel.level == .floating)
        #expect(!panel.hidesOnDeactivate)
        #expect(!panel.canBecomeKey)
        #expect(!panel.canBecomeMain)
        #expect(panel.isMovableByWindowBackground)
        let screen = NSRect(x: 100, y: 40, width: 1200, height: 760)
        let frame = DutyPanel.initialFrame(in: screen, height: 70)
        #expect(frame.width == 72)
        #expect(panel.frame.size == NSSize(width: 72, height: 26))
        #expect(frame.maxX == screen.maxX - 16)
        #expect(frame.maxY == screen.maxY - 8)
    }

    @Test func firstDutyCardLayoutDoesNotBecomeASavedBottomLeftPosition() throws {
        let name = "DutyCard-test-\(UUID().uuidString)"
        defer { NSWindow.removeFrame(usingName: name) }
        let panel = DutyPanel(savedFrameName: name)
        let screen = try #require(NSScreen.main).visibleFrame
        // Hosting content can resize the hidden panel before Duty becomes visible.
        var measured = panel.frame
        measured.origin.y += measured.height - 70
        measured.size.height = 70
        panel.setFrame(measured, display: false)
        panel.place(in: screen)
        #expect(panel.frame.maxX == screen.maxX - 16)
        #expect(panel.frame.maxY == screen.maxY - 8)
        #expect(panel.frame.size == NSSize(width: 72, height: 26))
        #expect(panel.frameAutosaveName == name)
    }

    @Test func dutyCardKeepsTheUsersPositionOnReshowAndRelaunch() throws {
        let name = "DutyCard-test-\(UUID().uuidString)"
        defer { NSWindow.removeFrame(usingName: name) }
        let screen = try #require(NSScreen.main).visibleFrame
        let moved = NSRect(x: screen.minX + 80, y: screen.minY + 100, width: 72, height: 26)
        do {
            let panel = DutyPanel(savedFrameName: name)
            panel.place(in: screen)
            panel.setFrame(moved, display: false)
            panel.place(in: screen)
            #expect(panel.frame == moved)
            panel.saveFrame(usingName: name)
            panel.setFrameAutosaveName("")
        }
        let saved = try #require(UserDefaults.standard.string(forKey: "NSWindow Frame \(name)"))
        // Use native restoration as the oracle: AppKit can remap coordinates across displays.
        let native = DutyPanel(savedFrameName: name)
        let measured = NSRect(x: 0, y: 0, width: 352, height: 90)
        native.setFrame(measured, display: false)
        #expect(native.setFrameUsingName(name, force: true))
        let relaunched = DutyPanel(savedFrameName: name)
        relaunched.setFrame(measured, display: false)
        #expect(UserDefaults.standard.string(forKey: "NSWindow Frame \(name)") == saved)
        relaunched.place(in: screen)
        #expect(relaunched.frame.maxX == native.frame.maxX)
        #expect(relaunched.frame.maxY == native.frame.maxY)
        #expect(relaunched.frame.size == NSSize(width: 72, height: 26))
    }

    @Test func anOldSavedCardRestoresOnlyItsTopRightAnchor() throws {
        let name = "DutyCard-test-\(UUID().uuidString)"
        defer { NSWindow.removeFrame(usingName: name) }
        let screen = try #require(NSScreen.main).visibleFrame
        let old = DutyPanel(savedFrameName: name)
        old.setFrame(
            NSRect(x: screen.minX + 80, y: screen.minY + 100, width: 352, height: 70),
            display: false)
        old.saveFrame(usingName: name)
        let native = DutyPanel(savedFrameName: name)
        #expect(native.setFrameUsingName(name, force: true))
        let lamp = DutyPanel(savedFrameName: name)
        lamp.place(in: screen)
        #expect(lamp.frame.maxX == native.frame.maxX)
        #expect(lamp.frame.maxY == native.frame.maxY)
        #expect(lamp.frame.size == NSSize(width: 72, height: 26))
    }

    @Test func allShellWordsHaveBothLanguagesAndThePreferenceAppliesOnRelaunch() throws {
        let english = ShellText(language: "en")
        let chinese = ShellText(language: "zh-Hans")
        let directories = try FileManager.default.contentsOfDirectory(
            atPath: Bundle.shell.bundlePath)
        for language in ["en", "zh-hans"] {
            // SwiftPM normalises resource directory names in its built bundle.
            #expect(directories.contains("\(language).lproj"))
            let file = Bundle.shell.bundleURL.appendingPathComponent(
                "\(language).lproj/Localizable.strings")
            let words = try #require(
                PropertyListSerialization.propertyList(
                    from: Data(contentsOf: file),
                    format: nil) as? [String: String])
            for key in Copy.allCases { #expect(words[key.rawValue] != nil) }
        }
        #expect(english(.ready) == "Ready")
        #expect(chinese(.ready) == "就绪")
        #expect(ShellText.preferredLanguage(saved: nil, system: ["zh-Hant-NZ"]) == "zh-Hans")
        #expect(ShellText.preferredLanguage(saved: "en", system: ["zh-Hans"]) == "en")
    }
}

/// Content whose natural height the test changes: 50 points a row.
@MainActor @Observable private final class GrowingContent {
    var rows = 2
}

private struct GrowingView: View {
    let content: GrowingContent
    var body: some View {
        VStack(spacing: 0) {
            ForEach(0..<content.rows, id: \.self) { _ in Color.clear.frame(height: 50) }
        }
    }
}
