import Foundation
import Testing

@testable import ShellCore

@Suite struct TelegramBindingReadingTests {
    @Test func bindingOpensTheWebClientWithTheBotAndStartParameter() throws {
        let reading = TelegramBindingReading([
            "bot_name": .string("My bot"), "username": .string("my_bot"),
        ])
        let url = try #require(reading.startURL)
        #expect(url.host == "web.telegram.org")
        #expect(url.path == "/a")
        let fragment = try #require(
            URLComponents(url: url, resolvingAgainstBaseURL: false)?.percentEncodedFragment)
        let parameters = try #require(URLComponents(string: fragment)?.queryItems)
        let target = try #require(parameters.first { $0.name == "tgaddr" }?.value)
        let deepLink = try #require(URLComponents(string: target))
        #expect(deepLink.scheme == "tg")
        #expect(deepLink.host == "resolve")
        #expect(deepLink.queryItems?.first { $0.name == "domain" }?.value == "my_bot")
        #expect(deepLink.queryItems?.first { $0.name == "start" }?.value == "bind")
    }

    @Test(arguments: ["", "bad&start=other", "bad/name", "bad#name"])
    func malformedBotNamesHaveNoLink(username: String) {
        #expect(TelegramBindingReading(["username": .string(username)]).startURL == nil)
    }
}
