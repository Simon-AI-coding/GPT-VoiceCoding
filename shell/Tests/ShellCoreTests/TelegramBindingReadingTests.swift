import Foundation
import Testing

@testable import ShellCore

@Suite struct TelegramBindingReadingTests {
    @Test func botIdentityDoesNotImplyARecipient() {
        let reading = TelegramBindingReading([
            "bot_name": .string("My bot"), "username": .string("my_bot"),
        ])
        #expect(reading.botName == "My bot")
        #expect(reading.username == "my_bot")
        #expect(reading.chatID == nil)
    }

    @Test func confirmedBindingCarriesTheRecipient() {
        let reading = TelegramBindingReading([
            "bot_name": .string("My bot"), "username": .string("my_bot"),
            "chat_id": .string("42"),
        ])
        #expect(reading.chatID == "42")
    }
}
