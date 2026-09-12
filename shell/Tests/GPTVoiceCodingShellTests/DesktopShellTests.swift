import AppKit
import Foundation
import Testing

@testable import GPTVoiceCodingShell
@testable import ShellCore

@MainActor
@Suite struct DesktopShellTests {
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
        let panel = DutyPanel()
        #expect(panel.styleMask.contains(.nonactivatingPanel))
        #expect(panel.collectionBehavior.contains([.canJoinAllSpaces, .fullScreenAuxiliary]))
        #expect(panel.level == .floating)
        #expect(!panel.hidesOnDeactivate)
        #expect(!panel.canBecomeKey)
        #expect(!panel.canBecomeMain)
        #expect(panel.isMovableByWindowBackground)
        #expect(panel.frameAutosaveName == DutyPanel.autosaveName)
        let screen = NSRect(x: 100, y: 40, width: 1200, height: 760)
        let frame = DutyPanel.initialFrame(in: screen, height: 70)
        #expect(frame.width == 352)
        #expect(frame.maxX == screen.maxX - 16)
        #expect(frame.maxY == screen.maxY - 8)
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
