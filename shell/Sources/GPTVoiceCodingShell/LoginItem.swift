import Foundation
import Observation
import ServiceManagement

/// Launch at login, as the app itself.
///
/// `SMAppService.mainApp` — **not** `agent(plistName:)`, which would put a
/// launchd job back in the picture and recreate the shape ADR 0005 moved away
/// from. The app is `LSUIElement`, so launching it at login means a menu-bar
/// item and no Dock icon, not a window.
@MainActor
@Observable
final class LoginItem {
    private(set) var enabled: Bool
    /// The system's own words when it refuses. Not rephrased here.
    private(set) var failure: String?
    private let read: () -> Bool
    private let change: (Bool) throws -> Void

    init(
        read: @escaping () -> Bool = { SMAppService.mainApp.status == .enabled },
        change: @escaping (Bool) throws -> Void = { wanted in
            if wanted {
                try SMAppService.mainApp.register()
            } else {
                try SMAppService.mainApp.unregister()
            }
        }
    ) {
        self.read = read
        self.change = change
        enabled = read()
    }

    func set(_ wanted: Bool) {
        do {
            try change(wanted)
            failure = nil
        } catch {
            failure = error.localizedDescription
        }
        enabled = read()
    }
}
