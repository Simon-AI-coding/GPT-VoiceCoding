import Foundation

/// The read-only configuration facts used by the desktop surfaces.
public struct ShellConfiguration: Equatable, Sendable {
    public let model: String?
    public let effort: String?
    public let realtimeModel: String?
    public let logPath: String?
    public let telegramBound: Bool

    public init(_ document: [String: JSONValue]) {
        model = document["model"]?.string
        effort = document["effort"]?.string
        realtimeModel = document["realtime_model"]?.string
        logPath = document["log_path"]?.string
        telegramBound = document["telegram_bound"]?.bool == true
    }

    public static func load(location: EngineLocation, resources: URL?) async throws
        -> ShellConfiguration
    {
        var command = try EngineCommand.resolve(
            resources: resources, configPath: location.configPath)
        command.arguments = [
            "-m", "gpt_voicecoding.shell_configuration", "--config", location.configPath,
        ]
        let report = await InstallationRunner().run(command, separateOutput: true)
        guard report.ok else {
            throw ConfigurationFailure.unreadable(report.lines.joined(separator: "\n"))
        }
        let document = try JSONSerialization.jsonObject(
            with: Data(report.standardOutput!.joined(separator: "\n").utf8))
        guard let fields = JSONValue.of(document).object else {
            throw ConfigurationFailure.unreadable(
                "the configuration reader did not return an object")
        }
        return ShellConfiguration(fields)
    }
}
