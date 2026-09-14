import Foundation

/// Facts returned by the engine's binding conversation. The shell never calls Telegram.
public struct TelegramBindingReading: Equatable, Sendable {
    public let botName: String
    public let username: String
    public let chatID: String?

    init(_ document: [String: JSONValue]) {
        botName = document["bot_name"]?.string ?? ""
        username = document["username"]?.string ?? ""
        chatID = document["chat_id"]?.string
    }

}
