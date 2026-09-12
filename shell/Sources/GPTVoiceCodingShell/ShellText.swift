import Foundation
import ShellCore

/// The shell's vocabulary only. Session state words belong to Core.
enum Copy: String, CaseIterable {
    case ready
    case calling
    case onCall
    case ending
    case couldNotConnect
    case callFailed
    case voiceOff
    case duty
    case voice
    case telegramOn
    case telegramOff
    case waitingCount
    case finishedCount
    case call
    case hangUp
    case home
    case settings
    case quit
    case quitAsk
    case keepCall
    case notWatching
    case engineDown
    case openDiagnostics
    case callID
    case callAgent
    case agentLabel
    case context
    case tokens
    case total
    case lastTurn
    case newAgent
    case newAgentAsk
    case keepAgent
    case confirmAgent
    case freshAgent
    case noAgent
    case connectSettings
    case sessions
    case waiting
    case finished
    case children
    case rosterTitle
    case emptyTitle
    case emptyBody
    case back
    case session
    case newest
    case pending
    case answerElsewhere
    case callSettings
    case general
    case telegram
    case diagnostics
    case placeholder
    case engineNotStarted, engineRunning, engineRestarting, engineRepeatedFailures
    case engineAlreadyRunning, engineCannotStart, engineStopped
    case verify
    case checking
    case verifyHint
    case verifyLimit
    case open
    case output
    case copyDiagnostics
    case somethingOff
    case unreadableSettings
    case appVersion
    case engineVersion
    case codexVersion
    case realtimeModel
    case socket
    case log
    case unavailable
    case notChosen
    case loading
    case sessionUnavailable
    case controlPanel
    case model
    case effort
}

struct ShellText {
    static let preferenceKey = "language"
    let language: String
    private let bundle: Bundle

    init(
        language: String = ShellText.preferredLanguage(
            saved: UserDefaults.standard.string(forKey: preferenceKey),
            system: Locale.preferredLanguages)
    ) {
        self.language = language
        bundle = Bundle(
            url: Bundle.shell.bundleURL.appendingPathComponent("\(language.lowercased()).lproj"))!
    }

    static func preferredLanguage(saved: String?, system: [String]) -> String {
        let language = saved ?? system.first ?? "en"
        return language.hasPrefix("zh") ? "zh-Hans" : "en"
    }

    func callAsFunction(_ key: Copy, _ arguments: CVarArg...) -> String {
        String(
            format: bundle.localizedString(forKey: key.rawValue, value: nil, table: nil),
            locale: Locale(identifier: language), arguments: arguments)
    }

    func name(_ name: String?) -> String { name ?? self(.session) }

    func phase(_ phase: CallPhase) -> String {
        switch phase {
        case .ready: return self(.ready)
        case .calling: return self(.calling)
        case .onCall: return self(.onCall)
        case .ending: return self(.ending)
        case .couldNotConnect: return self(.couldNotConnect)
        }
    }
}
