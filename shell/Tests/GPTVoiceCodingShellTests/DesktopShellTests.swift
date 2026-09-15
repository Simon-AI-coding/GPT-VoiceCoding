import AppKit
import Foundation
import Testing

@testable import GPTVoiceCodingShell
@testable import ShellCore

@MainActor
@Suite struct DesktopShellTests {
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
