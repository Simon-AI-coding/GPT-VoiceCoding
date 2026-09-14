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

    public var startURL: URL? {
        guard !username.isEmpty,
            username.unicodeScalars.allSatisfy({
                CharacterSet(
                    charactersIn: "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_"
                )
                .contains($0)
            })
        else { return nil }
        var target = URLComponents()
        target.scheme = "tg"
        target.host = "resolve"
        target.queryItems = [
            URLQueryItem(name: "domain", value: username),
            URLQueryItem(name: "start", value: "bind"),
        ]
        var fragment = URLComponents()
        fragment.queryItems = [URLQueryItem(name: "tgaddr", value: target.string)]
        var web = URLComponents(string: "https://web.telegram.org/a/")!
        web.percentEncodedFragment = fragment.percentEncodedQuery.map { "?" + $0 }
        return web.url
    }
}
