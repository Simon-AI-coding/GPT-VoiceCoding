import Foundation

/// The read-only configuration facts used by the desktop surfaces.
public struct ShellConfiguration: Equatable, Sendable {
    public let model: String?
    public let voice: String?
    public let voices: [String]
    public let realtimeModels: [String]
    public let silenceSeconds: Double?
    public let coolDownSeconds: Double?
    public let speechSettleSeconds: Double?
    public let effort: String?
    public let realtimeModel: String?
    public let logPath: String?
    public let telegramBound: Bool
    public let telegramName: String?
    public let tokenVariable: String?
    public let chatID: String?

    public init(_ document: [String: JSONValue]) {
        model = document["model"]?.string
        voice = document["voice"]?.string
        voices = document["voices"]?.array?.compactMap(\.string) ?? []
        realtimeModels = document["realtime_models"]?.array?.compactMap(\.string) ?? []
        silenceSeconds = document["silence_end_seconds"]?.number
        coolDownSeconds = document["cool_down_seconds"]?.number
        speechSettleSeconds = document["speech_settle_seconds"]?.number
        effort = document["effort"]?.string
        realtimeModel = document["realtime_model"]?.string
        logPath = document["log_path"]?.string
        telegramBound = document["telegram_bound"]?.bool == true
        telegramName = document["telegram_name"]?.string
        tokenVariable = document["token_env"]?.string
        chatID = document["chat_id"]?.string
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
