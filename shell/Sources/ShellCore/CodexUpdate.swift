import CoreServices
import Foundation

/// The app owns this event source and one serialized check. Python owns all
/// update decisions; file events only request another reading of the facts.
@MainActor
public final class CodexUpdate {
    public static let deadline = 4 * Installation.deadline
    public typealias Check = @Sendable (String) async -> InstallationReport

    private struct Result: Decodable {
        var status: String
        var failure: String
        var attempted: String
        var watchPaths: [String]
        enum CodingKeys: String, CodingKey {
            case status, failure, attempted
            case watchPaths = "watch_paths"
        }
    }

    private let check: Check
    private let report: (String?) -> Void
    private var observer: InstallationChanges?
    private var work: Task<Void, Never>?
    private var pending = false
    private var stopped = false
    private var attempted = ""
    private var lastFailure: String?
    private var watchedPaths: [String] = []

    public init(check: @escaping Check, report: @escaping (String?) -> Void) {
        self.check = check
        self.report = report
    }

    /// Startup and filesystem notifications enter through the same method.
    public func checkForUpdate() {
        guard !stopped else { return }
        pending = true
        guard work == nil else { return }
        work = Task { [weak self] in
            guard let self else { return }
            while pending && !stopped {
                pending = false
                let response = await check(attempted)
                guard !stopped else { break }
                do {
                    let output = (response.standardOutput ?? response.lines).joined(separator: "\n")
                    let result = try JSONDecoder().decode(Result.self, from: Data(output.utf8))
                    attempted = result.attempted
                    if watchedPaths != result.watchPaths {
                        watchedPaths = result.watchPaths
                        try watch()
                        // Close the startup/retarget registration gap once. This
                        // is event reconciliation, not a periodic timer.
                        pending = true
                    }
                    if result.status == "failed" {
                        showFailure(result.failure)
                    } else if result.status == "updated" {
                        showFailure(nil)
                    }
                } catch {
                    showFailure(response.failure ?? "Codex update check failed: \(error)")
                }
            }
            work = nil
        }
    }

    /// Cancel queued checks and listening, but finish an already-started switch
    /// before the app exits. Its runner and Python operations both have bounds.
    public func stop() async {
        stopped = true
        pending = false
        observer?.cancel()
        observer = nil
        await work?.value
        work = nil
    }

    private func watch() throws {
        observer?.cancel()
        observer = try InstallationChanges(paths: watchedPaths) { [weak self] in
            Task { @MainActor in
                guard let self, !self.stopped else { return }
                do { try self.watch() } catch { self.showFailure("Codex watch failed: \(error)") }
                self.checkForUpdate()
            }
        }
    }

    private func showFailure(_ failure: String?) {
        guard failure != lastFailure else { return }
        lastFailure = failure
        report(failure)
    }
}

/// FSEvents follows package contents as well as directory replacement. A watch
/// on only the npm shim misses a replaced native dependency below the package.
private final class InstallationChanges: @unchecked Sendable {
    private final class Callback: @unchecked Sendable {
        let paths: [String]
        let changed: @Sendable () -> Void

        init(paths: [String], changed: @escaping @Sendable () -> Void) {
            self.paths = paths
            self.changed = changed
        }

        func receive(_ events: [String]) {
            let normalized = events.flatMap { event in
                [event, URL(fileURLWithPath: event).resolvingSymlinksInPath().path]
            }
            if normalized.contains(where: { event in
                paths.contains { path in
                    event == path || event.hasPrefix(path + "/") || path.hasPrefix(event + "/")
                }
            }) {
                changed()
            }
        }
    }

    private let callback: Callback
    private var stream: FSEventStreamRef?

    init(paths: [String], changed: @escaping @Sendable () -> Void) throws {
        let normalized = Array(
            Set(
                paths.flatMap { path in
                    let url = URL(fileURLWithPath: path).standardizedFileURL
                    return [url.path, url.resolvingSymlinksInPath().path]
                }))
        callback = Callback(paths: normalized, changed: changed)
        guard !paths.isEmpty else { return }
        let roots = Set(
            normalized.map { path in
                var parent = URL(fileURLWithPath: path).deletingLastPathComponent()
                while !FileManager.default.fileExists(atPath: parent.path) && parent.path != "/" {
                    parent.deleteLastPathComponent()
                }
                return parent.path
            })
        var context = FSEventStreamContext(
            version: 0, info: Unmanaged.passUnretained(callback).toOpaque(),
            retain: nil, release: nil, copyDescription: nil)
        stream = FSEventStreamCreate(
            nil,
            { _, info, _, rawPaths, _, _ in
                guard let info else { return }
                let callback = Unmanaged<Callback>.fromOpaque(info).takeUnretainedValue()
                let events = unsafeBitCast(rawPaths, to: NSArray.self) as? [String] ?? []
                callback.receive(events)
            }, &context, Array(roots) as CFArray,
            FSEventStreamEventId(kFSEventStreamEventIdSinceNow), 0.2,
            FSEventStreamCreateFlags(
                kFSEventStreamCreateFlagUseCFTypes | kFSEventStreamCreateFlagFileEvents
                    | kFSEventStreamCreateFlagWatchRoot))
        guard let stream else { throw CocoaError(.fileReadUnknown) }
        FSEventStreamSetDispatchQueue(stream, .main)
        guard FSEventStreamStart(stream) else {
            cancel()
            throw CocoaError(.fileReadUnknown)
        }
    }

    func cancel() {
        guard let stream else { return }
        FSEventStreamStop(stream)
        FSEventStreamInvalidate(stream)
        FSEventStreamRelease(stream)
        self.stream = nil
    }

    deinit { cancel() }
}
