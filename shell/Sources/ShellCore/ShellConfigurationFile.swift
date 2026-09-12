import Foundation

public enum EngineSetting: Sendable {
    case voice, realtimeModel, silence, coolDown, speechSettle, effort

    var address: (table: String, key: String) {
        switch self {
        case .voice: return ("adapters.settings.call", "voice")
        case .realtimeModel: return ("adapters.settings.call", "realtime_model")
        case .silence: return ("policy", "silence_end_seconds")
        case .coolDown: return ("policy", "cool_down_seconds")
        case .speechSettle: return ("policy", "speech_settle_seconds")
        case .effort: return ("delegate", "effort")
        }
    }
}

/// The user's file, edited in place. Validation is the same Python reading used
/// at launch; the prospective document is checked before the original changes.
public struct ShellConfigurationFile: Sendable {
    // Kept in agreement with Python's adapter references by test_app_bundle.py.
    private static let telegramAdapter =
        "gpt_voicecoding.adapters.companion_channel.telegram:telegram_channel"
    private static let nullAdapter = "gpt_voicecoding.adapters.companion_channel:null_channel"
    /// The variable a new binding names; an existing token_env always wins.
    private static let defaultTokenVariable = "GPTVOICECODING_TELEGRAM_TOKEN"
    public let location: EngineLocation
    private let read: @Sendable (EngineLocation) async throws -> ShellConfiguration

    public init(
        location: EngineLocation,
        read: @escaping @Sendable (EngineLocation) async throws -> ShellConfiguration
    ) {
        self.location = location
        self.read = read
    }

    public func load() async throws -> ShellConfiguration { try await read(location) }

    public func createIfMissing(template: URL, delegateCLI: URL) throws -> Bool {
        let destination = URL(fileURLWithPath: location.configPath)
        guard !FileManager.default.fileExists(atPath: destination.path) else { return false }
        let template = try String(contentsOf: template, encoding: .utf8)
        let text = try MinimalTOML.setting(
            .string(delegateCLI.path), forKey: "cli", inTable: "delegate", of: template)
        try FileManager.default.createDirectory(
            at: destination.deletingLastPathComponent(), withIntermediateDirectories: true)
        try Data(text.utf8).write(to: destination, options: .withoutOverwriting)
        return true
    }

    public func save(_ setting: EngineSetting, value: JSONValue) async throws -> ShellConfiguration
    {
        let original = try String(contentsOfFile: location.configPath, encoding: .utf8)
        let address = setting.address
        let edited = try MinimalTOML.setting(
            value, forKey: address.key, inTable: address.table, of: original)
        return try await replace(with: edited)
    }

    public func bindTelegram(_ binding: TelegramBindingReading, token: String) async throws
        -> ShellConfiguration
    {
        guard let chatID = binding.chatID else {
            throw ConfigurationFailure.unreadable("Telegram binding has not been confirmed")
        }
        var text = try String(contentsOfFile: location.configPath, encoding: .utf8)
        let variable =
            try MinimalTOML.string(
                forKey: "token_env", inTable: "adapters.settings.companion_channel", of: text)
            ?? Self.defaultTokenVariable
        for (table, key, value) in [
            (
                "adapters", "companion_channel",
                Self.telegramAdapter
            ),
            ("adapters.settings.companion_channel", "token_env", variable),
            ("adapters.settings.companion_channel", "chat_id", chatID),
            ("shell.telegram", "bot_name", binding.botName),
        ] {
            text = try MinimalTOML.setting(.string(value), forKey: key, inTable: table, of: text)
        }
        return try await replace(with: text) { candidate in
            let credentials = TelegramCredentials(configPath: location.configPath)
            _ = try TelegramCredentials(
                configPath: candidate.path, environmentPath: credentials.environmentPath
            ).save(token: token)
        }
    }

    public func saveAgentModel(_ model: String, effort: String?) async throws -> ShellConfiguration
    {
        var text = try String(contentsOfFile: location.configPath, encoding: .utf8)
        text = try MinimalTOML.setting(
            .string(model), forKey: "model", inTable: "delegate", of: text)
        if let effort {
            text = try MinimalTOML.setting(
                .string(effort), forKey: "effort", inTable: "delegate", of: text)
        } else {
            text = MinimalTOML.removing(key: "effort", inTable: "delegate", from: text)
        }
        return try await replace(with: text)
    }

    public func unbindTelegram() async throws -> ShellConfiguration {
        var text = try String(contentsOfFile: location.configPath, encoding: .utf8)
        text = MinimalTOML.removing(table: "adapters.settings.companion_channel", from: text)
        text = MinimalTOML.removing(table: "shell.telegram", from: text)
        text = try MinimalTOML.setting(
            .string(Self.nullAdapter),
            forKey: "companion_channel", inTable: "adapters", of: text)
        return try await replace(with: text) { _ in
            let path = TelegramCredentials(configPath: location.configPath).environmentPath
            if FileManager.default.fileExists(atPath: path) {
                try FileManager.default.removeItem(atPath: path)
            }
        }
    }

    private func replace(with text: String, afterWrite: (URL) throws -> Void = { _ in })
        async throws -> ShellConfiguration
    {
        let destination = URL(fileURLWithPath: location.configPath)
        let original = try Data(contentsOf: destination)
        let candidate = destination.deletingLastPathComponent()
            .appendingPathComponent(".settings-\(UUID().uuidString).toml")
        try Data(text.utf8).write(to: candidate, options: .atomic)
        defer { try? FileManager.default.removeItem(at: candidate) }
        let reading = try await read(
            EngineLocation(configPath: candidate.path, socketPath: location.socketPath))
        try Data(text.utf8).write(to: destination, options: .atomic)
        do {
            try afterWrite(candidate)
        } catch {
            try original.write(to: destination, options: .atomic)
            throw error
        }
        return reading
    }
}
