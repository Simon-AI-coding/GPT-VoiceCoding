import AppKit
import ShellCore
import SwiftUI

/// A static four-item menu. The card and normal window are AppKit-owned.
@main
struct ShellApp: App {
    @NSApplicationDelegateAdaptor(ShellDelegate.self) private var delegate

    var body: some Scene {
        MenuBarExtra {
            let shell = delegate.shell
            Button(shell.text(.controlPanel)) { shell.open(.home) }
            Button(shell.text(.settings)) { shell.open(.settings(.voice)) }
            Toggle(
                shell.text(.duty),
                isOn: Binding(
                    get: { shell.panel.dutyOn },
                    set: { on in
                        Task { await shell.setDuty(on) }
                    })
            )
            .disabled(!shell.panel.engineReachable || shell.panel.busy)
            Button(shell.text(.quit)) { shell.quit() }.keyboardShortcut("q")
        } label: {
            Image(nsImage: DesignMark.menu.image)
        }
        .menuBarExtraStyle(.menu)
    }
}

/// AppKit must give the shell a chance to stop the child before it goes away.
///
/// Adapted from `legacy@1d32845:bridge/daemon.py:3056-3065,3091-3101`: its owner
/// installed termination handlers before serving; delegate ownership keeps that order.
@MainActor
final class ShellDelegate: NSObject, NSApplicationDelegate {
    let shell: ShellModel
    private var windows: DesktopWindows?
    override init() { shell = ShellModel() }
    init(shell: ShellModel) { self.shell = shell }

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.applicationIconImage = DesignMark.app.image
        windows = DesktopWindows(shell: shell)
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard !shell.stopping else { return .terminateNow }
        if shell.panel.phase == .onCall {
            shell.quit()
            return .terminateCancel
        }
        Task {
            await shell.stopEngine()
            NSApp.reply(toApplicationShouldTerminate: true)
        }
        return .terminateLater
    }
}
