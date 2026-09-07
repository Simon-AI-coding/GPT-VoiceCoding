import Foundation

/// Where a seam found itself running, written on that thread and read on the test's.
///
/// Shared because two suites make the same claim about two callers — #276's
/// rule is that the login-shell read happens on a thread its caller made, and
/// `InstallationRunner` and `ProcessLauncher` both have to keep it. A `private`
/// copy in each file was the same eight lines twice.
final class ThreadName: @unchecked Sendable {
    private let lock = NSLock()
    private var seen: String?

    func record(_ name: String?) { lock.withLock { seen = name } }
    var name: String? { lock.withLock { seen } }
}
